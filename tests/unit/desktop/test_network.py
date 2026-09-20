import base64
import hashlib
import json
import threading
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from keyring.errors import KeyringError
from PIL import Image

from openfotos_desktop import credentials
from openfotos_desktop.credentials import RefreshTokenStore
from openfotos_desktop.face_models import FaceModelSetupError
from openfotos_desktop.ingestion import (
    BatchState,
    CheckpointStore,
    EventCache,
    InventoryScanner,
    LocalUploadState,
    PreviewPolicyCache,
    SubEventCache,
)
from openfotos_desktop.network import (
    DesktopApiError,
    DesktopNetworkService,
    SourceChangedError,
    _server_origin,
)
from openfotos_vision import ACCEPTED_FACE_MODEL_CONTRACT

_SUB_EVENT = SubEventCache(
    id=UUID("00000000-0000-4000-8000-000000000104"),
    name="Reception",
    position=1,
)


class NoFaceEngine:
    model = ACCEPTED_FACE_MODEL_CONTRACT.model
    runtime_versions = {"synthetic": "1"}

    def detect_and_embed(self, image_bytes: bytes):
        assert image_bytes
        return ()


class MemoryTokenStore:
    def __init__(self) -> None:
        self.tokens = {}
        self.persistence_warning = None

    def save(self, server_url, installation_id, refresh_token):
        self.tokens[(server_url, installation_id)] = refresh_token
        return True

    def load(self, server_url, installation_id):
        return self.tokens.get((server_url, installation_id))

    def delete(self, server_url, installation_id):
        self.tokens.pop((server_url, installation_id), None)


def approved_batch(tmp_path: Path):
    photo = tmp_path / "source.jpg"
    Image.new("RGB", (8, 6), color="navy").save(photo, format="JPEG")
    store = CheckpointStore(tmp_path / "checkpoint.sqlite3")
    event_id = uuid4()
    store.cache_event(
        EventCache(
            id=event_id,
            name="Reception",
            storage_limit_bytes=50_000_000_000,
            processing_profile_id="pilot-profile-v1",
            server_url="http://localhost:8000",
            device_label="Studio workstation",
            sub_events=(_SUB_EVENT,),
        )
    )
    batch_id = store.create_batch(event_id, _SUB_EVENT.id, label="Edited originals")
    store.add_files(batch_id, [photo])
    InventoryScanner(store).scan(batch_id)
    store.approve_batch(batch_id, supported_profile_id="pilot-profile-v1")
    return store, event_id, batch_id, photo


def test_local_tenant_hosts_allow_http_but_remote_hosts_require_https() -> None:
    assert _server_origin("http://alpha.localhost:8000") == "http://alpha.localhost:8000"
    assert _server_origin("https://alpha.openfotos.example") == "https://alpha.openfotos.example"

    with pytest.raises(DesktopApiError, match="requires HTTPS"):
        _server_origin("http://alpha.openfotos.example")
    with pytest.raises(DesktopApiError, match="origin only"):
        _server_origin("https://alpha.openfotos.example/api/v1")


def api_handler(event_id: UUID, batch_id: UUID, asset_id: UUID, state: dict):
    def event_data():
        return {
            "id": str(event_id),
            "name": "Reception",
            "state": "uploading",
            "storage_limit_bytes": 50_000_000_000,
            "processing_profile_id": "pilot-profile-v1",
            "face_model_id": "opencv-yunet-2023mar-sface-2021dec",
            "face_index_ready": False,
            "sub_events": [
                {
                    "id": str(_SUB_EVENT.id),
                    "name": _SUB_EVENT.name,
                    "position": _SUB_EVENT.position,
                }
            ],
            "reserved_original_bytes": 0,
            "verified_original_bytes": 0,
            "remaining_original_bytes": 50_000_000_000,
            "intake_state": state.get("intake_state", "open"),
            "intake_generation": state.get("intake_generation", 1),
            "max_contribution_devices": 10,
            "active_contribution_devices": 1,
            "device_label": "",
            "preview_policy": state.get("policy"),
        }

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/auth/login/":
            return httpx.Response(
                200,
                json={
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "events": [event_data()],
                },
            )
        if path == "/api/v1/auth/refresh/":
            return httpx.Response(
                200,
                json={"access_token": "access-token", "refresh_token": "refresh-token"},
            )
        if path == "/api/v1/events/":
            if state.get("policy_after_refresh"):
                state["policy"] = state["policy_after_refresh"]
            return httpx.Response(200, json={"events": [event_data()]})
        if path == "/api/v1/auth/logout/":
            state["revoked_refresh_token"] = json.loads(request.content)["refresh_token"]
            return httpx.Response(200, json={"revoked": True})
        if path == f"/api/v1/events/{event_id}/preview-policy/confirm/":
            body = json.loads(request.content)
            assert body == {
                "enabled": False,
                "template": "compact-bottom-right",
                "text": "",
                "logo_kind": "none",
                "mark_png_base64": "",
            }
            state["clean_preview_confirmations"] = state.get("clean_preview_confirmations", 0) + 1
            state["policy"] = {
                "id": str(uuid4()),
                "enabled": False,
                "template": "compact-bottom-right",
                "text": "",
                "logo_kind": "none",
                "renderer_id": "watermark-raster-v1",
                "derivative_profile_id": "gallery-jpeg-v1",
                "mark_sha256": "",
            }
            return httpx.Response(201, json={"preview_policy": state["policy"]})
        if path == f"/api/v1/events/{event_id}/batches/":
            state["manifest"] = json.loads(request.content)
            return httpx.Response(201, json={"id": str(batch_id), "state": "reserved"})
        if path == f"/api/v1/events/{event_id}/batches/{batch_id}/":
            assets = [
                {
                    "asset_id": str(asset_id),
                    "variant": "originals",
                    "state": (
                        "excluded"
                        if state.get("not_included")
                        else ("verified" if state.get("verified") else "reserved")
                    ),
                    "failure_code": "",
                    "gallery_excluded": False,
                    "face_analysis": {
                        "state": state.get("face_state", "pending"),
                        "attempt_count": state.get("face_attempts", 0),
                        "failure_code": "",
                        "detected_face_count": 0,
                        "usable_face_count": 0,
                    },
                }
            ]
            assets.extend(
                {
                    "asset_id": str(asset_id),
                    "variant": value["variant"],
                    "state": (
                        "verified"
                        if value["variant"] in state.get("verified_derivatives", set())
                        else "reserved"
                    ),
                    "failure_code": "",
                    "gallery_excluded": False,
                    "attempt_count": state.get("derivative_attempts", 0),
                }
                for value in state.get("derivative_manifest", [])
            )
            return httpx.Response(
                200,
                json={
                    "id": str(batch_id),
                    "sub_event_id": str(_SUB_EVENT.id),
                    "state": (
                        "not_included"
                        if state.get("not_included")
                        else ("complete" if state.get("verified") else "reserved")
                    ),
                    "assets": assets,
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
        if path == f"/api/v1/events/{event_id}/assets/{asset_id}/derivatives/":
            body = json.loads(request.content)
            state["derivative_manifest"] = body["objects"]
            state.setdefault("verified_derivatives", set())
            return httpx.Response(
                201,
                json={
                    "asset_id": str(asset_id),
                    "objects": [
                        {
                            "variant": value["variant"],
                            "state": (
                                "verified"
                                if value["variant"] in state["verified_derivatives"]
                                else "reserved"
                            ),
                            "failure_code": "",
                        }
                        for value in body["objects"]
                    ],
                },
            )
        if path == f"/api/v1/events/{event_id}/assets/{asset_id}/derivative-leases/":
            variants = json.loads(request.content)["variants"]
            objects = {value["variant"]: value for value in state["derivative_manifest"]}
            return httpx.Response(
                200,
                json={
                    "leases": [
                        {
                            "asset_id": str(asset_id),
                            "variant": variant,
                            "url": f"https://storage.invalid/{variant}",
                            "headers": {
                                "Content-Length": str(objects[variant]["size_bytes"]),
                                "Content-MD5": objects[variant]["content_md5"],
                                "Content-Type": "image/jpeg",
                                "If-None-Match": "*",
                                "x-amz-meta-openfotos-sha256": objects[variant]["sha256"],
                            },
                        }
                        for variant in variants
                    ]
                },
            )
        prefix = f"/api/v1/events/{event_id}/assets/{asset_id}/derivatives/"
        if path.startswith(prefix) and path.endswith("/complete/"):
            variant = path.removeprefix(prefix).removesuffix("/complete/")
            state.setdefault("verified_derivatives", set()).add(variant)
            return httpx.Response(
                200,
                json={"asset_id": str(asset_id), "variant": variant, "state": "verified"},
            )
        face_path = (
            f"/api/v1/events/{event_id}/sub-events/{_SUB_EVENT.id}/assets/{asset_id}/face-analysis/"
        )
        if path == face_path:
            body = json.loads(request.content)
            assert body["status"] == "no_usable_face"
            state["face_state"] = "no_usable_face"
            state["face_attempts"] = 1
            return httpx.Response(
                200,
                json={
                    "state": "no_usable_face",
                    "attempt_count": 1,
                    "failure_code": "",
                    "detected_face_count": 0,
                    "usable_face_count": 0,
                },
            )
        raise AssertionError(f"Unexpected API request: {request.method} {path}")

    return handle


def test_direct_upload_retries_then_resumes_at_the_verified_object_boundary(tmp_path: Path) -> None:
    store, event_id, batch_id, photo = approved_batch(tmp_path)
    asset_id = store.list_items(batch_id)[0].id
    state = {"storage_attempts": 0}
    sleeps = []

    def storage_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/original":
            return httpx.Response(200)
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
        face_engine_factory=NoFaceEngine,
        sleeper=sleeps.append,
        jitter=lambda _start, maximum: maximum,
    )
    events = service.sign_in_photographer(
        "http://localhost:8000",
        "primary",
        "password",
        "Primary workstation",
    )
    progress = []

    service.upload(
        batch_id,
        transfer_limit=1,
        on_progress=lambda done, total: progress.append((done, total)),
    )

    assert events[0].device_label == "Primary workstation"
    assert state["manifest"]["device_label"] == "Primary workstation"
    assert state["storage_attempts"] == 5
    assert sleeps == [1.0, 2.0, 4.0, 8.0]
    assert progress[-1] == (1, 1)
    assert state["clean_preview_confirmations"] == 1
    assert state["policy"]["enabled"] is False
    assert store.get_batch(batch_id).state is BatchState.COMPLETE
    checkpoint = store.get_upload_checkpoint(asset_id)
    assert checkpoint.state is LocalUploadState.VERIFIED
    assert checkpoint.attempt_count == 5
    service.close()
    store.close()


def test_sync_marks_a_published_in_flight_batch_not_included(tmp_path: Path) -> None:
    store, event_id, batch_id, _photo = approved_batch(tmp_path)
    asset_id = store.list_items(batch_id)[0].id
    store.mark_batch_reserved(batch_id)
    state = {
        "not_included": True,
        "policy": {
            "id": str(uuid4()),
            "enabled": False,
            "template": "compact-bottom-right",
            "text": "",
            "logo_kind": "none",
            "renderer_id": "watermark-raster-v1",
            "derivative_profile_id": "gallery-jpeg-v1",
            "mark_sha256": "",
        },
    }
    service = DesktopNetworkService(
        store,
        token_store=MemoryTokenStore(),
        face_engine_factory=NoFaceEngine,
        api_client=httpx.Client(
            transport=httpx.MockTransport(api_handler(event_id, batch_id, asset_id, state))
        ),
        storage_client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
    )
    service.sign_in_photographer(
        "http://localhost:8000", "photographer", "password", "Studio workstation"
    )

    service.upload(batch_id, transfer_limit=1, on_progress=lambda _done, _total: None)

    assert store.get_batch(batch_id).state is BatchState.NOT_INCLUDED
    assert store.get_upload_checkpoint(asset_id).state is LocalUploadState.EXCLUDED
    service.close()
    store.close()


def test_one_sync_generates_uploads_and_resumes_both_derivatives(tmp_path: Path) -> None:
    store, event_id, batch_id, _photo = approved_batch(tmp_path)
    asset_id = store.list_items(batch_id)[0].id
    policy = PreviewPolicyCache(
        id=uuid4(),
        enabled=False,
        template="compact-bottom-right",
        text="",
        logo_kind="none",
        renderer_id="watermark-raster-v1",
        derivative_profile_id="gallery-jpeg-v1",
        mark_sha256="",
    )
    state = {
        "policy_after_refresh": {
            "id": str(policy.id),
            "enabled": policy.enabled,
            "template": policy.template,
            "text": policy.text,
            "logo_kind": policy.logo_kind,
            "renderer_id": policy.renderer_id,
            "derivative_profile_id": policy.derivative_profile_id,
            "mark_sha256": policy.mark_sha256,
        }
    }
    uploads = []

    def storage_handler(request: httpx.Request) -> httpx.Response:
        content = request.read()
        uploads.append((request.url.path, content))
        assert request.headers["if-none-match"] == "*"
        return httpx.Response(200)

    service = DesktopNetworkService(
        store,
        token_store=MemoryTokenStore(),
        api_client=httpx.Client(
            transport=httpx.MockTransport(api_handler(event_id, batch_id, asset_id, state))
        ),
        storage_client=httpx.Client(transport=httpx.MockTransport(storage_handler)),
        face_engine_factory=NoFaceEngine,
    )
    service.sign_in_photographer(
        "http://localhost:8000", "photographer", "password", "Studio workstation"
    )
    stages = []
    service.upload(
        batch_id,
        transfer_limit=1,
        on_progress=lambda *_: None,
        on_stage=lambda stage, done, total: stages.append((stage, done, total)),
    )

    assert [path for path, _content in uploads] == [
        "/original",
        "/previews",
        "/thumbnails",
    ]
    assert all(content.startswith(b"\xff\xd8") for _path, content in uploads[1:])
    assert stages[-1] == ("face-index", 1, 1)
    assert state.get("clean_preview_confirmations") is None
    assert store.derivatives_complete(batch_id)
    assert store.face_analysis_complete(batch_id)
    assert not list(store.derivative_cache_directory(batch_id).iterdir())

    service.upload(batch_id, transfer_limit=1, on_progress=lambda *_: None)
    assert len(uploads) == 3
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
    service.sign_in_photographer(
        "http://localhost:8000", "photographer", "password", "Studio workstation"
    )
    photo.write_bytes(photo.read_bytes() + b"changed")

    with pytest.raises(SourceChangedError):
        service.upload(batch_id, transfer_limit=1, on_progress=lambda *_: None)

    assert storage_calls == []
    assert store.get_upload_checkpoint(asset_id).last_error_code == "source_changed"
    service.close()
    store.close()


def test_sign_out_revokes_the_server_session_and_forgets_the_refresh_token(
    tmp_path: Path,
) -> None:
    store, event_id, batch_id, _photo = approved_batch(tmp_path)
    asset_id = store.list_items(batch_id)[0].id
    state = {}
    token_store = MemoryTokenStore()
    service = DesktopNetworkService(
        store,
        token_store=token_store,
        face_engine_factory=NoFaceEngine,
        api_client=httpx.Client(
            transport=httpx.MockTransport(api_handler(event_id, batch_id, asset_id, state))
        ),
        storage_client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
    )
    service.sign_in_photographer(
        "http://localhost:8000", "photographer", "password", "Studio workstation"
    )
    assert token_store.tokens

    service.sign_out()

    assert state["revoked_refresh_token"] == "refresh-token"
    assert token_store.tokens == {}
    with pytest.raises(DesktopApiError) as failure:
        service.resume("http://localhost:8000")
    assert failure.value.code == "session_unavailable"
    service.close()
    store.close()


def test_saved_session_origin_requires_exactly_one_cached_origin_with_a_token(
    tmp_path: Path,
) -> None:
    store, event_id, batch_id, _photo = approved_batch(tmp_path)
    asset_id = store.list_items(batch_id)[0].id
    token_store = MemoryTokenStore()
    service = DesktopNetworkService(
        store,
        token_store=token_store,
        face_engine_factory=NoFaceEngine,
        api_client=httpx.Client(
            transport=httpx.MockTransport(api_handler(event_id, batch_id, asset_id, {}))
        ),
        storage_client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
    )

    assert service.saved_session_origin() is None
    token_store.tokens[("http://localhost:8000", store.installation_id)] = "refresh-token"
    assert service.saved_session_origin() == "http://localhost:8000"
    store.cache_event(
        EventCache(
            id=uuid4(),
            name="Second server event",
            storage_limit_bytes=50_000_000_000,
            processing_profile_id="pilot-profile-v1",
            server_url="https://other.example",
        )
    )
    assert service.saved_session_origin() is None
    service.close()
    store.close()


def test_sign_in_persists_the_workstation_label_for_later_resumes(tmp_path: Path) -> None:
    store, event_id, batch_id, _photo = approved_batch(tmp_path)
    asset_id = store.list_items(batch_id)[0].id
    service = DesktopNetworkService(
        store,
        token_store=MemoryTokenStore(),
        face_engine_factory=NoFaceEngine,
        api_client=httpx.Client(
            transport=httpx.MockTransport(api_handler(event_id, batch_id, asset_id, {}))
        ),
        storage_client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
    )

    service.sign_in_photographer("http://localhost:8000", "photographer", "password", "Yashas")

    assert store.workstation_label() == "Yashas"
    assert store.get_event(event_id).device_label == "Yashas"
    service.close()
    store.close()


def test_resume_applies_the_saved_workstation_label_to_server_events(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "checkpoint.sqlite3")
    event_id = uuid4()
    store.cache_event(
        EventCache(
            id=event_id,
            name="Reception",
            storage_limit_bytes=50_000_000_000,
            processing_profile_id="pilot-profile-v1",
            server_url="http://localhost:8000",
            sub_events=(_SUB_EVENT,),
        )
    )
    store.set_workstation_label("Yashas")
    token_store = MemoryTokenStore()
    token_store.tokens[("http://localhost:8000", store.installation_id)] = "refresh-token"
    service = DesktopNetworkService(
        store,
        token_store=token_store,
        face_engine_factory=NoFaceEngine,
        api_client=httpx.Client(
            transport=httpx.MockTransport(api_handler(event_id, uuid4(), uuid4(), {}))
        ),
        storage_client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
    )

    events = service.resume("http://localhost:8000")

    assert events[0].device_label == "Yashas"
    assert store.get_event(event_id).device_label == "Yashas"
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


def approved_multi_batch(tmp_path: Path, names: list[str]):
    photos = []
    for index, name in enumerate(names):
        photo = tmp_path / name
        Image.new("RGB", (8, 6), color=(40 + index, 80, 120)).save(photo, format="JPEG")
        photos.append(photo)
    store = CheckpointStore(tmp_path / "checkpoint.sqlite3")
    event_id = uuid4()
    store.cache_event(
        EventCache(
            id=event_id,
            name="Reception",
            storage_limit_bytes=50_000_000_000,
            processing_profile_id="pilot-profile-v1",
            server_url="http://localhost:8000",
            device_label="Studio workstation",
            sub_events=(_SUB_EVENT,),
        )
    )
    batch_id = store.create_batch(event_id, _SUB_EVENT.id, label="Edited originals")
    store.add_files(batch_id, photos)
    InventoryScanner(store).scan(batch_id)
    store.approve_batch(batch_id, supported_profile_id="pilot-profile-v1")
    return store, event_id, batch_id, photos


def multi_asset_state(asset_ids: list[UUID]) -> dict:
    return {
        "policy": {
            "id": str(uuid4()),
            "enabled": False,
            "template": "compact-bottom-right",
            "text": "",
            "logo_kind": "none",
            "renderer_id": "watermark-raster-v1",
            "derivative_profile_id": "gallery-jpeg-v1",
            "mark_sha256": "",
        },
        "assets": {
            str(asset_id): {
                "verified": False,
                "variants": set(),
                "derivative_manifest": [],
                "face_state": "pending",
                "face_attempts": 0,
            }
            for asset_id in asset_ids
        },
    }


def multi_asset_handler(event_id: UUID, batch_id: UUID, state: dict):
    def event_data():
        return {
            "id": str(event_id),
            "name": "Reception",
            "state": "uploading",
            "storage_limit_bytes": 50_000_000_000,
            "processing_profile_id": "pilot-profile-v1",
            "face_model_id": "opencv-yunet-2023mar-sface-2021dec",
            "face_index_ready": False,
            "sub_events": [
                {
                    "id": str(_SUB_EVENT.id),
                    "name": _SUB_EVENT.name,
                    "position": _SUB_EVENT.position,
                }
            ],
            "reserved_original_bytes": 0,
            "verified_original_bytes": 0,
            "remaining_original_bytes": 50_000_000_000,
            "intake_state": "open",
            "intake_generation": 1,
            "max_contribution_devices": 10,
            "active_contribution_devices": 1,
            "device_label": "",
            "preview_policy": state.get("policy"),
        }

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/auth/login/":
            return httpx.Response(
                200,
                json={
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "events": [event_data()],
                },
            )
        if path == "/api/v1/events/":
            return httpx.Response(200, json={"events": [event_data()]})
        if path == f"/api/v1/events/{event_id}/batches/":
            state["manifest"] = json.loads(request.content)
            return httpx.Response(201, json={"id": str(batch_id), "state": "reserved"})
        if path == f"/api/v1/events/{event_id}/batches/{batch_id}/":
            assets = []
            for asset_id, asset in state["assets"].items():
                assets.append(
                    {
                        "asset_id": asset_id,
                        "variant": "originals",
                        "state": "verified" if asset["verified"] else "reserved",
                        "failure_code": "",
                        "gallery_excluded": False,
                        "face_analysis": {
                            "state": asset["face_state"],
                            "attempt_count": asset["face_attempts"],
                            "failure_code": "",
                            "detected_face_count": 0,
                            "usable_face_count": 0,
                        },
                    }
                )
                assets.extend(
                    {
                        "asset_id": asset_id,
                        "variant": value["variant"],
                        "state": (
                            "verified" if value["variant"] in asset["variants"] else "reserved"
                        ),
                        "failure_code": "",
                        "gallery_excluded": False,
                        "attempt_count": asset.get("derivative_attempts", 0),
                    }
                    for value in asset["derivative_manifest"]
                )
            complete = all(asset["verified"] for asset in state["assets"].values())
            return httpx.Response(
                200,
                json={
                    "id": str(batch_id),
                    "sub_event_id": str(_SUB_EVENT.id),
                    "state": "complete" if complete else "reserved",
                    "assets": assets,
                },
            )
        if path == f"/api/v1/events/{event_id}/batches/{batch_id}/upload-leases/":
            requested = json.loads(request.content)["asset_ids"]
            manifest_assets = {value["id"]: value for value in state["manifest"]["assets"]}
            return httpx.Response(
                200,
                json={
                    "leases": [
                        {
                            "asset_id": asset_id,
                            "url": f"https://storage.invalid/original/{asset_id}",
                            "headers": {
                                "Content-Length": str(manifest_assets[asset_id]["size_bytes"]),
                                "Content-MD5": manifest_assets[asset_id]["content_md5"],
                                "Content-Type": "image/jpeg",
                                "If-None-Match": "*",
                                "x-amz-meta-openfotos-sha256": manifest_assets[asset_id]["sha256"],
                            },
                        }
                        for asset_id in requested
                    ]
                },
            )
        for asset_id, asset in state["assets"].items():
            asset_prefix = f"/api/v1/events/{event_id}/assets/{asset_id}"
            if path == f"{asset_prefix}/complete/":
                asset["verified"] = True
                return httpx.Response(200, json={"asset_id": asset_id, "state": "verified"})
            if path == f"{asset_prefix}/derivatives/":
                if state.get("derivative_error") is not None:
                    return state["derivative_error"]
                body = json.loads(request.content)
                asset["derivative_manifest"] = body["objects"]
                return httpx.Response(
                    201,
                    json={
                        "asset_id": asset_id,
                        "objects": [
                            {
                                "variant": value["variant"],
                                "state": (
                                    "verified"
                                    if value["variant"] in asset["variants"]
                                    else "reserved"
                                ),
                                "failure_code": "",
                            }
                            for value in body["objects"]
                        ],
                    },
                )
            if path == f"{asset_prefix}/derivative-leases/":
                variants = json.loads(request.content)["variants"]
                objects = {value["variant"]: value for value in asset["derivative_manifest"]}
                return httpx.Response(
                    200,
                    json={
                        "leases": [
                            {
                                "asset_id": asset_id,
                                "variant": variant,
                                "url": f"https://storage.invalid/{variant}/{asset_id}",
                                "headers": {
                                    "Content-Length": str(objects[variant]["size_bytes"]),
                                    "Content-MD5": objects[variant]["content_md5"],
                                    "Content-Type": "image/jpeg",
                                    "If-None-Match": "*",
                                    "x-amz-meta-openfotos-sha256": objects[variant]["sha256"],
                                },
                            }
                            for variant in variants
                        ]
                    },
                )
            derivative_prefix = f"{asset_prefix}/derivatives/"
            if path.startswith(derivative_prefix) and path.endswith("/complete/"):
                variant = path.removeprefix(derivative_prefix).removesuffix("/complete/")
                asset["variants"].add(variant)
                return httpx.Response(
                    200,
                    json={"asset_id": asset_id, "variant": variant, "state": "verified"},
                )
            face_path = (
                f"/api/v1/events/{event_id}/sub-events/{_SUB_EVENT.id}"
                f"/assets/{asset_id}/face-analysis/"
            )
            if path == face_path:
                asset["face_state"] = "no_usable_face"
                asset["face_attempts"] += 1
                return httpx.Response(
                    200,
                    json={
                        "state": "no_usable_face",
                        "attempt_count": asset["face_attempts"],
                        "failure_code": "",
                        "detected_face_count": 0,
                        "usable_face_count": 0,
                    },
                )
        raise AssertionError(f"Unexpected API request: {request.method} {path}")

    return handle


def test_face_pool_embeds_multiple_photos_concurrently(tmp_path: Path) -> None:
    store, event_id, batch_id, _photos = approved_multi_batch(tmp_path, ["a.jpg", "b.jpg", "c.jpg"])
    items = store.list_items(batch_id)
    asset_ids = [item.id for item in items]
    state = multi_asset_state(asset_ids)
    for asset in state["assets"].values():
        asset["verified"] = True
    # Pre-verify every original so the first face wave spans all three photos.
    store.mark_batch_reserved(batch_id)
    for item in items:
        store.mark_upload_verified(item.id)
    barrier = threading.Barrier(3, timeout=10)
    engine_count = 0

    class PooledEngine:
        model = ACCEPTED_FACE_MODEL_CONTRACT.model
        runtime_versions = {"synthetic": "1"}

        def detect_and_embed(self, image_bytes: bytes):
            assert image_bytes
            barrier.wait()
            return ()

    def factory() -> PooledEngine:
        nonlocal engine_count
        engine_count += 1
        return PooledEngine()

    uploads = []
    service = DesktopNetworkService(
        store,
        token_store=MemoryTokenStore(),
        api_client=httpx.Client(
            transport=httpx.MockTransport(multi_asset_handler(event_id, batch_id, state))
        ),
        storage_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: uploads.append(request.url.path) or httpx.Response(200)
            )
        ),
        face_engine_factory=factory,
        derivative_workers=3,
        face_workers=3,
    )
    service.sign_in_photographer(
        "http://localhost:8000", "photographer", "password", "Studio workstation"
    )

    service.upload(batch_id, transfer_limit=2, on_progress=lambda *_: None)

    # A serial face stage would stall on the barrier; three workers pass it together.
    assert engine_count == 3
    assert store.face_analysis_complete(batch_id)
    assert store.derivatives_complete(batch_id)
    preview_uploads = [path for path in uploads if path.startswith("/previews/")]
    assert len(preview_uploads) == 3
    service.close()
    store.close()


def test_derivatives_start_while_later_originals_still_upload(tmp_path: Path) -> None:
    store, event_id, batch_id, _photos = approved_multi_batch(tmp_path, ["a.jpg", "b.jpg"])
    by_name = {item.basename: item.id for item in store.list_items(batch_id)}
    a_id, b_id = by_name["a.jpg"], by_name["b.jpg"]
    state = multi_asset_state([a_id, b_id])
    a_preview_uploaded = threading.Event()
    order = []

    def storage_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/original/{b_id}":
            order.append("b-original-start")
            a_preview_uploaded.wait(timeout=10)
            order.append("b-original-end")
            return httpx.Response(200)
        if path == f"/previews/{a_id}":
            a_preview_uploaded.set()
            order.append("a-preview-end")
            return httpx.Response(200)
        return httpx.Response(200)

    service = DesktopNetworkService(
        store,
        token_store=MemoryTokenStore(),
        api_client=httpx.Client(
            transport=httpx.MockTransport(multi_asset_handler(event_id, batch_id, state))
        ),
        storage_client=httpx.Client(transport=httpx.MockTransport(storage_handler)),
        face_engine_factory=NoFaceEngine,
    )
    service.sign_in_photographer(
        "http://localhost:8000", "photographer", "password", "Studio workstation"
    )

    service.upload(batch_id, transfer_limit=2, on_progress=lambda *_: None)

    assert "a-preview-end" in order
    assert order.index("a-preview-end") < order.index("b-original-end")
    assert store.face_analysis_complete(batch_id)
    service.close()
    store.close()


def test_missing_face_models_do_not_mask_an_originals_stage_error(
    tmp_path: Path,
) -> None:
    """The engine pool is built lazily: with no downloaded models, an earlier originals
    failure must still surface instead of a face-model setup error."""
    store, event_id, batch_id, photo = approved_batch(tmp_path)
    asset_id = store.list_items(batch_id)[0].id

    def missing_models():
        raise FaceModelSetupError(
            "face_model_setup_required",
            "The accepted face models have not finished downloading. Retry the download.",
        )

    service = DesktopNetworkService(
        store,
        token_store=MemoryTokenStore(),
        api_client=httpx.Client(
            transport=httpx.MockTransport(api_handler(event_id, batch_id, asset_id, {}))
        ),
        storage_client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
        face_engine_factory=missing_models,
    )
    service.sign_in_photographer(
        "http://localhost:8000", "photographer", "password", "Studio workstation"
    )
    photo.write_bytes(photo.read_bytes() + b"changed")

    with pytest.raises(SourceChangedError):
        service.upload(batch_id, transfer_limit=1, on_progress=lambda *_: None)

    service.close()
    store.close()


def test_fatal_derivative_error_aborts_the_pipeline(tmp_path: Path) -> None:
    store, event_id, batch_id, _photos = approved_multi_batch(tmp_path, ["a.jpg"])
    [item] = store.list_items(batch_id)
    state = multi_asset_state([item.id])
    state["derivative_error"] = httpx.Response(
        409,
        json={
            "error": {
                "code": "derivative_profile_mismatch",
                "message": "The event gallery profile changed.",
            }
        },
    )
    service = DesktopNetworkService(
        store,
        token_store=MemoryTokenStore(),
        api_client=httpx.Client(
            transport=httpx.MockTransport(multi_asset_handler(event_id, batch_id, state))
        ),
        storage_client=httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200))
        ),
        face_engine_factory=NoFaceEngine,
    )
    service.sign_in_photographer(
        "http://localhost:8000", "photographer", "password", "Studio workstation"
    )

    with pytest.raises(DesktopApiError) as failure:
        service.upload(batch_id, transfer_limit=1, on_progress=lambda *_: None)

    assert failure.value.code == "derivative_profile_mismatch"
    checkpoint = store.get_derivative_checkpoint(item.id, "previews")
    assert checkpoint.last_error_code == "derivative_profile_mismatch"
    service.close()
    store.close()
