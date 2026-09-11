import base64
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from keyring.errors import KeyringError
from PIL import Image

from openfotos_desktop import credentials
from openfotos_desktop.credentials import RefreshTokenStore
from openfotos_desktop.ingestion import (
    BatchState,
    CheckpointStore,
    EventCache,
    InventoryScanner,
    LocalUploadState,
)
from openfotos_desktop.network import DesktopNetworkService, SourceChangedError


class MemoryTokenStore:
    def __init__(self) -> None:
        self.tokens = {}
        self.persistence_warning = None

    def save(self, server_url, installation_id, refresh_token):
        self.tokens[(server_url, installation_id)] = refresh_token
        return True

    def load(self, server_url, installation_id):
        return self.tokens.get((server_url, installation_id))


def approved_batch(tmp_path: Path):
    photo = tmp_path / "source.jpg"
    Image.new("RGB", (8, 6), color="navy").save(photo, format="JPEG")
    store = CheckpointStore(tmp_path / "checkpoint.sqlite3")
    event_id = uuid4()
    store.cache_event(
        EventCache(
            id=event_id,
            name="Reception",
            storage_limit_bytes=25_000_000_000,
            processing_profile_id="pilot-profile-v1",
            server_url="http://localhost:8000",
            role="lead",
            device_label="Lead workstation",
        )
    )
    batch_id = store.create_batch(event_id, label="Edited originals")
    store.add_files(batch_id, [photo])
    InventoryScanner(store).scan(batch_id)
    store.approve_batch(batch_id, supported_profile_id="pilot-profile-v1")
    return store, event_id, batch_id, photo


def api_handler(event_id: UUID, batch_id: UUID, asset_id: UUID, state: dict):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/auth/login/":
            return httpx.Response(
                200,
                json={
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "events": [
                        {
                            "id": str(event_id),
                            "name": "Reception",
                            "storage_limit_bytes": 25_000_000_000,
                            "processing_profile_id": "pilot-profile-v1",
                            "role": "lead",
                            "reserved_original_bytes": 0,
                            "verified_original_bytes": 0,
                            "intake_state": "open",
                            "intake_generation": 1,
                            "device_label": "",
                        }
                    ],
                },
            )
        if path == f"/api/v1/events/{event_id}/batches/":
            state["manifest"] = json.loads(request.content)
            return httpx.Response(201, json={"id": str(batch_id), "state": "reserved"})
        if path == f"/api/v1/events/{event_id}/batches/{batch_id}/":
            return httpx.Response(
                200,
                json={
                    "id": str(batch_id),
                    "state": "complete" if state.get("verified") else "reserved",
                    "assets": [
                        {
                            "asset_id": str(asset_id),
                            "state": "verified" if state.get("verified") else "reserved",
                            "failure_code": "",
                        }
                    ],
                },
            )
        if path == f"/api/v1/events/{event_id}/batches/{batch_id}/upload-leases/":
            assert json.loads(request.content) == {"asset_ids": [str(asset_id)]}
            manifest_asset = state["manifest"]["assets"][0]
            return httpx.Response(
                200,
                json={
                    "leases": [
                        {
                            "asset_id": str(asset_id),
                            "url": "https://storage.invalid/original",
                            "headers": {
                                "Content-Length": str(manifest_asset["size_bytes"]),
                                "Content-MD5": manifest_asset["content_md5"],
                                "Content-Type": "image/jpeg",
                                "If-None-Match": "*",
                                "x-amz-meta-openfotos-sha256": manifest_asset["sha256"],
                            },
                        }
                    ]
                },
            )
        if path == f"/api/v1/events/{event_id}/assets/{asset_id}/complete/":
            state["verified"] = True
            return httpx.Response(200, json={"asset_id": str(asset_id), "state": "verified"})
        raise AssertionError(f"Unexpected API request: {request.method} {path}")

    return handle


def test_direct_upload_retries_then_resumes_at_the_verified_object_boundary(tmp_path: Path) -> None:
    store, event_id, batch_id, photo = approved_batch(tmp_path)
    asset_id = store.list_items(batch_id)[0].id
    state = {"storage_attempts": 0}
    sleeps = []

    def storage_handler(request: httpx.Request) -> httpx.Response:
        state["storage_attempts"] += 1
        content = request.read()
        assert content == photo.read_bytes()
        assert request.headers["if-none-match"] == "*"
        assert request.headers["content-type"] == "image/jpeg"
        if state["storage_attempts"] < 5:
            return httpx.Response(503)
        return httpx.Response(200)

    service = DesktopNetworkService(
        store,
        token_store=MemoryTokenStore(),
        api_client=httpx.Client(
            transport=httpx.MockTransport(api_handler(event_id, batch_id, asset_id, state))
        ),
        storage_client=httpx.Client(transport=httpx.MockTransport(storage_handler)),
        sleeper=sleeps.append,
        jitter=lambda _start, maximum: maximum,
    )
    events = service.sign_in_lead(
        "http://localhost:8000",
        "lead",
        "password",
        "Lead workstation",
    )
    progress = []

    service.upload(
        batch_id, transfer_limit=1, on_progress=lambda done, total: progress.append((done, total))
    )

    assert events[0].device_label == "Lead workstation"
    assert state["manifest"]["device_label"] == "Lead workstation"
    assert state["storage_attempts"] == 5
    assert sleeps == [1.0, 2.0, 4.0, 8.0]
    assert progress[-1] == (1, 1)
    assert store.get_batch(batch_id).state is BatchState.COMPLETE
    checkpoint = store.get_upload_checkpoint(asset_id)
    assert checkpoint.state is LocalUploadState.VERIFIED
    assert checkpoint.attempt_count == 5
    service.close()
    store.close()


def test_source_change_after_approval_fails_without_uploading(tmp_path: Path) -> None:
    store, event_id, batch_id, photo = approved_batch(tmp_path)
    asset_id = store.list_items(batch_id)[0].id
    state = {}
    storage_calls = []
    service = DesktopNetworkService(
        store,
        token_store=MemoryTokenStore(),
        api_client=httpx.Client(
            transport=httpx.MockTransport(api_handler(event_id, batch_id, asset_id, state))
        ),
        storage_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: storage_calls.append(request) or httpx.Response(200)
            )
        ),
    )
    service.sign_in_lead("http://localhost:8000", "lead", "password", "Lead workstation")
    photo.write_bytes(photo.read_bytes() + b"changed")

    with pytest.raises(SourceChangedError):
        service.upload(batch_id, transfer_limit=1, on_progress=lambda *_: None)

    assert storage_calls == []
    assert store.get_upload_checkpoint(asset_id).last_error_code == "source_changed"
    service.close()
    store.close()


def test_refresh_tokens_use_memory_only_when_keyring_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "checkpoint.sqlite3"
    with CheckpointStore(database) as store:
        token_store = RefreshTokenStore()

        def unavailable(*_args):
            raise KeyringError("synthetic unavailable keyring")

        monkeypatch.setattr(credentials.keyring, "set_password", unavailable)
        assert not token_store.save(
            "https://alpha.example", store.installation_id, "secret-refresh"
        )
        assert token_store.load("https://alpha.example", store.installation_id) == "secret-refresh"
        assert token_store.persistence_warning is not None

    assert b"secret-refresh" not in database.read_bytes()


def test_presigned_manifest_headers_match_local_checksums(tmp_path: Path) -> None:
    store, _, batch_id, photo = approved_batch(tmp_path)
    item = store.list_items(batch_id)[0]

    assert item.sha256 == hashlib.sha256(photo.read_bytes()).hexdigest()
    assert (
        item.content_md5
        == base64.b64encode(
            hashlib.md5(photo.read_bytes(), usedforsecurity=False).digest()
        ).decode()
    )
    store.close()
