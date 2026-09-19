import base64
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from openfotos_contracts import (
    AssetVariant,
    ContributionState,
    EventState,
    IntakeState,
    UploadObjectState,
)
from openfotos_server.events import views
from openfotos_server.events.derivative_services import issue_derivative_leases
from openfotos_server.events.desktop_auth import token_digest
from openfotos_server.events.ingestion_services import (
    IngestionError,
    issue_upload_leases,
)
from openfotos_server.events.models import (
    Asset,
    AssetObject,
    ConsentAttestation,
    ContributionBatch,
    DesktopSession,
    Event,
    EventInstallation,
    FaceAnalysis,
    FaceAnalysisState,
    Photographer,
    PhotographerMembership,
    PreviewPolicy,
    SubEvent,
)
from openfotos_server.events.portfolio_services import (
    CONSENT_NOTICE_VERSION,
    publish_event_with_portal,
    unpublish_event_for_upload,
)
from openfotos_storage import PresignedGet
from openfotos_storage.backend import ObjectAlreadyExists, ObjectHead

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def configure_test_static_files(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    settings.MIDDLEWARE = [
        item for item in settings.MIDDLEWARE if item != "whitenoise.middleware.WhiteNoiseMiddleware"
    ]


class MemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str, str]] = {}

    def put_immutable(
        self, *, key, body, content_length, content_md5, sha256, content_type
    ) -> None:
        content = bytes(body)
        assert len(content) == content_length
        assert (
            content_md5
            == base64.b64encode(hashlib.md5(content, usedforsecurity=False).digest()).decode()
        )
        if key in self.objects:
            raise ObjectAlreadyExists
        self.objects[key] = (content, content_type, sha256)

    def head(self, key):
        stored = self.objects.get(key)
        if stored is None:
            return None
        content, content_type, sha256 = stored
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
        return PresignedGet(
            url=f"https://storage.invalid/{key}",
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
        )


@dataclass(frozen=True)
class PublicationContext:
    user: object
    event: Event
    session: DesktopSession
    complete_batch: ContributionBatch
    included_asset: Asset
    in_flight_batch: ContributionBatch


def _object(asset, variant, *, state=UploadObjectState.VERIFIED.value, size=10):
    digest = hashlib.sha256(f"{asset.id}:{variant}".encode()).hexdigest()
    return AssetObject.objects.create(
        asset=asset,
        variant=variant,
        object_key=f"events/{asset.batch.installation.event_id}/{variant}/{asset.id}.jpg",
        expected_bytes=size,
        sha256=digest,
        content_md5=base64.b64encode(
            hashlib.md5(digest.encode(), usedforsecurity=False).digest()
        ).decode(),
        width=100,
        height=80,
        state=state,
        verified_at=timezone.now() if state == UploadObjectState.VERIFIED.value else None,
        lease_expires_at=(
            timezone.now() + timedelta(minutes=5)
            if state == UploadObjectState.RESERVED.value
            else None
        ),
    )


def _publication_context(*, face_ready=True, in_flight_count=3) -> PublicationContext:
    photographer = Photographer.objects.create(slug="alpha", display_name="Alpha Photos")
    user = get_user_model().objects.create_user(username="photographer", password="safe-pass")
    PhotographerMembership.objects.create(photographer=photographer, user=user)
    event = Event.objects.create(
        photographer=photographer,
        name="Private Wedding",
        state=EventState.UPLOADING.value,
        intake_state=IntakeState.OPEN.value,
        cover_object_key=f"events/{uuid4()}/portfolio/cover.jpg",
        cover_sha256="a" * 64,
        cover_width=1200,
        cover_height=800,
        reserved_original_count=1 + in_flight_count,
        reserved_original_bytes=10 * (1 + in_flight_count),
        verified_original_count=1,
        verified_original_bytes=10,
    )
    ConsentAttestation.objects.create(
        event=event,
        actor=user,
        notice_version=CONSENT_NOTICE_VERSION,
    )
    PreviewPolicy.objects.create(event=event, enabled=False, confirmed_by=user)
    sub_event = SubEvent.objects.create(event=event, name="Reception", position=1)
    installation = EventInstallation.objects.create(
        event=event,
        user=user,
        installation_id=uuid4(),
        label="Studio workstation 1",
    )
    other_installation = EventInstallation.objects.create(
        event=event,
        user=user,
        installation_id=uuid4(),
        label="Studio workstation 2",
    )
    session = DesktopSession.objects.create(
        photographer=photographer,
        user=user,
        installation_id=other_installation.installation_id,
        access_token_hash="a" * 64,
        access_expires_at=timezone.now() + timedelta(hours=1),
        refresh_token_hash="b" * 64,
        refresh_expires_at=timezone.now() + timedelta(days=1),
    )
    complete_batch = ContributionBatch.objects.create(
        id=uuid4(),
        installation=installation,
        sub_event=sub_event,
        intake_generation=event.intake_generation,
        state=ContributionState.COMPLETE.value,
        label="Completed batch",
        processing_profile_id=event.processing_profile_id,
        declared_asset_count=1,
        declared_original_bytes=10,
        manifest_sha256="c" * 64,
        completed_at=timezone.now(),
    )
    included_asset = Asset.objects.create(
        id=uuid4(),
        batch=complete_batch,
        original_filename="included.jpg",
        width=100,
        height=80,
        sha256="d" * 64,
        gallery_position=1,
    )
    for variant in ("originals", "previews", "thumbnails"):
        _object(included_asset, variant)
    if face_ready:
        FaceAnalysis.objects.create(
            asset=included_asset,
            state=FaceAnalysisState.NO_USABLE_FACE,
            source_sha256=included_asset.sha256,
            model_id=event.face_model_id,
            completed_at=timezone.now(),
        )

    in_flight_batch = ContributionBatch.objects.create(
        id=uuid4(),
        installation=other_installation,
        sub_event=sub_event,
        intake_generation=event.intake_generation,
        state=ContributionState.RESERVED.value,
        label="Uploading batch",
        processing_profile_id=event.processing_profile_id,
        declared_asset_count=in_flight_count,
        declared_original_bytes=10 * in_flight_count,
        manifest_sha256="e" * 64,
    )
    for index in range(in_flight_count):
        asset = Asset.objects.create(
            id=uuid4(),
            batch=in_flight_batch,
            original_filename=f"unfinished-{index}.jpg",
            width=100,
            height=80,
            sha256=f"{index + 1:064x}",
        )
        _object(asset, "originals", state=UploadObjectState.RESERVED.value)
    return PublicationContext(
        user=user,
        event=event,
        session=session,
        complete_batch=complete_batch,
        included_asset=included_asset,
        in_flight_batch=in_flight_batch,
    )


def test_publish_snapshots_only_completed_batches_and_unpublish_reopens_uploads() -> None:
    context = _publication_context()
    store = MemoryObjectStore()

    published = publish_event_with_portal(
        event=context.event,
        actor=context.user,
        object_store=store,
    )

    event = published.event
    context.in_flight_batch.refresh_from_db()
    manifest = event.current_ingestion_manifest
    assert event.state == EventState.PUBLISHED.value
    assert event.intake_state == IntakeState.CLOSED.value
    assert manifest.asset_count == 1
    assert [item["batch_id"] for item in manifest.document["contributions"]] == [
        str(context.complete_batch.id)
    ]
    assert [item["asset_id"] for item in manifest.document["assets"]] == [
        str(context.included_asset.id)
    ]
    assert context.in_flight_batch.state == ContributionState.NOT_INCLUDED.value
    assert set(
        AssetObject.objects.filter(asset__batch=context.in_flight_batch).values_list(
            "state", flat=True
        )
    ) == {UploadObjectState.EXCLUDED.value}
    assert event.reserved_original_count == event.verified_original_count == 1
    assert published.capability.event_id == event.id

    with pytest.raises(IngestionError) as blocked:
        issue_upload_leases(
            session=context.session,
            event_id=event.id,
            batch_id=context.in_flight_batch.id,
            object_store=store,
        )
    assert blocked.value.code == "event_published"

    reopened = unpublish_event_for_upload(event=event, actor=context.user)
    assert reopened.state == EventState.UPLOADING.value
    assert reopened.intake_state == IntakeState.OPEN.value
    assert reopened.intake_generation == 2
    assert reopened.current_ingestion_manifest is None


def test_derivative_leases_are_blocked_after_publication() -> None:
    context = _publication_context()
    store = MemoryObjectStore()
    publish_event_with_portal(event=context.event, actor=context.user, object_store=store)

    asset_id = (
        Asset.objects.filter(batch=context.in_flight_batch).values_list("id", flat=True).first()
    )
    assert asset_id is not None
    with pytest.raises(IngestionError) as blocked:
        issue_derivative_leases(
            session=context.session,
            event_id=context.event.id,
            asset_id=asset_id,
            variants=(AssetVariant.PREVIEW,),
            object_store=store,
        )

    assert blocked.value.code == "event_not_processing"


def test_not_included_batch_detail_matches_desktop_sync_contract() -> None:
    context = _publication_context()
    store = MemoryObjectStore()
    publish_event_with_portal(event=context.event, actor=context.user, object_store=store)

    access_token = "desktop-access-token-for-the-sync-contract"
    context.session.access_token_hash = token_digest(access_token)
    context.session.save(update_fields=("access_token_hash", "updated_at"))

    client = Client()
    response = client.get(
        f"/api/v1/events/{context.event.id}/batches/{context.in_flight_batch.id}/",
        headers={
            "host": "alpha.localhost",
            "authorization": f"Bearer {access_token}",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "not_included"
    assert len(body["assets"]) == 3
    assert {item["variant"] for item in body["assets"]} == {AssetVariant.ORIGINAL.value}
    assert {item["state"] for item in body["assets"]} == {UploadObjectState.EXCLUDED.value}


def test_failed_publish_does_not_exclude_in_flight_work() -> None:
    context = _publication_context(face_ready=False)
    store = MemoryObjectStore()

    with pytest.raises(IngestionError) as error:
        publish_event_with_portal(
            event=context.event,
            actor=context.user,
            object_store=store,
        )

    assert error.value.code == "face_index_required"
    context.event.refresh_from_db()
    context.in_flight_batch.refresh_from_db()
    assert context.event.state == EventState.UPLOADING.value
    assert context.event.intake_state == IntakeState.OPEN.value
    assert context.in_flight_batch.state == ContributionState.RESERVED.value
    assert set(
        AssetObject.objects.filter(asset__batch=context.in_flight_batch).values_list(
            "state", flat=True
        )
    ) == {UploadObjectState.RESERVED.value}
    assert not store.objects


def test_event_dashboard_has_five_sections_and_requires_in_flight_confirmation(
    monkeypatch,
) -> None:
    context = _publication_context(in_flight_count=3)
    store = MemoryObjectStore()
    monkeypatch.setattr(views, "configured_object_store", lambda: store)
    client = Client()
    client.force_login(context.user)
    dashboard_url = reverse("events:photographer-event", args=(context.event.id,))

    response = client.get(dashboard_url, headers={"host": "alpha.localhost"})
    content = response.content.decode()
    sections = re.findall(r'data-dashboard-section="([^"]+)"', content)
    assert sections == [
        "sub-event-creation",
        "sub-events",
        "contribution-assignments",
        "publication",
        "gallery-preview",
    ]
    assert "Studio workstation 2 has 3 photos mid-upload" in content
    assert "Do you really want to publish?" in content
    assert "Generate a new event PIN" not in content
    assert 'class="sub-event-filters dashboard-gallery-filters"' in content

    publish_url = reverse("events:publish-event", args=(context.event.id,))
    unconfirmed = client.post(publish_url, headers={"host": "alpha.localhost"})
    assert unconfirmed.status_code == 302
    context.event.refresh_from_db()
    assert context.event.state == EventState.UPLOADING.value

    confirmed = client.post(
        publish_url,
        {"confirm_in_flight": "yes"},
        headers={"host": "alpha.localhost"},
    )
    assert confirmed.status_code == 200
    assert b"Portfolio gallery" in confirmed.content
    context.event.refresh_from_db()
    assert context.event.state == EventState.PUBLISHED.value

    published_dashboard = client.get(dashboard_url, headers={"host": "alpha.localhost"})
    assert b"Generate a new event PIN" in published_dashboard.content


def test_dashboard_explains_processing_blockers(monkeypatch) -> None:
    context = _publication_context()
    monkeypatch.setattr(views, "configured_object_store", lambda: MemoryObjectStore())
    context.included_asset.derivative_failure_code = "invalid_color_profile"
    context.included_asset.derivative_attempt_count = 5
    context.included_asset.save(
        update_fields=("derivative_failure_code", "derivative_attempt_count")
    )
    client = Client()
    client.force_login(context.user)

    response = client.get(
        reverse("events:photographer-event", args=(context.event.id,)),
        headers={"host": "alpha.localhost"},
    )
    content = response.content.decode()

    assert response.status_code == 200
    assert "Preview and thumbnail blockers" in content
    assert "block publication until you exclude them" in content
    assert "Derivative blockers" not in content


def test_republish_after_unpublish_revalidates_every_included_photo() -> None:
    context = _publication_context()
    store = MemoryObjectStore()
    publish_event_with_portal(event=context.event, actor=context.user, object_store=store)

    reopened = unpublish_event_for_upload(event=context.event, actor=context.user)
    AssetObject.objects.filter(
        asset=context.included_asset,
        variant=AssetVariant.PREVIEW.value,
    ).update(state=UploadObjectState.RESERVED.value, verified_at=None)

    with pytest.raises(IngestionError) as refused:
        publish_event_with_portal(
            event=reopened,
            actor=context.user,
            object_store=store,
        )

    assert refused.value.code == "derivatives_required"
    refreshed = Event.objects.get(pk=context.event.id)
    assert refreshed.state == EventState.UPLOADING.value
    assert refreshed.intake_state == IntakeState.OPEN.value
    assert refreshed.current_ingestion_manifest is None

    AssetObject.objects.filter(
        asset=context.included_asset,
        variant=AssetVariant.PREVIEW.value,
    ).update(state=UploadObjectState.VERIFIED.value, verified_at=timezone.now())
    republished = publish_event_with_portal(
        event=refreshed,
        actor=context.user,
        object_store=store,
    )

    assert republished.event.state == EventState.PUBLISHED.value
    assert republished.event.intake_state == IntakeState.CLOSED.value
    assert republished.event.current_ingestion_manifest.generation == 2
    # The event PIN survives an unpublish/re-publish cycle; only leaked sessions are invalidated.
    assert republished.pin is None
