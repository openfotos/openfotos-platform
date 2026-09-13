"""Authenticated JSON transport for the desktop control plane."""

from dataclasses import dataclass
from threading import Lock
from uuid import UUID

import httpx

from .credentials import RefreshTokenStore
from .errors import DesktopApiError


@dataclass(frozen=True)
class _Session:
    server_url: str
    access_token: str
    refresh_token: str


class AuthenticatedApiClient:
    def __init__(
        self,
        installation_id: UUID,
        *,
        token_store: RefreshTokenStore | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.installation_id = installation_id
        self.token_store = token_store or RefreshTokenStore()
        self.client = client or httpx.Client(timeout=httpx.Timeout(30, connect=10))
        self._owns_client = client is None
        self._session: _Session | None = None
        self._refresh_lock = Lock()

    @property
    def persistence_warning(self) -> str | None:
        return self.token_store.persistence_warning

    @property
    def server_url(self) -> str:
        return self._require_session().server_url

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def start_session(self, origin: str, path: str, body: dict) -> dict:
        response = self._public_post(origin, path, body)
        self._accept_tokens(origin, response)
        return response

    def resume(self, origin: str) -> dict:
        refresh_token = self.token_store.load(origin, self.installation_id)
        if not refresh_token:
            raise DesktopApiError("session_unavailable", "Sign in to continue.")
        self._session = _Session(origin, "", refresh_token)
        self._refresh()
        return self.request("GET", "/api/v1/events/")

    def request(self, method: str, path: str, *, json=None, idempotency_key=None) -> dict:
        session = self._require_session()
        headers = {"Authorization": f"Bearer {session.access_token}"}
        if idempotency_key:
            headers["Idempotency-Key"] = str(idempotency_key)
        response = self._send(method, session.server_url, path, json=json, headers=headers)
        if response.status_code == 401:
            stale_access_token = session.access_token
            with self._refresh_lock:
                if self._require_session().access_token == stale_access_token:
                    self._refresh()
            session = self._require_session()
            headers["Authorization"] = f"Bearer {session.access_token}"
            response = self._send(method, session.server_url, path, json=json, headers=headers)
        return _response_json(response)

    def _send(self, method: str, origin: str, path: str, *, json, headers) -> httpx.Response:
        try:
            return self.client.request(method, f"{origin}{path}", json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise DesktopApiError(
                "server_unavailable",
                "The OpenFotos server could not be reached.",
                retryable=True,
            ) from exc

    def _refresh(self) -> None:
        session = self._require_session()
        try:
            response = self._public_post(
                session.server_url,
                "/api/v1/auth/refresh/",
                {"refresh_token": session.refresh_token},
            )
        except DesktopApiError as exc:
            if exc.code in {"invalid_access_token", "invalid_refresh_token"}:
                self.token_store.delete(session.server_url, self.installation_id)
                self._session = None
            raise
        self._accept_tokens(session.server_url, response)

    def _public_post(self, origin: str, path: str, body: dict) -> dict:
        try:
            response = self.client.post(f"{origin}{path}", json=body)
        except httpx.HTTPError as exc:
            raise DesktopApiError(
                "server_unavailable",
                "The OpenFotos server could not be reached.",
                retryable=True,
            ) from exc
        return _response_json(response)

    def _accept_tokens(self, origin: str, response: dict) -> None:
        session = _Session(
            server_url=origin,
            access_token=response["access_token"],
            refresh_token=response["refresh_token"],
        )
        self._session = session
        self.token_store.save(origin, self.installation_id, session.refresh_token)

    def _require_session(self) -> _Session:
        if self._session is None:
            raise DesktopApiError("session_unavailable", "Sign in to continue.")
        return self._session


def _response_json(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError as exc:
        raise DesktopApiError(
            "invalid_server_response", "The OpenFotos server returned an invalid response."
        ) from exc
    if response.is_error:
        error = body.get("error", {}) if isinstance(body, dict) else {}
        raise DesktopApiError(
            str(error.get("code", "server_error")),
            str(error.get("message", "The OpenFotos request failed.")),
            retryable=bool(error.get("retryable")),
        )
    if not isinstance(body, dict):
        raise DesktopApiError(
            "invalid_server_response", "The OpenFotos server returned an invalid response."
        )
    return body
