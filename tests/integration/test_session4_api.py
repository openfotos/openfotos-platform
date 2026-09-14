import base64
import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from openfotos_server.events import api
from openfotos_server.events.models import (
    DesktopSession,
    Event,
    IdempotencyRecord,
    Photographer,
    PhotographerMembership,
    SubEvent,
)
from openfotos_storage.backend import ObjectAlreadyExists, ObjectHead, PresignedPut

pytestmark = pytest.mark.django_db


class MemoryObjectStore:
    def __init__(self) -> None:
        self.objects = {}

    def presign_put(
        self,
        *,
        key,
        content_length,
        content_md5,
        sha256,
        expires_in_seconds,
    ):
        return PresignedPut(
            url=f"https://storage.invalid/{key}",
            headers={
                "Content-Length": str(content_length),
                "Content-MD5": content_md5,
                "Content-Type": "image/jpeg",
                "If-None-Match": "*",
                "x-amz-meta-openfotos-sha256": sha256,
            },
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
        )

    def upload(self, key, content, *, sha256=None, content_type="image/jpeg"):
        self.objects[key] = (
            content,
            content_type,
            {"openfotos-sha256": sha256 or hashlib.sha256(content).hexdigest()},
        )

    def head(self, key):
        stored = self.objects.get(key)
        if stored is None:
            return None
        content, content_type, metadata = stored
        return ObjectHead(
            key=key,
            content_length=len(content),
            content_type=content_type,
            etag=hashlib.md5(content, usedforsecurity=False).hexdigest(),
            metadata=metadata,
        )

    def delete(self, key):
        self.objects.pop(key, None)

    def put_immutable(
        self,
        *,
        key,
        body,
        content_length,
        content_md5,
        sha256,
        content_type,
    ):
        del content_length, content_md5
        if key in self.objects:
            raise ObjectAlreadyExists
        self.upload(key, bytes(body), sha256=sha256, content_type=content_type)


@pytest.fixture
def tenant():
    photographer = Photographer.objects.create(slug="alpha", display_name="Alpha Photos")
    user = get_user_model().objects.create_user(username="primary", password="correct-password")
    PhotographerMembership.objects.create(photographer=photographer, user=user)
    event = Event.objects.create(
        photographer=photographer,
        name="Reception",
        expires_at=timezone.now() + timedelta(days=30),
    )
    SubEvent.objects.create(event=event, name="Reception", position=1)
    return photographer, user, event


def post_json(client, url, data, *, token=None, idempotency_key=None, host="alpha.localhost"):
    headers = {"host": host}
    if token:
        headers["authorization"] = f"Bearer {token}"
    if idempotency_key:
        headers["idempotency-key"] = str(idempotency_key)
    return client.post(url, data=data, content_type="application/json", headers=headers)


def login(client, *, installation_id=None):
    response = post_json(
        client,
        reverse("desktop-api:login"),
        {
            "username": "primary",
            "password": "correct-password",
            "installation_id": str(installation_id or uuid4()),
        },
    )
    assert response.status_code == 200, response.json()
    return response.json()


def manifest(content: bytes, *, batch_id=None, asset_id=None):
    sub_event = SubEvent.objects.get(is_archived=False)
    return {
        "batch_id": str(batch_id or uuid4()),
        "sub_event_id": str(sub_event.id),
        "label": "Editor export",
        "processing_profile_id": "pilot-profile-v1",
        "device_label": "Installation workstation",
        "assets": [
            {
                "id": str(asset_id or uuid4()),
                "filename": "photo.jpg",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "content_md5": base64.b64encode(
                    hashlib.md5(content, usedforsecurity=False).digest()
                ).decode(),
                "width": 8,
                "height": 6,
            }
        ],
    }


def test_desktop_login_is_tenant_scoped_and_contract_is_strict(tenant) -> None:
    _, _, event = tenant
    client = Client()

    signed_in = login(client)

    assert [item["id"] for item in signed_in["events"]] == [str(event.id)]
    wrong_host = client.get(
        reverse("desktop-api:events"),
        headers={"host": "localhost", "authorization": f"Bearer {signed_in['access_token']}"},
    )
    assert wrong_host.status_code == 404
    invalid = post_json(
        client,
        reverse("desktop-api:login"),
        {
            "username": "primary",
            "password": "correct-password",
            "installation_id": str(uuid4()),
            "unexpected": True,
        },
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_request"

    attempted_manifest = manifest(b"synthetic jpeg")
    attempted_manifest["assets"][0]["object_key"] = "events/another-event/originals/chosen.jpg"
    chosen_key = post_json(
        client,
        reverse("desktop-api:reserve-batch", args=(event.id,)),
        attempted_manifest,
        token=signed_in["access_token"],
        idempotency_key=uuid4(),
    )
    assert chosen_key.status_code == 400
    assert chosen_key.json()["error"]["code"] == "invalid_request"


def test_photographer_installations_share_event_visibility_and_idempotency_is_strict(
    tenant,
) -> None:
    _, _, event = tenant
    client = Client()
    first_token = login(client)["access_token"]
    second_token = login(client)["access_token"]
    content = b"synthetic jpeg"
    payload = manifest(content)
    idempotency_key = uuid4()

    reserved = post_json(
        client,
        reverse("desktop-api:reserve-batch", args=(event.id,)),
        payload,
        token=first_token,
        idempotency_key=idempotency_key,
    )
    replayed = post_json(
        client,
        reverse("desktop-api:reserve-batch", args=(event.id,)),
        payload,
        token=first_token,
        idempotency_key=idempotency_key,
    )
    assert reserved.status_code == replayed.status_code == 201
    assert reserved.json() == replayed.json()

    changed = {**payload, "label": "Changed request"}
    conflict = post_json(
        client,
        reverse("desktop-api:reserve-batch", args=(event.id,)),
        changed,
        token=first_token,
        idempotency_key=idempotency_key,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"

    private = client.get(
        reverse("desktop-api:batch-detail", args=(event.id, payload["batch_id"])),
        headers={"host": "alpha.localhost", "authorization": f"Bearer {second_token}"},
    )
    assert private.status_code == 200
    assert private.json()["id"] == payload["batch_id"]


def test_pending_idempotency_claim_blocks_a_duplicate_before_mutation(tenant) -> None:
    _, user, event = tenant
    client = Client()
    primary = login(client)
    session = DesktopSession.objects.get(user=user)
    idempotency_key = uuid4()
    IdempotencyRecord.objects.create(
        actor_key=str(session.id),
        key=idempotency_key,
        operation="close_intake",
        request_sha256=hashlib.sha256(b"{}").hexdigest(),
        response_status=0,
        response_body={},
        expires_at=timezone.now() + timedelta(minutes=5),
    )

    response = client.post(
        f"/api/v1/events/{event.id}/intake/close/",
        data=b"{}",
        content_type="application/json",
        headers={
            "host": "alpha.localhost",
            "authorization": f"Bearer {primary['access_token']}",
            "idempotency-key": str(idempotency_key),
        },
    )

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "idempotency_in_progress",
        "message": "The matching request is still in progress; retry it shortly.",
        "retryable": True,
    }
    event.refresh_from_db()
    assert event.intake_state == "open"


def test_failed_mutation_releases_its_idempotency_claim(tenant) -> None:
    _, user, event = tenant
    client = Client()
    primary = login(client)
    session = DesktopSession.objects.get(user=user)
    idempotency_key = uuid4()

    invalid = post_json(
        client,
        reverse("desktop-api:reserve-batch", args=(event.id,)),
        {"unexpected": True},
        token=primary["access_token"],
        idempotency_key=idempotency_key,
    )
    assert invalid.status_code == 400
    assert not IdempotencyRecord.objects.filter(
        actor_key=str(session.id), key=idempotency_key
    ).exists()

    reserved = post_json(
        client,
        reverse("desktop-api:reserve-batch", args=(event.id,)),
        manifest(b"synthetic jpeg"),
        token=primary["access_token"],
        idempotency_key=idempotency_key,
    )
    assert reserved.status_code == 201, reserved.json()


def test_api_upload_recovery_close_and_finalize(monkeypatch, tenant) -> None:
    _, _, event = tenant
    client = Client()
    storage = MemoryObjectStore()
    monkeypatch.setattr(api, "configured_object_store", lambda: storage)
    photographer = login(client)
    installation_token = photographer["access_token"]
    content = b"synthetic jpeg bytes"
    payload = manifest(content)
    reserve = post_json(
        client,
        reverse("desktop-api:reserve-batch", args=(event.id,)),
        payload,
        token=installation_token,
        idempotency_key=uuid4(),
    )
    assert reserve.status_code == 201, reserve.json()
    lease_response = post_json(
        client,
        reverse("desktop-api:upload-leases", args=(event.id, payload["batch_id"])),
        {"asset_ids": [payload["assets"][0]["id"]]},
        token=installation_token,
    )
    [lease] = lease_response.json()["leases"]
    key = urlparse(lease["url"]).path.lstrip("/")
    storage.upload(key, content)

    completed = post_json(
        client,
        reverse("desktop-api:complete-asset", args=(event.id, payload["assets"][0]["id"])),
        {},
        token=installation_token,
        idempotency_key=uuid4(),
    )
    assert completed.status_code == 200, completed.json()
    closed = post_json(
        client,
        f"/api/v1/events/{event.id}/intake/close/",
        {},
        token=installation_token,
        idempotency_key=uuid4(),
    )
    assert closed.status_code == 200
    finalized = post_json(
        client,
        reverse("desktop-api:finalize", args=(event.id,)),
        {},
        token=photographer["access_token"],
        idempotency_key=uuid4(),
    )
    assert finalized.status_code == 200, finalized.json()
    assert finalized.json()["asset_count"] == 1
