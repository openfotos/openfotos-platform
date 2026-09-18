"""Validate deployment inputs and replace this process with Gunicorn."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

import django
from django.core.management import call_command


class StartupConfigurationError(ValueError):
    pass


def gunicorn_arguments(environment: Mapping[str, str]) -> list[str]:
    port = _bounded_integer(environment, "PORT", default=8000, minimum=1, maximum=65_535)
    workers = _bounded_integer(environment, "GUNICORN_WORKERS", default=1, minimum=1, maximum=32)
    threads = _bounded_integer(environment, "GUNICORN_THREADS", default=4, minimum=1, maximum=64)
    return [
        "gunicorn",
        "openfotos_server.wsgi:application",
        "--bind",
        f"0.0.0.0:{port}",
        "--workers",
        str(workers),
        "--threads",
        str(threads),
        "--error-logfile",
        "-",
    ]


def _bounded_integer(
    environment: Mapping[str, str],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw_value = environment.get(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise StartupConfigurationError(f"{name} must be an integer.") from exc
    if not minimum <= value <= maximum:
        raise StartupConfigurationError(f"{name} must be between {minimum} and {maximum}.")
    return value


def main(arguments: Sequence[str] | None = None) -> None:
    if arguments:
        raise SystemExit("The server startup command does not accept arguments.")
    try:
        command = gunicorn_arguments(os.environ)
    except StartupConfigurationError as exc:
        raise SystemExit(str(exc)) from exc
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openfotos_server.settings")
    django.setup()
    call_command("verify_deployment")
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
