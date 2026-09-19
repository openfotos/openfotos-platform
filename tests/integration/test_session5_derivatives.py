import base64
import hashlib
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from openfotos_contracts import (
    DERIVATIVE_PROFILE_ID,
    AssetDerivativesInput,
    AssetVariant,
    ContributionInput,
    EventState,
    PreviewPolicyInput,
)
from openfotos_server.events.derivative_services import (
    confirm_preview_policy,
    issue_derivative_leases,
    issue_owned_original_url,
    normalize_mark_png,
    refresh_derivative_readiness,
    register_asset_derivatives,
    verify_derivative,
)
from openfotos_server.events.desktop_auth import (
    authenticate_photographer,
)
from openfotos_server.events.ingestion_services import (
    IngestionError,
    close_intake,
    finalize_ingestion,
    issue_upload_leases,
    reserve_contribution,
    verify_uploaded_object,
)
from openfotos_server.events.models import (
    Asset,
    AssetObject,
    Event,
    Photographer,
    PhotographerMembership,
    SubEvent,
)
from openfotos_storage import PresignedGet
from openfotos_storage.backend import ObjectAlreadyExists, ObjectHead, PresignedPut

pytestmark = pytest.mark.django_db


class MemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str, dict[str, str]]] = {}

    def presign_put(
        self, *, key, content_length, content_md5, sha256, content_type, expires_in_seconds
    ):
        del content_length, content_md5, content_type
        return PresignedPut(
            url=f"https://storage.invalid/{key}",
            headers={"x-amz-meta-openfotos-sha256": sha256},
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
        )

    def presign_get(self, *, key, expires_in_seconds, content_disposition="inline"):
        del content_disposition
        return PresignedGet(
            url=f"https://storage.invalid/{key}",
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
        )

    def put_immutable(
        self, *, key, body, content_length, content_md5, sha256, content_type
    ) -> None:
        del content_length, content_md5
        if key in self.objects:
            raise ObjectAlreadyExists
        self.upload(key, bytes(body), sha256=sha256, content_type=content_type)

    def upload(self, key, content, *, sha256=None, content_type="image/jpeg") -> None:
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


def _md5(content: bytes) -> str:
    return base64.b64encode(hashlib.md5(content, usedforsecurity=False).digest()).decode()


def _mark() -> str:
    output = BytesIO()
    Image.new("RGBA", (120, 40), (250, 250, 250, 220)).save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode()


def _setup():
    photographer = Photographer.objects.create(slug="alpha", display_name="Alpha Photos")
    user = get_user_model().objects.create_user(
        username="photographer",
        password="correct-password",
    )
    PhotographerMembership.objects.create(photographer=photographer, user=user)
    event = Event.objects.create(
        photographer=photographer,
        name="Reception",
        expires_at=timezone.now() + timedelta(days=30),
    )
    SubEvent.objects.create(event=event, name="Reception", position=1)
    first = authenticate_photographer(
        photographer=photographer,
        username="photographer",
        password="correct-password",
        installation_id=uuid4(),
    ).session
    second = authenticate_photographer(
        photographer=photographer,
        username="photographer",
        password="correct-password",
        installation_id=uuid4(),
    ).session
    return event, first, second


def _verified_original(event, installation, storage, *, content=b"original jpeg bytes"):
    asset_id = uuid4()
    sub_event = SubEvent.objects.get(event=event, is_archived=False)
    contribution = ContributionInput.from_dict(
        {
            "batch_id": str(uuid4()),
            "sub_event_id": str(sub_event.id),
            "label": "Camera one",
            "processing_profile_id": "pilot-profile-v1",
            "device_label": "Installation",
            "assets": [
                {
                    "id": str(asset_id),
                    "filename": "IMG_10.jpg",
                    "content_type": "image/jpeg",
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "content_md5": _md5(content),
                    "width": 8,
                    "height": 6,
                }
            ],
        }
    )
    batch = reserve_contribution(
        session=installation,
        event_id=event.id,
        contribution=contribution,
    )
    [lease] = issue_upload_leases(
        session=installation,
        event_id=event.id,
        batch_id=batch.id,
        object_store=storage,
    )
    original = AssetObject.objects.get(asset_id=lease["asset_id"], variant="originals")
    storage.upload(original.object_key, content)
    verify_uploaded_object(
        session=installation,
        event_id=event.id,
        asset_id=asset_id,
        object_store=storage,
    )
    return Asset.objects.get(pk=asset_id), content


def _derivative_input(asset, policy):
    preview = b"preview jpeg"
    thumbnail = b"thumbnail jpeg"
    return (
        AssetDerivativesInput.from_dict(
            {
                "asset_id": str(asset.id),
                "source_sha256": asset.sha256,
                "policy_id": str(policy.id),
                "profile_id": DERIVATIVE_PROFILE_ID,
                "captured_at": "2026-09-12T10:20:30",
                "objects": [
                    {
                        "variant": "previews",
                        "size_bytes": len(preview),
                        "sha256": hashlib.sha256(preview).hexdigest(),
                        "content_md5": _md5(preview),
                        "width": 8,
                        "height": 6,
                    },
                    {
                        "variant": "thumbnails",
                        "size_bytes": len(thumbnail),
                        "sha256": hashlib.sha256(thumbnail).hexdigest(),
                        "content_md5": _md5(thumbnail),
                        "width": 8,
                        "height": 6,
                    },
                ],
            }
        ),
        {AssetVariant.PREVIEW: preview, AssetVariant.THUMBNAIL: thumbnail},
    )


def test_derivatives_are_owned_verified_and_make_current_generation_ready() -> None:
    event, primary, installation = _setup()
    storage = MemoryObjectStore()
    policy = confirm_preview_policy(
        session=primary,
        event_id=event.id,
        value=PreviewPolicyInput.from_dict(
            {
                "enabled": True,
                "template": "compact-bottom-right",
                "text": "",
                "logo_kind": "onenodeai",
                "mark_png_base64": _mark(),
            }
        ),
        object_store=storage,
    )
    asset, original_content = _verified_original(event, installation, storage)
    value, contents = _derivative_input(asset, policy)
    registered = register_asset_derivatives(session=installation, event_id=event.id, value=value)
    assert {item.variant for item in registered} == {"previews", "thumbnails"}

    close_intake(session=primary, event_id=event.id)
    finalize_ingestion(session=primary, event_id=event.id, object_store=storage)
    assert not refresh_derivative_readiness(event.id)

    for variant, content in contents.items():
        [lease] = issue_derivative_leases(
            session=installation,
            event_id=event.id,
            asset_id=asset.id,
            variants=(variant,),
            object_store=storage,
        )
        derivative = AssetObject.objects.get(asset=asset, variant=variant.value)
        storage.upload(derivative.object_key, content)
        verified = verify_derivative(
            session=installation,
            event_id=event.id,
            asset_id=asset.id,
            variant=variant,
            object_store=storage,
        )
        assert verified.state == "verified"
        assert lease["variant"] == variant.value

    event.refresh_from_db()
    asset.refresh_from_db()
    original = AssetObject.objects.get(asset=asset, variant="originals")
    assert event.state == EventState.REVIEW.value
    assert event.derivatives_ready_generation == event.intake_generation
    assert asset.gallery_position == 1
    assert storage.objects[original.object_key][0] == original_content


def test_policy_is_immutable_and_derivative_access_is_installation_scoped() -> None:
    event, primary, installation = _setup()
    storage = MemoryObjectStore()
    asset, _ = _verified_original(event, installation, storage)
    value = PreviewPolicyInput.from_dict(
        {
            "enabled": False,
            "template": "compact-bottom-right",
            "text": "",
            "logo_kind": "none",
            "mark_png_base64": "",
        }
    )
    policy = confirm_preview_policy(
        session=installation,
        event_id=event.id,
        value=value,
        object_store=storage,
    )
    with pytest.raises(IngestionError) as immutable_policy:
        confirm_preview_policy(
            session=primary,
            event_id=event.id,
            value=PreviewPolicyInput.from_dict(
                {
                    "enabled": True,
                    "template": "bottom-center",
                    "text": "Different",
                    "logo_kind": "none",
                    "mark_png_base64": _mark(),
                }
            ),
            object_store=storage,
        )
    assert immutable_policy.value.code == "preview_policy_already_confirmed"

    other = authenticate_photographer(
        photographer=event.photographer,
        username="photographer",
        password="correct-password",
        installation_id=uuid4(),
    )
    derivative_value, _ = _derivative_input(asset, policy)
    with pytest.raises(IngestionError) as private:
        register_asset_derivatives(
            session=other.session,
            event_id=event.id,
            value=derivative_value,
        )
    assert private.value.code == "asset_not_found"
    with pytest.raises(IngestionError) as private_source:
        issue_owned_original_url(
            session=other.session,
            event_id=event.id,
            asset_id=asset.id,
            object_store=storage,
        )
    assert private_source.value.code == "asset_not_found"


def test_finalization_reconciles_derivatives_that_finished_while_intake_was_open() -> None:
    event, primary, installation = _setup()
    storage = MemoryObjectStore()
    asset, _ = _verified_original(event, installation, storage)
    policy = confirm_preview_policy(
        session=primary,
        event_id=event.id,
        value=PreviewPolicyInput.from_dict(
            {
                "enabled": False,
                "template": "compact-bottom-right",
                "text": "",
                "logo_kind": "none",
                "mark_png_base64": "",
            }
        ),
        object_store=None,
    )
    value, contents = _derivative_input(asset, policy)
    register_asset_derivatives(session=installation, event_id=event.id, value=value)
    for variant, content in contents.items():
        derivative = AssetObject.objects.get(asset=asset, variant=variant.value)
        storage.upload(derivative.object_key, content)
        verify_derivative(
            session=installation,
            event_id=event.id,
            asset_id=asset.id,
            variant=variant,
            object_store=storage,
        )
    event.refresh_from_db()
    assert event.state != EventState.REVIEW.value

    close_intake(session=primary, event_id=event.id)
    finalize_ingestion(session=primary, event_id=event.id, object_store=storage)

    event.refresh_from_db()
    assert event.state == EventState.REVIEW.value
    assert event.derivatives_ready_generation == event.intake_generation


def test_disabled_policy_api_accepts_photographer_without_object_storage() -> None:
    event, _, _ = _setup()
    photographer_tokens = authenticate_photographer(
        photographer=event.photographer,
        username="photographer",
        password="correct-password",
        installation_id=uuid4(),
    )
    client = Client()
    url = reverse("desktop-api:confirm-preview-policy", args=(event.id,))
    body = {
        "enabled": False,
        "template": "compact-bottom-right",
        "text": "",
        "logo_kind": "none",
        "mark_png_base64": "",
    }
    confirmed = client.post(
        url,
        body,
        content_type="application/json",
        headers={
            "host": "alpha.localhost",
            "authorization": f"Bearer {photographer_tokens.access_token}",
            "idempotency-key": str(uuid4()),
        },
    )
    assert confirmed.status_code == 201, confirmed.json()
    assert confirmed.json()["preview_policy"]["enabled"] is False
    assert event.audit_events.filter(action="preview_policy.confirmed").count() == 1


def test_server_rejects_fully_transparent_marks_even_with_colored_rgb() -> None:
    output = BytesIO()
    Image.new("RGBA", (40, 20), (200, 10, 30, 0)).save(output, format="PNG")

    with pytest.raises(IngestionError) as invalid:
        normalize_mark_png(output.getvalue())
    assert invalid.value.code == "invalid_watermark_image"
