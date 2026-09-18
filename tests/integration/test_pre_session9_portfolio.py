import base64
import hashlib
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from openfotos_contracts import EventState, IntakeState
from openfotos_server.events.models import (
    Asset,
    AssetObject,
    ConsentAttestation,
    ContributionBatch,
    Event,
    EventInstallation,
    FaceAnalysis,
    FaceSearchResultSet,
    GuestCapability,
    IngestionManifest,
    OwnerCapability,
    Photographer,
    PhotographerMembership,
    PortalCapability,
    SubEvent,
)
from openfotos_server.events.portfolio_services import (
    CONSENT_NOTICE_VERSION,
    publish_event_with_portal,
)
from openfotos_server.events.retention_services import (
    RetentionError,
    erase_event_for_privacy,
    purge_event_media,
)
from openfotos_storage.backend import ObjectAlreadyExists, ObjectHead, PresignedGet

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }


class MemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str, str]] = {}

    def put_immutable(
        self, *, key, body, content_length, content_md5, sha256, content_type
    ) -> None:
        content = bytes(body)
        assert content_length == len(content)
        assert (
            content_md5
            == base64.b64encode(hashlib.md5(content, usedforsecurity=False).digest()).decode()
        )
        if key in self.objects:
            raise ObjectAlreadyExists
        self.objects[key] = (content, content_type, sha256)

    def head(self, key):
        value = self.objects.get(key)
        if value is None:
            return None
        content, content_type, sha256 = value
        return ObjectHead(
            key=key,
            content_length=len(content),
            content_type=content_type,
            etag=hashlib.md5(content, usedforsecurity=False).hexdigest(),
            metadata={"openfotos-sha256": sha256},
        )

    def delete(self, key):
        self.objects.pop(key, None)

    def presign_get(self, *, key, expires_in_seconds, content_disposition="inline"):
        del content_disposition
        assert expires_in_seconds > 0
        return PresignedGet(
            url=f"https://storage.invalid/{key}",
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
        )


def image_upload(name="cover.png") -> SimpleUploadedFile:
    content = BytesIO()
    image = Image.new("RGB", (24, 16), color="navy")
    image.getexif()[0x010E] = "private source metadata"
    image.save(content, format="PNG", exif=image.getexif())
    return SimpleUploadedFile(name, content.getvalue(), content_type="image/png")


@pytest.fixture
def tenant():
    photographer = Photographer.objects.create(slug="alpha", display_name="Alpha Photos")
    user = get_user_model().objects.create_user(username="studio", password="correct-password")
    PhotographerMembership.objects.create(photographer=photographer, user=user)
    return photographer, user


def test_photographer_creates_event_with_sanitized_cover_and_versioned_consent(
    tenant, monkeypatch
) -> None:
    photographer, user = tenant
    store = MemoryObjectStore()
    monkeypatch.setattr("openfotos_server.events.views.configured_object_store", lambda: store)
    client = Client()
    client.force_login(user)

    response = client.post(
        reverse("events:create-event"),
        {
            "name": "A & B Wedding",
            "cover_photo": image_upload(),
            "consent_attested": "on",
        },
        headers={"host": "alpha.localhost"},
    )

    event = Event.objects.get(photographer=photographer)
    assert response.status_code == 302
    assert response.url == reverse("events:photographer-event", args=(event.id,))
    assert event.name == "A & B Wedding"
    assert event.cover_object_key.startswith(f"events/{event.id}/portfolio/cover-")
    assert event.consent_attestation.notice_version == CONSENT_NOTICE_VERSION
    stored, content_type, _sha256 = store.objects[event.cover_object_key]
    assert content_type == "image/jpeg"
    with Image.open(BytesIO(stored)) as sanitized:
        assert sanitized.format == "JPEG"
        assert not sanitized.getexif()


def test_portfolio_lists_only_published_events_and_portal_pin_unlocks_without_owner_controls(
    tenant, monkeypatch
) -> None:
    photographer, user = tenant
    now = timezone.now()
    published = Event.objects.create(
        photographer=photographer,
        name="Published Wedding",
        state=EventState.PUBLISHED.value,
        cover_object_key="events/published/portfolio/cover.jpg",
        cover_sha256="a" * 64,
        cover_width=1200,
        cover_height=800,
        expires_at=now + timedelta(days=20),
        first_published_at=now,
        purge_after=now + timedelta(days=50),
    )
    draft = Event.objects.create(
        photographer=photographer,
        name="Hidden Draft",
        cover_object_key="events/draft/portfolio/cover.jpg",
    )
    portal = PortalCapability(event=published, expires_at=published.expires_at)
    portal.set_pin("0427")
    portal.save()
    store = MemoryObjectStore()
    monkeypatch.setattr("openfotos_server.events.views.configured_object_store", lambda: store)
    monkeypatch.setattr(
        "openfotos_server.events.share_views.configured_object_store", lambda: store
    )
    client = Client()

    listing = client.get(reverse("events:portfolio"), headers={"host": "alpha.localhost"})

    assert listing.status_code == 200
    assert published.name.encode() in listing.content
    assert draft.name.encode() not in listing.content
    assert reverse("events:portal-gallery", args=(portal.id,)).encode() in listing.content

    locked = client.get(
        reverse("events:portal-gallery", args=(portal.id,)),
        headers={"host": "alpha.localhost"},
    )
    assert locked.status_code == 200
    assert b"Enter the four-digit PIN" in locked.content

    unlocked = client.post(
        reverse("events:portal-unlock", args=(portal.id,)),
        {"pin": "0427"},
        headers={"host": "alpha.localhost"},
    )
    assert unlocked.status_code == 302
    gallery = client.get(unlocked.url, headers={"host": "alpha.localhost"})
    assert gallery.status_code == 200
    assert b"Owner controls" not in gallery.content
    assert b"Search with one clear face" in gallery.content


def test_first_publication_creates_one_time_four_digit_portal_pin(tenant) -> None:
    photographer, user = tenant
    event = Event.objects.create(
        photographer=photographer,
        name="Ready Wedding",
        state=EventState.PUBLISHED.value,
        cover_object_key="events/ready/portfolio/cover.jpg",
        cover_sha256="b" * 64,
        cover_width=1200,
        cover_height=800,
        expires_at=timezone.now() + timedelta(days=1),
    )
    ConsentAttestation.objects.create(
        event=event,
        actor=user,
        notice_version=CONSENT_NOTICE_VERSION,
    )

    issued = publish_event_with_portal(event=event, actor=user)

    assert issued.pin is not None and len(issued.pin) == 4 and issued.pin.isascii()
    assert issued.capability.check_pin(issued.pin)
    assert issued.event.first_published_at is not None
    assert issued.event.expires_at - issued.event.first_published_at == timedelta(days=365)
    assert issued.event.purge_after - issued.event.expires_at == timedelta(days=30)


def test_due_retention_purge_removes_private_media_and_keeps_cover_card(tenant) -> None:
    photographer, user = tenant
    now = timezone.now()
    event = Event.objects.create(
        photographer=photographer,
        name="Retained Showcase",
        state=EventState.PUBLISHED.value,
        cover_object_key="events/showcase/portfolio/cover.jpg",
        cover_sha256="c" * 64,
        cover_width=1200,
        cover_height=800,
        expires_at=now - timedelta(days=31),
        first_published_at=now - timedelta(days=396),
        purge_after=now - timedelta(days=1),
        reserved_original_bytes=1,
        verified_original_bytes=1,
    )
    sub_event = SubEvent.objects.create(event=event, name="Wedding", position=1)
    installation = EventInstallation.objects.create(
        event=event,
        user=user,
        installation_id="00000000-0000-4000-8000-000000000030",
        label="Studio",
    )
    batch = ContributionBatch.objects.create(
        id="00000000-0000-4000-8000-000000000031",
        installation=installation,
        sub_event=sub_event,
        intake_generation=1,
        label="Edited",
        processing_profile_id="pilot-profile-v1",
        declared_asset_count=1,
        declared_original_bytes=1,
        manifest_sha256="d" * 64,
    )
    asset = Asset.objects.create(
        id="00000000-0000-4000-8000-000000000032",
        batch=batch,
        original_filename="private-name.png",
        width=1,
        height=1,
        sha256="e" * 64,
        gallery_excluded_at=now,
        gallery_exclusion_reason="Participant requested removal",
        gallery_excluded_by=user,
    )
    private_key = f"events/{event.id}/originals/{asset.id}.png"
    AssetObject.objects.create(
        asset=asset,
        variant="original",
        object_key=private_key,
        expected_bytes=1,
        sha256="e" * 64,
        content_md5="ndTkYSaMgDT1yFZOFVxnpg==",
        content_type="image/png",
        width=1,
        height=1,
    )
    manifest_key = f"events/{event.id}/manifests/generation-000001.json"
    manifest = IngestionManifest.objects.create(
        event=event,
        generation=1,
        state="committed",
        object_key=manifest_key,
        content_sha256="f" * 64,
        document={"private": True},
        asset_count=1,
        original_bytes=1,
    )
    event.current_ingestion_manifest = manifest
    event.save(update_fields=("current_ingestion_manifest",))
    owner = OwnerCapability(
        event=event,
        created_by=user,
        secret_digest="1" * 64,
        expires_at=now + timedelta(days=1),
    )
    owner.set_pin("1234")
    owner.save()
    guest = GuestCapability(
        owner=owner,
        secret_digest="2" * 64,
        expires_at=now + timedelta(days=1),
    )
    guest.set_pin("5678")
    guest.save()
    portal = PortalCapability(event=event, expires_at=now + timedelta(days=1))
    portal.set_pin("0427")
    portal.save()
    store = MemoryObjectStore()
    store.objects[private_key] = (b"x", "image/png", "e" * 64)
    store.objects[manifest_key] = (b"{}", "application/json", "f" * 64)

    result = purge_event_media(event_id=event.id, object_store=store, at=now)

    event.refresh_from_db()
    asset.refresh_from_db()
    batch.refresh_from_db()
    assert result.deleted_object_count == 2
    assert private_key not in store.objects and manifest_key not in store.objects
    assert event.cover_object_key == "events/showcase/portfolio/cover.jpg"
    assert event.media_purged_at == now
    assert asset.original_filename == "purged"
    assert asset.sha256 == "0" * 64
    assert asset.gallery_excluded_at is None
    assert asset.gallery_exclusion_reason == ""
    assert asset.gallery_excluded_by is None
    assert batch.declared_original_bytes == 0
    assert not AssetObject.objects.filter(asset=asset).exists()
    assert not IngestionManifest.objects.filter(event=event).exists()
    assert not OwnerCapability.objects.filter(event=event).exists()
    assert not GuestCapability.objects.filter(owner=owner).exists()
    assert not PortalCapability.objects.filter(event=event).exists()


def test_retention_purge_fails_before_grace_deadline_without_deleting(tenant) -> None:
    photographer, _user = tenant
    event = Event.objects.create(
        photographer=photographer,
        name="Not Due",
        purge_after=timezone.now() + timedelta(days=1),
    )
    store = MemoryObjectStore()

    with pytest.raises(RetentionError, match="has not reached"):
        purge_event_media(event_id=event.id, object_store=store)

    event.refresh_from_db()
    assert event.media_purged_at is None


def test_retention_command_requires_explicit_confirmation(tenant) -> None:
    photographer, _user = tenant
    event = Event.objects.create(photographer=photographer, name="Confirmation required")

    with pytest.raises(CommandError, match="--confirm"):
        call_command("purge_expired_event_media", event_id=str(event.id))


def test_retention_report_is_non_destructive_and_omits_event_names(tenant, capsys) -> None:
    photographer, _user = tenant
    now = timezone.now()
    due = Event.objects.create(
        photographer=photographer,
        name="Private client name",
        purge_after=now - timedelta(minutes=1),
    )
    upcoming = Event.objects.create(
        photographer=photographer,
        name="Another private name",
        purge_after=now + timedelta(days=3),
    )
    Event.objects.create(
        photographer=photographer,
        name="Already purged",
        purge_after=now - timedelta(days=1),
        media_purged_at=now,
    )

    call_command("report_event_retention", days_ahead=7)

    report = capsys.readouterr().out
    assert str(due.id) in report
    assert str(upcoming.id) in report
    assert "Private client name" not in report
    assert "Another private name" not in report
    assert Event.objects.filter(pk__in=(due.id, upcoming.id)).count() == 2


def test_privacy_erasure_quarantines_then_removes_the_entire_event(tenant) -> None:
    photographer, user = tenant
    now = timezone.now()
    cover_key = "events/private/portfolio/cover.jpg"
    event = Event.objects.create(
        photographer=photographer,
        name="Removal Requested Wedding",
        state=EventState.PUBLISHED.value,
        cover_object_key=cover_key,
        cover_sha256="c" * 64,
        cover_width=1200,
        cover_height=800,
        expires_at=now + timedelta(days=300),
        first_published_at=now - timedelta(days=65),
        purge_after=now + timedelta(days=330),
        reserved_original_bytes=1,
        verified_original_bytes=1,
        reserved_original_count=1,
        verified_original_count=1,
    )
    ConsentAttestation.objects.create(
        event=event,
        actor=user,
        notice_version=CONSENT_NOTICE_VERSION,
    )
    sub_event = SubEvent.objects.create(event=event, name="Private Ceremony", position=1)
    installation = EventInstallation.objects.create(
        event=event,
        user=user,
        installation_id="00000000-0000-4000-8000-000000000040",
        label="Personally named workstation",
    )
    batch = ContributionBatch.objects.create(
        id="00000000-0000-4000-8000-000000000041",
        installation=installation,
        sub_event=sub_event,
        intake_generation=1,
        label="Private folder label",
        processing_profile_id="pilot-profile-v1",
        declared_asset_count=1,
        declared_original_bytes=1,
        manifest_sha256="d" * 64,
    )
    asset = Asset.objects.create(
        id="00000000-0000-4000-8000-000000000042",
        batch=batch,
        original_filename="private-person-name.jpg",
        width=1,
        height=1,
        sha256="e" * 64,
        gallery_excluded_at=now,
        gallery_exclusion_reason="Participant requested removal",
        gallery_excluded_by=user,
    )
    original_key = f"events/{event.id}/originals/{asset.id}.jpg"
    upload = AssetObject.objects.create(
        asset=asset,
        variant="original",
        object_key=original_key,
        expected_bytes=1,
        sha256="e" * 64,
        content_md5="ndTkYSaMgDT1yFZOFVxnpg==",
        content_type="image/jpeg",
        width=1,
        height=1,
        state="verified",
        lease_expires_at=now + timedelta(minutes=5),
    )
    FaceAnalysis.objects.create(asset=asset, model_id=event.face_model_id)
    owner = OwnerCapability(
        event=event,
        created_by=user,
        secret_digest="1" * 64,
        expires_at=now + timedelta(days=1),
    )
    owner.set_pin("1234")
    owner.save()
    FaceSearchResultSet.objects.create(
        owner_capability=owner,
        event=event,
        ordered_asset_ids=[str(asset.id)],
        expires_at=now + timedelta(minutes=10),
    )
    store = MemoryObjectStore()
    store.objects[cover_key] = (b"cover", "image/jpeg", "c" * 64)
    store.objects[original_key] = (b"x", "image/jpeg", "e" * 64)

    with pytest.raises(RetentionError) as active_lease:
        erase_event_for_privacy(
            event_id=event.id,
            instruction_reference="studio-email-20260918",
            object_store=store,
            at=now,
        )

    assert active_lease.value.code == "upload_lease_active"
    event.refresh_from_db()
    assert event.state == EventState.CANCELLED.value
    assert event.intake_state == IntakeState.CLOSED.value
    assert event.erasure_requested_at == now
    assert not OwnerCapability.objects.filter(event=event).exists()
    assert not FaceSearchResultSet.objects.filter(event=event).exists()
    assert set(store.objects) == {cover_key, original_key}

    AssetObject.objects.filter(pk=upload.pk).update(lease_expires_at=now - timedelta(seconds=1))
    result = erase_event_for_privacy(
        event_id=event.id,
        instruction_reference="studio-email-20260918",
        object_store=store,
        at=now + timedelta(minutes=6),
    )

    event.refresh_from_db()
    asset.refresh_from_db()
    batch.refresh_from_db()
    installation.refresh_from_db()
    sub_event.refresh_from_db()
    assert result.deleted_object_count == 2
    assert store.objects == {}
    assert event.name == "Erased event"
    assert event.cover_object_key == ""
    assert event.privacy_erased_at == now + timedelta(minutes=6)
    assert event.reserved_original_count == event.verified_original_count == 0
    assert asset.original_filename == "purged"
    assert asset.gallery_excluded_at is None
    assert asset.gallery_exclusion_reason == ""
    assert asset.gallery_excluded_by is None
    assert batch.label == "" and batch.declared_asset_count == 0
    assert installation.user is None and installation.status == "revoked"
    assert sub_event.name.startswith("Erased ")
    assert not ConsentAttestation.objects.filter(event=event).exists()
    assert not FaceAnalysis.objects.filter(asset=asset).exists()
    assert not AssetObject.objects.filter(asset=asset).exists()


def test_privacy_erasure_command_requires_confirmation_and_exact_name(tenant) -> None:
    photographer, _user = tenant
    event = Event.objects.create(photographer=photographer, name="Exact event")

    with pytest.raises(CommandError, match="--confirm"):
        call_command(
            "erase_event_for_privacy",
            event_id=str(event.id),
            confirm_event_name=event.name,
            instruction_reference="studio-email-20260918",
        )

    with pytest.raises(CommandError, match="does not match"):
        call_command(
            "erase_event_for_privacy",
            event_id=str(event.id),
            confirm_event_name="Wrong event",
            instruction_reference="studio-email-20260918",
            confirm=True,
        )
