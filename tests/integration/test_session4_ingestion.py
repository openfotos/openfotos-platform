import base64
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.db import OperationalError, close_old_connections, connections
from django.utils import timezone

from openfotos_contracts import ContributionInput, EventState, UploadObjectState
from openfotos_server.events.desktop_auth import (
    authenticate_photographer,
)
from openfotos_server.events.ingestion_services import (
    IngestionError,
    cancel_batch,
    close_intake,
    event_for_session,
    exclude_asset,
    finalize_ingestion,
    issue_upload_leases,
    reopen_intake,
    reserve_contribution,
    revoke_installation,
    verify_uploaded_object,
)
from openfotos_server.events.models import (
    AssetObject,
    DesktopSession,
    Event,
    IngestionManifest,
    Photographer,
    PhotographerMembership,
    SubEvent,
)
from openfotos_storage.backend import (
    ObjectAlreadyExists,
    ObjectHead,
    ObjectStoreError,
    PresignedPut,
)

pytestmark = pytest.mark.django_db


class MemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str, dict[str, str]]] = {}
        self.deleted: list[str] = []

    def presign_put(
        self,
        *,
        key,
        content_length,
        content_md5,
        sha256,
        expires_in_seconds,
    ) -> PresignedPut:
        del content_length, content_md5
        return PresignedPut(
            url=f"https://storage.invalid/{key}",
            headers={"x-amz-meta-openfotos-sha256": sha256},
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
        )

    def upload(self, key: str, content: bytes, *, content_type="image/jpeg", sha256=None) -> None:
        self.objects[key] = (
            content,
            content_type,
            {"openfotos-sha256": sha256 or hashlib.sha256(content).hexdigest()},
        )

    def head(self, key: str) -> ObjectHead | None:
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

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)
        self.deleted.append(key)

    def put_immutable(
        self,
        *,
        key,
        body,
        content_length,
        content_md5,
        sha256,
        content_type,
    ) -> None:
        del content_length, content_md5
        if key in self.objects:
            raise ObjectAlreadyExists
        content = bytes(body)
        self.upload(key, content, content_type=content_type, sha256=sha256)


def setup_event(*, storage_limit=25_000_000_000):
    photographer = Photographer.objects.create(slug="alpha", display_name="Alpha Photos")
    user = get_user_model().objects.create_user(
        username="photographer",
        password="correct-password",
    )
    PhotographerMembership.objects.create(photographer=photographer, user=user)
    event = Event.objects.create(
        photographer=photographer,
        name="Reception",
        storage_limit_bytes=storage_limit,
        expires_at=timezone.now() + timedelta(days=30),
    )
    SubEvent.objects.create(event=event, name="Reception", position=1)
    first = authenticate_photographer(
        photographer=photographer,
        username="photographer",
        password="correct-password",
        installation_id=uuid4(),
    )
    second = authenticate_photographer(
        photographer=photographer,
        username="photographer",
        password="correct-password",
        installation_id=uuid4(),
    )
    return event, first.session, second.session


def contribution(content: bytes, *, batch_id=None, asset_id=None) -> ContributionInput:
    sub_event = SubEvent.objects.get(is_archived=False)
    payload = {
        "batch_id": str(batch_id or uuid4()),
        "sub_event_id": str(sub_event.id),
        "label": "Editor export",
        "processing_profile_id": "pilot-profile-v1",
        "device_label": "Primary workstation",
        "assets": [
            {
                "id": str(asset_id or uuid4()),
                "filename": "edited-photo.jpg",
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
    return ContributionInput.from_dict(payload)


def contribution_many(contents: list[bytes]) -> ContributionInput:
    sub_event = SubEvent.objects.get(is_archived=False)
    return ContributionInput.from_dict(
        {
            "batch_id": str(uuid4()),
            "sub_event_id": str(sub_event.id),
            "label": "Two editor exports",
            "processing_profile_id": "pilot-profile-v1",
            "device_label": "Installation workstation",
            "assets": [
                {
                    "id": str(uuid4()),
                    "filename": f"edited-photo-{index}.jpg",
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "content_md5": base64.b64encode(
                        hashlib.md5(content, usedforsecurity=False).digest()
                    ).decode(),
                    "width": 8,
                    "height": 6,
                }
                for index, content in enumerate(contents, start=1)
            ],
        }
    )


def test_reservation_is_immutable_all_or_nothing_and_device_scoped() -> None:
    content = b"one complete synthetic jpeg payload"
    event, _, first_installation = setup_event(storage_limit=len(content))
    second_installation = authenticate_photographer(
        photographer=event.photographer,
        username="photographer",
        password="correct-password",
        installation_id=uuid4(),
    )
    manifest = contribution(content)

    batch = reserve_contribution(
        session=first_installation,
        event_id=event.id,
        contribution=manifest,
    )
    repeated = reserve_contribution(
        session=first_installation,
        event_id=event.id,
        contribution=manifest,
    )
    assert repeated.id == batch.id
    event.refresh_from_db()
    assert event.reserved_original_bytes == len(content)
    assert event.state == EventState.UPLOADING.value

    with pytest.raises(IngestionError) as quota_error:
        reserve_contribution(
            session=second_installation.session,
            event_id=event.id,
            contribution=contribution(content),
        )
    assert quota_error.value.code == "event_storage_limit"
    assert AssetObject.objects.count() == 1

    with pytest.raises(IngestionError) as privacy_error:
        issue_upload_leases(
            session=second_installation.session,
            event_id=event.id,
            batch_id=batch.id,
            object_store=MemoryObjectStore(),
        )
    assert privacy_error.value.code == "batch_not_found"


def test_lost_response_recovery_verifies_object_and_finalizes_immutable_generation() -> None:
    content = b"synthetic jpeg bytes"
    event, primary, installation = setup_event()
    manifest_input = contribution(content)
    batch = reserve_contribution(
        session=installation,
        event_id=event.id,
        contribution=manifest_input,
    )
    storage = MemoryObjectStore()
    [lease] = issue_upload_leases(
        session=installation,
        event_id=event.id,
        batch_id=batch.id,
        object_store=storage,
    )
    upload = AssetObject.objects.get(asset_id=lease["asset_id"])
    storage.upload(upload.object_key, content)

    verified = verify_uploaded_object(
        session=installation,
        event_id=event.id,
        asset_id=upload.asset_id,
        object_store=storage,
    )
    repeated = verify_uploaded_object(
        session=installation,
        event_id=event.id,
        asset_id=upload.asset_id,
        object_store=storage,
    )
    assert verified.state == repeated.state == UploadObjectState.VERIFIED.value
    event.refresh_from_db()
    assert event.verified_original_bytes == len(content)

    close_intake(session=primary, event_id=event.id)
    finalized = finalize_ingestion(
        session=primary,
        event_id=event.id,
        object_store=storage,
    )
    again = finalize_ingestion(
        session=primary,
        event_id=event.id,
        object_store=storage,
    )
    assert finalized.id == again.id
    assert finalized.document["summary"] == {
        "verified_asset_count": 1,
        "verified_original_bytes": len(content),
        "excluded_asset_count": 0,
    }
    assert finalized.object_key.endswith("manifests/generation-000001.json")
    assert len(IngestionManifest.objects.filter(event=event)) == 1

    reopened = reopen_intake(session=primary, event_id=event.id)
    assert reopened.intake_generation == 2
    assert reopened.current_ingestion_manifest is None
    assert reopened.state == EventState.UPLOADING.value
    assert finalized.object_key in storage.objects


def test_finalization_fails_retryably_when_existing_manifest_cannot_be_read() -> None:
    content = b"synthetic jpeg bytes"
    event, primary, installation = setup_event()
    manifest_input = contribution(content)
    batch = reserve_contribution(
        session=installation,
        event_id=event.id,
        contribution=manifest_input,
    )

    class ExistingManifestUnavailableStore(MemoryObjectStore):
        def put_immutable(self, **kwargs):
            del kwargs
            raise ObjectAlreadyExists

        def head(self, key):
            if "/manifests/" in key:
                raise ObjectStoreError("synthetic outage")
            return super().head(key)

    storage = ExistingManifestUnavailableStore()
    issue_upload_leases(
        session=installation,
        event_id=event.id,
        batch_id=batch.id,
        object_store=storage,
    )
    original = AssetObject.objects.get(asset_id=manifest_input.assets[0].id)
    MemoryObjectStore.upload(storage, original.object_key, content)
    verify_uploaded_object(
        session=installation,
        event_id=event.id,
        asset_id=original.asset_id,
        object_store=storage,
    )
    close_intake(session=primary, event_id=event.id)

    with pytest.raises(IngestionError) as unavailable:
        finalize_ingestion(session=primary, event_id=event.id, object_store=storage)

    assert unavailable.value.code == "object_store_unavailable"
    assert unavailable.value.retryable
    event.refresh_from_db()
    assert event.state == EventState.UPLOADING.value


def test_mismatching_uploaded_bytes_are_deleted_and_fail_closed() -> None:
    expected = b"expected synthetic jpeg bytes"
    uploaded = b"different synthetic jpeg bytes"
    event, _, installation = setup_event()
    manifest_input = contribution(expected)
    batch = reserve_contribution(
        session=installation,
        event_id=event.id,
        contribution=manifest_input,
    )
    storage = MemoryObjectStore()
    [lease] = issue_upload_leases(
        session=installation,
        event_id=event.id,
        batch_id=batch.id,
        object_store=storage,
    )
    upload = AssetObject.objects.get(asset_id=lease["asset_id"])
    storage.upload(upload.object_key, uploaded, sha256=upload.asset.sha256)

    with pytest.raises(IngestionError) as error:
        verify_uploaded_object(
            session=installation,
            event_id=event.id,
            asset_id=upload.asset_id,
            object_store=storage,
        )

    assert error.value.code == "asset_size_mismatch"
    upload.refresh_from_db()
    assert upload.state == UploadObjectState.FAILED.value
    assert upload.object_key in storage.deleted


def test_upload_only_session_cannot_resolve_another_event() -> None:
    event, _, installation = setup_event()
    other = Event.objects.create(photographer=event.photographer, name="Other")

    assert event_for_session(installation, other.id) == other


def test_batch_cancellation_waits_for_leases_deletes_objects_and_releases_quota() -> None:
    content = b"cancelled synthetic jpeg"
    event, primary, installation = setup_event(storage_limit=len(content))
    manifest_input = contribution(content)
    batch = reserve_contribution(
        session=installation,
        event_id=event.id,
        contribution=manifest_input,
    )
    storage = MemoryObjectStore()
    issue_upload_leases(
        session=installation,
        event_id=event.id,
        batch_id=batch.id,
        object_store=storage,
    )

    with pytest.raises(IngestionError) as active_lease:
        cancel_batch(
            session=primary,
            event_id=event.id,
            batch_id=batch.id,
            object_store=storage,
        )
    assert active_lease.value.code == "upload_lease_active"

    upload = AssetObject.objects.get(asset_id=manifest_input.assets[0].id)
    AssetObject.objects.filter(pk=upload.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1)
    )
    storage.upload(upload.object_key, content)
    cancelled = cancel_batch(
        session=primary,
        event_id=event.id,
        batch_id=batch.id,
        object_store=storage,
    )
    repeated = cancel_batch(
        session=primary,
        event_id=event.id,
        batch_id=batch.id,
        object_store=storage,
    )

    event.refresh_from_db()
    upload.refresh_from_db()
    assert cancelled.id == repeated.id
    assert cancelled.state == "cancelled"
    assert upload.state == UploadObjectState.EXCLUDED.value
    assert event.reserved_original_bytes == event.verified_original_bytes == 0
    assert upload.object_key in storage.deleted


def test_storage_failure_during_cancellation_preserves_reservation() -> None:
    content = b"synthetic jpeg retained on failure"
    event, primary, installation = setup_event(storage_limit=len(content))
    manifest_input = contribution(content)
    batch = reserve_contribution(
        session=installation,
        event_id=event.id,
        contribution=manifest_input,
    )

    class UnavailableObjectStore(MemoryObjectStore):
        def head(self, key):
            del key
            raise ObjectStoreError("synthetic outage")

    with pytest.raises(IngestionError) as unavailable:
        cancel_batch(
            session=primary,
            event_id=event.id,
            batch_id=batch.id,
            object_store=UnavailableObjectStore(),
        )
    assert unavailable.value.code == "object_store_unavailable"
    assert unavailable.value.retryable
    event.refresh_from_db()
    batch.refresh_from_db()
    upload = AssetObject.objects.get(asset_id=manifest_input.assets[0].id)
    assert event.reserved_original_bytes == len(content)
    assert batch.state == "reserved"
    assert upload.state == UploadObjectState.RESERVED.value


def test_partial_batch_requires_reasoned_exclusion_and_keeps_verified_bytes_charged() -> None:
    contents = [b"verified synthetic jpeg", b"excluded synthetic jpeg"]
    event, primary, installation = setup_event(storage_limit=sum(map(len, contents)))
    manifest_input = contribution_many(contents)
    batch = reserve_contribution(
        session=installation,
        event_id=event.id,
        contribution=manifest_input,
    )
    storage = MemoryObjectStore()
    issue_upload_leases(
        session=installation,
        event_id=event.id,
        batch_id=batch.id,
        object_store=storage,
    )
    first_asset, second_asset = manifest_input.assets
    first_upload = AssetObject.objects.get(asset_id=first_asset.id)
    storage.upload(first_upload.object_key, contents[0])
    verify_uploaded_object(
        session=installation,
        event_id=event.id,
        asset_id=first_asset.id,
        object_store=storage,
    )
    AssetObject.objects.filter(asset_id=second_asset.id).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1)
    )

    excluded = exclude_asset(
        session=primary,
        event_id=event.id,
        asset_id=second_asset.id,
        reason="Source drive was lost after reservation.",
        object_store=storage,
    )

    event.refresh_from_db()
    batch.refresh_from_db()
    assert excluded.state == UploadObjectState.EXCLUDED.value
    assert batch.state == "complete"
    assert event.reserved_original_bytes == len(contents[0])
    assert event.verified_original_bytes == len(contents[0])
    with pytest.raises(IngestionError) as cancellation:
        cancel_batch(
            session=primary,
            event_id=event.id,
            batch_id=batch.id,
            object_store=storage,
        )
    assert cancellation.value.code == "batch_has_verified_assets"

    close_intake(session=primary, event_id=event.id)
    finalized = finalize_ingestion(
        session=primary,
        event_id=event.id,
        object_store=storage,
    )
    assert finalized.asset_count == 1
    assert finalized.excluded_asset_count == 1


def test_revoked_installation_keeps_reservation_for_photographer_verification() -> None:
    content = b"uploaded before installation revocation"
    event, primary, installation = setup_event(storage_limit=len(content))
    manifest_input = contribution(content)
    batch = reserve_contribution(
        session=installation,
        event_id=event.id,
        contribution=manifest_input,
    )
    storage = MemoryObjectStore()
    [lease] = issue_upload_leases(
        session=installation,
        event_id=event.id,
        batch_id=batch.id,
        object_store=storage,
    )
    upload = AssetObject.objects.get(asset_id=lease["asset_id"])
    storage.upload(upload.object_key, content)
    revoke_installation(
        session=primary,
        event_id=event.id,
        installation_id=batch.installation_id,
    )

    with pytest.raises(IngestionError) as revoked:
        issue_upload_leases(
            session=installation,
            event_id=event.id,
            batch_id=batch.id,
            object_store=storage,
        )
    assert revoked.value.code == "batch_not_found"
    event.refresh_from_db()
    assert event.reserved_original_bytes == len(content)

    verified = verify_uploaded_object(
        session=primary,
        event_id=event.id,
        asset_id=upload.asset_id,
        object_store=storage,
    )
    assert verified.state == UploadObjectState.VERIFIED.value


@pytest.mark.django_db(transaction=True)
def test_ten_devices_racing_reservations_never_exceed_event_allowance() -> None:
    content = b"fixed-size synthetic jpeg"
    photographer = Photographer.objects.create(slug="race", display_name="Race Photos")
    user = get_user_model().objects.create_user(
        username="race-photographer",
        password="correct-password",
    )
    PhotographerMembership.objects.create(photographer=photographer, user=user)
    event = Event.objects.create(
        photographer=photographer,
        name="Concurrent reception",
        storage_limit_bytes=5 * len(content),
        expires_at=timezone.now() + timedelta(days=30),
    )
    SubEvent.objects.create(event=event, name="Reception", position=1)
    session_ids = []
    contributions = []
    for _index in range(10):
        tokens = authenticate_photographer(
            photographer=photographer,
            username="race-photographer",
            password="correct-password",
            installation_id=uuid4(),
        )
        session_ids.append(tokens.session.id)
        contributions.append(contribution(content))

    def attempt(args):
        session_id, manifest_input = args
        close_old_connections()
        try:
            for retry in range(20):
                try:
                    session = DesktopSession.objects.select_related("photographer", "user").get(
                        pk=session_id
                    )
                    reserve_contribution(
                        session=session,
                        event_id=event.id,
                        contribution=manifest_input,
                    )
                    return "reserved"
                except OperationalError:
                    if retry == 19:
                        raise
                    time.sleep(0.01 * (retry + 1))
                except IngestionError as exc:
                    return exc.code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(attempt, zip(session_ids, contributions, strict=True)))

    event.refresh_from_db()
    assert results.count("reserved") == 5
    assert results.count("event_storage_limit") == 5
    assert event.reserved_original_bytes == event.storage_limit_bytes
    assert AssetObject.objects.filter(asset__batch__installation__event=event).count() == 5
