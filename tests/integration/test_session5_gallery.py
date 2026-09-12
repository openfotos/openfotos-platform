import base64
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from openfotos_contracts import EventState, IngestionManifestState, UploadObjectState
from openfotos_server.events import views
from openfotos_server.events.cookies import visitor_cookie_name
from openfotos_server.events.gallery_services import (
    exclude_from_gallery,
    gallery_page,
    restore_to_gallery,
)
from openfotos_server.events.ingestion_services import IngestionError
from openfotos_server.events.models import (
    Asset,
    AssetObject,
    AuditAction,
    ContributionBatch,
    Event,
    IngestionManifest,
    Photographer,
    PhotographerMembership,
    PreviewPolicy,
    UploaderDevice,
)
from openfotos_storage import PresignedGet

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def configure_test_static_files(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
    settings.MIDDLEWARE = [
        middleware
        for middleware in settings.MIDDLEWARE
        if middleware != "whitenoise.middleware.WhiteNoiseMiddleware"
    ]


class SigningStore:
    def presign_get(self, *, key, expires_in_seconds, content_disposition="inline"):
        del content_disposition
        return PresignedGet(
            url=f"https://storage.invalid/{key}?expires={expires_in_seconds}",
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
        )


def _md5(content: bytes) -> str:
    return base64.b64encode(hashlib.md5(content, usedforsecurity=False).digest()).decode()


def _tenant(slug="alpha"):
    photographer = Photographer.objects.create(
        slug=slug,
        display_name=f"{slug.title()} Photos",
    )
    user = get_user_model().objects.create_user(
        username=f"{slug}-lead", password="correct-password"
    )
    PhotographerMembership.objects.create(photographer=photographer, user=user)
    event = Event.objects.create(
        photographer=photographer,
        name="Private Reception",
        expires_at=timezone.now() + timedelta(days=10),
        state=EventState.PROCESSING.value,
        intake_state="closed",
    )
    event.set_pin("123456")
    event.save(update_fields=("pin_hash",))
    device = UploaderDevice.objects.create(
        event=event,
        installation_id=uuid4(),
        label="Lead",
        role="lead",
    )
    batch = ContributionBatch.objects.create(
        id=uuid4(),
        device=device,
        intake_generation=1,
        state="complete",
        label="Complete",
        processing_profile_id="pilot-profile-v1",
        declared_asset_count=1,
        declared_original_bytes=100,
        manifest_sha256="0" * 64,
    )
    manifest = IngestionManifest.objects.create(
        event=event,
        generation=1,
        state=IngestionManifestState.COMMITTED.value,
        object_key=f"events/{event.id}/manifests/generation-000001.json",
        content_sha256="1" * 64,
        document={},
        asset_count=1,
        original_bytes=100,
        committed_at=timezone.now(),
    )
    event.current_ingestion_manifest = manifest
    event.save(update_fields=("current_ingestion_manifest",))
    PreviewPolicy.objects.create(event=event, enabled=False, confirmed_by=user)
    return photographer, user, event, batch


def _gallery_asset(event, batch, *, position=1, filename="client-private-name.jpg"):
    content = b"synthetic"
    asset = Asset.objects.create(
        id=uuid4(),
        batch=batch,
        original_filename=filename,
        width=800,
        height=600,
        sha256=hashlib.sha256(content).hexdigest(),
        gallery_position=position,
    )
    for variant, width, height in (
        ("originals", 800, 600),
        ("previews", 800, 600),
        ("thumbnails", 512, 384),
    ):
        AssetObject.objects.create(
            asset=asset,
            variant=variant,
            object_key=f"events/{event.id}/{variant}/{asset.id}.jpg",
            expected_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            content_md5=_md5(content),
            width=width,
            height=height,
            state=UploadObjectState.VERIFIED.value,
            verified_at=timezone.now(),
        )
    return asset


def test_dashboard_publishes_and_visitor_gets_only_authorized_signed_variants(monkeypatch) -> None:
    photographer, user, event, batch = _tenant()
    asset = _gallery_asset(event, batch)
    event.derivatives_ready_generation = 1
    event.state = EventState.REVIEW.value
    event.save(update_fields=("derivatives_ready_generation", "state"))
    monkeypatch.setattr(views, "configured_object_store", SigningStore)

    photographer_client = Client()
    photographer_client.force_login(user)
    dashboard = photographer_client.get(
        reverse("events:photographer-event", args=(event.id,)),
        headers={"host": "alpha.localhost"},
    )
    assert dashboard.status_code == 200
    assert dashboard.headers["Cache-Control"] == "private, no-store"
    assert dashboard.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"
    assert b"client-private-name.jpg" not in dashboard.content
    assert b"thumbnails" in dashboard.content

    published = photographer_client.post(
        reverse("events:publish-event", args=(event.id,)),
        headers={"host": "alpha.localhost"},
    )
    assert published.status_code == 302
    event.refresh_from_db()
    assert event.state == EventState.PUBLISHED.value

    visitor = Client()
    photo_url = reverse("events:visitor-photo", args=(event.public_token, asset.id))
    assert visitor.get(photo_url, headers={"host": "alpha.localhost"}).status_code == 404
    event_url = reverse("events:event-access", args=(event.public_token,))
    visitor.post(event_url, {"pin": "123456"}, headers={"host": "alpha.localhost"})
    gallery = visitor.get(event_url, headers={"host": "alpha.localhost"})
    assert gallery.status_code == 200
    assert gallery.headers["Cache-Control"] == "private, no-store"
    assert b"thumbnails" in gallery.content
    assert b"previews" not in gallery.content
    assert b"client-private-name.jpg" not in gallery.content

    photo = visitor.get(photo_url, headers={"host": "alpha.localhost"})
    assert photo.status_code == 200
    assert b"previews" in photo.content
    assert b"originals" not in photo.content

    old_cookie = visitor.cookies[visitor_cookie_name(event)].value
    photographer_client.post(
        reverse("events:unpublish-event", args=(event.id,)),
        headers={"host": "alpha.localhost"},
    )
    event.refresh_from_db()
    assert event.state == EventState.REVIEW.value
    assert visitor.cookies[visitor_cookie_name(event)].value == old_cookie
    assert visitor.get(photo_url, headers={"host": "alpha.localhost"}).status_code == 404


def test_dashboard_is_tenant_scoped_and_updates_download_policy(monkeypatch) -> None:
    _, user, event, _ = _tenant()
    _, other_user, _, _ = _tenant("beta")
    monkeypatch.setattr(views, "configured_object_store", SigningStore)
    client = Client()
    client.force_login(other_user)
    assert (
        client.get(
            reverse("events:photographer-event", args=(event.id,)),
            headers={"host": "beta.localhost"},
        ).status_code
        == 404
    )

    client.force_login(user)
    changed = client.post(
        reverse("events:update-download-policy", args=(event.id,)),
        {"policy": "explicit-shares"},
        headers={"host": "alpha.localhost"},
    )
    assert changed.status_code == 302
    event.refresh_from_db()
    assert event.original_download_policy == "explicit-shares"
    assert event.audit_events.filter(action=AuditAction.DOWNLOAD_POLICY_CHANGED).exists()


def test_failed_asset_exclusion_requires_five_attempts_and_is_reversible() -> None:
    _, user, event, batch = _tenant()
    asset = _gallery_asset(event, batch)
    asset.derivative_failure_code = "invalid_color_profile"
    asset.derivative_attempt_count = 4
    asset.save(update_fields=("derivative_failure_code", "derivative_attempt_count"))

    with pytest.raises(IngestionError) as retry_first:
        exclude_from_gallery(
            event=event,
            asset_id=asset.id,
            actor=user,
            reason="The verified source cannot produce a safe preview.",
        )
    assert retry_first.value.code == "derivative_retries_remaining"

    asset.derivative_attempt_count = 5
    asset.save(update_fields=("derivative_attempt_count",))
    excluded = exclude_from_gallery(
        event=event,
        asset_id=asset.id,
        actor=user,
        reason="The verified source cannot produce a safe preview.",
    )
    assert excluded.gallery_excluded_at is not None
    assert event.audit_events.filter(action=AuditAction.ASSET_GALLERY_EXCLUDED).exists()

    restored = restore_to_gallery(event=event, asset_id=asset.id, actor=user)
    assert restored.gallery_excluded_at is None
    assert restored.gallery_exclusion_reason == ""
    assert event.audit_events.filter(action=AuditAction.ASSET_GALLERY_RESTORED).exists()


def test_gallery_pages_are_capped_at_48_thumbnails() -> None:
    _, _, event, batch = _tenant()
    for position in range(1, 50):
        _gallery_asset(
            event,
            batch,
            position=position,
            filename=f"private-{position}.jpg",
        )

    first, first_images = gallery_page(
        event=event,
        page_number=1,
        object_store=SigningStore(),
    )
    second, second_images = gallery_page(
        event=event,
        page_number=2,
        object_store=SigningStore(),
    )

    assert first.paginator.num_pages == 2
    assert len(first_images) == 48
    assert len(second_images) == 1
    assert first_images[0].asset.original_filename == "private-1.jpg"
