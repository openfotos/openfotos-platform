import base64
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx

from openfotos_desktop.api_client import AuthenticatedApiClient
from openfotos_desktop.object_transfer import ObjectTransferClient


class MemoryTokenStore:
    persistence_warning = None

    def __init__(self) -> None:
        self.tokens = {}

    def save(self, server_url, installation_id, refresh_token):
        self.tokens[(server_url, installation_id)] = refresh_token
        return True

    def load(self, server_url, installation_id):
        return self.tokens.get((server_url, installation_id))

    def delete(self, server_url, installation_id):
        self.tokens.pop((server_url, installation_id), None)


def test_authenticated_transport_refreshes_once_and_retries_with_the_new_token() -> None:
    installation_id = uuid4()
    token_store = MemoryTokenStore()
    seen_authorization = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login/":
            return httpx.Response(
                200,
                json={"access_token": "old-access", "refresh_token": "old-refresh"},
            )
        if request.url.path == "/api/v1/auth/refresh/":
            assert json.loads(request.read()) == {"refresh_token": "old-refresh"}
            return httpx.Response(
                200,
                json={"access_token": "new-access", "refresh_token": "new-refresh"},
            )
        if request.url.path == "/api/v1/events/":
            seen_authorization.append(request.headers["authorization"])
            if request.headers["authorization"] == "Bearer old-access":
                return httpx.Response(401, json={"error": {"code": "expired"}})
            return httpx.Response(200, json={"events": []})
        raise AssertionError(f"Unexpected request {request.url.path}")

    client = AuthenticatedApiClient(
        installation_id,
        token_store=token_store,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.start_session(
        "https://alpha.example",
        "/api/v1/auth/login/",
        {"installation_id": str(installation_id)},
    )

    assert client.request("GET", "/api/v1/events/") == {"events": []}
    assert seen_authorization == ["Bearer old-access", "Bearer new-access"]
    assert token_store.load("https://alpha.example", installation_id) == "new-refresh"


def test_object_transfer_reports_digests_for_the_exact_streamed_bytes(tmp_path: Path) -> None:
    content = b"synthetic private image bytes"
    source = tmp_path / "source.jpg"
    destination = tmp_path / "downloaded.jpg"
    source.write_bytes(content)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            assert request.read() == content
            return httpx.Response(200)
        if request.method == "GET":
            return httpx.Response(200, content=content)
        raise AssertionError(f"Unexpected request {request.method}")

    transfers = ObjectTransferClient(httpx.Client(transport=httpx.MockTransport(handler)))

    uploaded = transfers.put_file(
        url="https://storage.invalid/private.jpg",
        headers={"If-None-Match": "*"},
        path=source,
    )
    downloaded = transfers.download_file(
        url="https://storage.invalid/private.jpg",
        destination=destination,
    )

    assert uploaded.sha256 == downloaded.sha256 == hashlib.sha256(content).hexdigest()
    assert (
        uploaded.content_md5
        == base64.b64encode(hashlib.md5(content, usedforsecurity=False).digest()).decode()
    )
    assert destination.read_bytes() == content
