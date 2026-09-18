"""Run a privacy-conscious rehearsal load check against one published portal."""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx


class LoadTestConfigurationError(RuntimeError):
    pass


class _FormParser(HTMLParser):
    def __init__(self, action_fragment: str) -> None:
        super().__init__()
        self.action_fragment = action_fragment
        self.action = ""
        self.csrf_token = ""
        self._inside_target = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form":
            action = attributes.get("action") or ""
            self._inside_target = self.action_fragment in action
            if self._inside_target:
                self.action = action
        elif (
            tag == "input"
            and self._inside_target
            and attributes.get("name") == "csrfmiddlewaretoken"
        ):
            self.csrf_token = attributes.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._inside_target = False


def _form(html: str, *, base_url: str, action_fragment: str) -> tuple[str, str]:
    parser = _FormParser(action_fragment)
    parser.feed(html)
    if not parser.action or not parser.csrf_token:
        raise LoadTestConfigurationError(f"Could not find the {action_fragment!r} form.")
    return urljoin(base_url, parser.action), parser.csrf_token


def percentile(values: list[float], percentage: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, int((len(ordered) * percentage + 99) // 100) - 1)
    return ordered[min(index, len(ordered) - 1)]


@dataclass(slots=True)
class _PreparedUser:
    client: httpx.AsyncClient
    action_url: str
    csrf_token: str


@dataclass(frozen=True, slots=True)
class _Measurement:
    seconds: float
    error: str = ""


async def _unlock(client: httpx.AsyncClient, portal_url: str, pin: str) -> httpx.Response:
    landing = await client.get(portal_url)
    landing.raise_for_status()
    action, csrf_token = _form(
        landing.text,
        base_url=str(landing.url),
        action_fragment="/unlock/",
    )
    return await client.post(action, data={"csrfmiddlewaretoken": csrf_token, "pin": pin})


async def _prepare_user(
    operation: str,
    *,
    portal_url: str,
    pin: str,
) -> _PreparedUser:
    client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(30, connect=10),
        trust_env=False,
        headers={"User-Agent": "OpenFotos-Rehearsal-Load-Test/0.1"},
    )
    try:
        if operation == "unlock":
            landing = await client.get(portal_url)
            landing.raise_for_status()
            action, token = _form(
                landing.text,
                base_url=str(landing.url),
                action_fragment="/unlock/",
            )
            return _PreparedUser(client, action, token)
        gallery = await _unlock(client, portal_url, pin)
        gallery.raise_for_status()
        if operation == "gallery":
            return _PreparedUser(client, portal_url, "")
        action, token = _form(
            gallery.text,
            base_url=str(gallery.url),
            action_fragment="/search/",
        )
        return _PreparedUser(client, action, token)
    except Exception:
        await client.aclose()
        raise


async def _measure_user(
    prepared: _PreparedUser,
    *,
    operation: str,
    pin: str,
    reference_photo: bytes | None,
    reference_name: str,
    reference_type: str,
    start: asyncio.Event,
) -> _Measurement:
    await start.wait()
    started = time.perf_counter()
    try:
        if operation == "unlock":
            response = await prepared.client.post(
                prepared.action_url,
                data={"csrfmiddlewaretoken": prepared.csrf_token, "pin": pin},
            )
        elif operation == "gallery":
            response = await prepared.client.get(prepared.action_url)
        else:
            response = await prepared.client.post(
                prepared.action_url,
                data={"csrfmiddlewaretoken": prepared.csrf_token, "consent": "on"},
                files={
                    "reference_photo": (
                        reference_name,
                        reference_photo,
                        reference_type,
                    )
                },
            )
        elapsed = time.perf_counter() - started
        response.raise_for_status()
        if operation == "search":
            clear_url, clear_token = _form(
                response.text,
                base_url=str(response.url),
                action_fragment="/clear/",
            )
            cleanup = await prepared.client.post(
                clear_url,
                data={"csrfmiddlewaretoken": clear_token},
            )
            cleanup.raise_for_status()
        return _Measurement(elapsed)
    except (httpx.HTTPError, LoadTestConfigurationError) as exc:
        return _Measurement(time.perf_counter() - started, type(exc).__name__)
    finally:
        await prepared.client.aclose()


async def run_load_test(
    *,
    operation: str,
    users: int,
    portal_url: str,
    pin: str,
    reference_photo: bytes | None,
    reference_name: str,
    reference_type: str,
) -> list[_Measurement]:
    preparation_results = await asyncio.gather(
        *(_prepare_user(operation, portal_url=portal_url, pin=pin) for _ in range(users)),
        return_exceptions=True,
    )
    prepared = [result for result in preparation_results if isinstance(result, _PreparedUser)]
    failures = [result for result in preparation_results if isinstance(result, BaseException)]
    if failures:
        await asyncio.gather(*(user.client.aclose() for user in prepared))
        raise failures[0]
    start = asyncio.Event()
    tasks = [
        asyncio.create_task(
            _measure_user(
                user,
                operation=operation,
                pin=pin,
                reference_photo=reference_photo,
                reference_name=reference_name,
                reference_type=reference_type,
                start=start,
            )
        )
        for user in prepared
    ]
    start.set()
    return await asyncio.gather(*tasks)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("gallery", "unlock", "search"))
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--reference-photo", type=Path)
    parser.add_argument("--confirm-reference-consent", action="store_true")
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    if not 1 <= arguments.users <= 500:
        raise SystemExit("--users must be between 1 and 500.")
    portal_url = os.environ.get("OPENFOTOS_LOAD_PORTAL_URL", "").strip()
    pin = os.environ.get("OPENFOTOS_LOAD_PORTAL_PIN", "").strip()
    if urlparse(portal_url).scheme != "https":
        raise SystemExit("OPENFOTOS_LOAD_PORTAL_URL must be an HTTPS URL.")
    if len(pin) != 4 or not pin.isascii() or not pin.isdigit():
        raise SystemExit("OPENFOTOS_LOAD_PORTAL_PIN must contain four ASCII digits.")

    reference = None
    reference_name = ""
    reference_type = "application/octet-stream"
    if arguments.operation == "search":
        if not arguments.confirm_reference_consent:
            raise SystemExit("Search load tests require --confirm-reference-consent.")
        if arguments.reference_photo is None or not arguments.reference_photo.is_file():
            raise SystemExit("Search load tests require an existing --reference-photo.")
        reference = arguments.reference_photo.read_bytes()
        if not reference or len(reference) > 20 * 1024 * 1024:
            raise SystemExit("The reference photo must contain at most 20 MiB.")
        reference_name = arguments.reference_photo.name
        reference_type = mimetypes.guess_type(reference_name)[0] or reference_type

    try:
        measurements = asyncio.run(
            run_load_test(
                operation=arguments.operation,
                users=arguments.users,
                portal_url=portal_url,
                pin=pin,
                reference_photo=reference,
                reference_name=reference_name,
                reference_type=reference_type,
            )
        )
    except (httpx.HTTPError, LoadTestConfigurationError) as exc:
        raise SystemExit(f"Load-test setup failed: {type(exc).__name__}.") from exc

    errors = [measurement.error for measurement in measurements if measurement.error]
    seconds = [measurement.seconds for measurement in measurements]
    error_rate = len(errors) / len(measurements)
    result = {
        "operation": arguments.operation,
        "users": arguments.users,
        "completed": len(measurements) - len(errors),
        "errors": len(errors),
        "error_rate": round(error_rate, 4),
        "p50_seconds": round(percentile(seconds, 50), 3),
        "p95_seconds": round(percentile(seconds, 95), 3),
        "max_seconds": round(max(seconds), 3),
        "error_types": sorted(set(errors)),
    }
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    latency_limit = 10 if arguments.operation == "search" else 2
    if error_rate >= 0.01 or result["p95_seconds"] > latency_limit:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
