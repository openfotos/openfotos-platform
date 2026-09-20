"""Transactional contribution reservation and aggregate reconciliation."""

import base64
import hashlib
import json
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from openfotos_contracts import (
    DERIVATIVE_VARIANTS,
    EVENT_ORIGINAL_ASSET_LIMIT,
    AssetVariant,
    ContributionInput,
    ContributionState,
    EventState,
    IngestionManifestState,
    InstallationStatus,
    IntakeState,
    UploadObjectState,
)
from openfotos_storage import asset_key, ingestion_manifest_key
from openfotos_storage.backend import ObjectAlreadyExists, ObjectStoreError, S3ObjectStore

from .audit import record_audit
from .desktop_auth import DesktopAuthError, register_event_installation
from .event_lifecycle import (
    LifecycleViolation,
    state_for_contribution,
    state_for_finalized_ingestion,
    state_for_reopened_intake,
)
from .models import (
    Asset,
    AssetObject,
    AuditAction,
    AuditResult,
    ContributionBatch,
    DesktopSession,
    Event,
    EventInstallation,
    FaceAnalysis,
    FaceAnalysisState,
    IngestionManifest,
    PhotographerMembership,
    PreviewPolicy,
    SubEvent,
)


class IngestionError(ValueError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def visible_events(session: DesktopSession) -> list[Event]:
    return list(
        Event.objects.filter(
            photographer=session.photographer,
            photographer__memberships__user=session.user,
            photographer__memberships__is_active=True,
        ).distinct()
    )


def event_for_session(session: DesktopSession, event_id: UUID) -> Event:
    try:
        event = Event.objects.select_related("photographer").get(
            pk=event_id,
            photographer=session.photographer,
        )
    except Event.DoesNotExist as exc:
        raise IngestionError("event_not_found", "The event is unavailable.") from exc
    membership = PhotographerMembership.objects.filter(
        photographer=event.photographer,
        user=session.user,
        is_active=True,
    ).exists()
    if not membership:
        raise IngestionError("event_not_found", "The event is unavailable.")
    return event


def asset_for_processing_session(
    *,
    session: DesktopSession,
    event: Event,
    asset_id: UUID,
    sub_event_id: UUID | None = None,
    for_update: bool = False,
) -> Asset:
    """Resolve one active-child asset without widening tenant or installation scope."""
    query = Asset.objects.select_related("batch__installation", "batch__sub_event")
    if for_update:
        query = query.select_for_update()
    filters = {
        "pk": asset_id,
        "batch__installation__event": event,
    }
    if sub_event_id is not None:
        filters["batch__sub_event_id"] = sub_event_id
    try:
        asset = query.get(**filters)
    except Asset.DoesNotExist as exc:
        raise IngestionError("asset_not_found", "The asset is unavailable.") from exc
    installation = EventInstallation.objects.filter(
        event=event,
        user=session.user,
        installation_id=session.installation_id,
        status=InstallationStatus.ACTIVE.value,
    ).first()
    if installation is None:
        raise IngestionError("asset_not_found", "The asset is unavailable.")
    if asset.batch.installation_id != installation.id:
        raise IngestionError("asset_not_found", "The asset is unavailable.")
    if asset.batch.sub_event.is_archived:
        raise IngestionError("sub_event_archived", "The sub-event is archived.")
    return asset


@transaction.atomic
def reserve_contribution(
    *,
    session: DesktopSession,
    event_id: UUID,
    contribution: ContributionInput,
    request=None,
) -> ContributionBatch:
    event = event_for_session(session, event_id)
    installation = _contribution_installation(
        session=session,
        event=event,
        label=contribution.device_label,
        request=request,
    )
    locked_event = Event.objects.select_for_update().get(pk=event.pk)
    try:
        sub_event = SubEvent.objects.get(
            pk=contribution.sub_event_id,
            event=locked_event,
            is_archived=False,
        )
    except SubEvent.DoesNotExist as exc:
        raise IngestionError(
            "sub_event_not_found", "Select an active sub-event in this event."
        ) from exc
    manifest_sha256 = hashlib.sha256(contribution.canonical_bytes()).hexdigest()
    existing = ContributionBatch.objects.filter(pk=contribution.batch_id).first()
    if existing is not None:
        if (
            existing.installation_id == installation.id
            and existing.installation.event_id == locked_event.id
            and existing.sub_event_id == sub_event.id
            and existing.manifest_sha256 == manifest_sha256
        ):
            return existing
        raise IngestionError("idempotency_conflict", "The contribution ID is already in use.")
    if locked_event.state == EventState.PUBLISHED.value:
        raise IngestionError(
            "event_published",
            "The event was published; this contribution was not included.",
        )
    if locked_event.intake_state != IntakeState.OPEN.value:
        raise IngestionError("intake_closed", "The event is not accepting new contributions.")
    try:
        contribution_state = state_for_contribution(EventState(locked_event.state))
    except LifecycleViolation as exc:
        raise IngestionError(exc.code, str(exc)) from exc
    if contribution.processing_profile_id != locked_event.processing_profile_id:
        raise IngestionError(
            "processing_profile_mismatch",
            "The desktop processing profile does not match the event.",
        )
    if (
        locked_event.reserved_original_bytes + contribution.original_bytes
        > locked_event.storage_limit_bytes
    ):
        raise IngestionError(
            "event_storage_limit",
            "The complete contribution exceeds the remaining event allowance.",
        )
    asset_count = len(contribution.assets)
    if locked_event.reserved_original_count + asset_count > EVENT_ORIGINAL_ASSET_LIMIT:
        raise IngestionError(
            "event_asset_limit",
            "The complete contribution exceeds the 10,000-photo event limit.",
        )
    if Asset.objects.filter(pk__in=(asset.id for asset in contribution.assets)).exists():
        raise IngestionError("idempotency_conflict", "One or more asset IDs are already in use.")

    if not PreviewPolicy.objects.filter(event=locked_event).exists():
        policy = PreviewPolicy.objects.create(
            event=locked_event,
            enabled=False,
            confirmed_by=session.user,
        )
        record_audit(
            photographer=locked_event.photographer,
            event=locked_event,
            actor=session.user,
            action=AuditAction.PREVIEW_POLICY_CONFIRMED,
            result=AuditResult.SUCCEEDED,
            request=request,
            metadata={"policy_id": str(policy.id), "enabled": False, "automatic": True},
        )

    batch = ContributionBatch.objects.create(
        id=contribution.batch_id,
        installation=installation,
        sub_event=sub_event,
        intake_generation=locked_event.intake_generation,
        label=contribution.label,
        processing_profile_id=contribution.processing_profile_id,
        declared_asset_count=asset_count,
        declared_original_bytes=contribution.original_bytes,
        manifest_sha256=manifest_sha256,
    )
    assets = [
        Asset(
            id=item.id,
            batch=batch,
            original_filename=item.filename,
            width=item.width,
            height=item.height,
            sha256=item.sha256,
        )
        for item in contribution.assets
    ]
    Asset.objects.bulk_create(assets)
    AssetObject.objects.bulk_create(
        [
            AssetObject(
                asset=asset,
                variant=AssetVariant.ORIGINAL.value,
                object_key=asset_key(
                    locked_event.id,
                    asset.id,
                    AssetVariant.ORIGINAL,
                    content_type=item.content_type,
                ),
                expected_bytes=item.size_bytes,
                sha256=item.sha256,
                content_md5=item.content_md5,
                content_type=item.content_type,
                width=item.width,
                height=item.height,
            )
            for asset, item in zip(assets, contribution.assets, strict=True)
        ]
    )
    locked_event.reserved_original_bytes += contribution.original_bytes
    locked_event.reserved_original_count += asset_count
    locked_event.state = contribution_state.value
    locked_event.save(
        update_fields=(
            "reserved_original_bytes",
            "reserved_original_count",
            "state",
            "updated_at",
        )
    )
    record_audit(
        photographer=locked_event.photographer,
        event=locked_event,
        actor=session.user,
        event_installation=installation,
        action=AuditAction.CONTRIBUTION_RESERVED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={
            "batch_id": str(batch.id),
            "asset_count": batch.declared_asset_count,
            "original_bytes": batch.declared_original_bytes,
            "generation": batch.intake_generation,
            "sub_event_id": str(sub_event.id),
        },
    )
    return batch


@transaction.atomic
def issue_upload_leases(
    *,
    session: DesktopSession,
    event_id: UUID,
    batch_id: UUID,
    object_store: S3ObjectStore,
    asset_ids: tuple[UUID, ...] | None = None,
) -> list[dict]:
    event = event_for_session(session, event_id)
    locked_event = Event.objects.select_for_update().get(pk=event.pk)
    if (
        locked_event.intake_state != IntakeState.OPEN.value
        or locked_event.state == EventState.PUBLISHED.value
    ):
        raise IngestionError(
            "event_published",
            "The event was published; this contribution was not included.",
        )
    batch = _owned_batch(
        session=session,
        event=locked_event,
        batch_id=batch_id,
        for_update=True,
    )
    if batch.state != ContributionState.RESERVED.value:
        return []
    if asset_ids is not None and (
        not asset_ids
        or len(asset_ids) > settings.UPLOAD_LEASE_PAGE_SIZE
        or len(set(asset_ids)) != len(asset_ids)
    ):
        raise IngestionError("invalid_request", "The upload lease selection is invalid.")
    object_query = (
        AssetObject.objects.select_for_update()
        .select_related("asset")
        .filter(
            asset__batch=batch,
            variant=AssetVariant.ORIGINAL.value,
            state__in=(UploadObjectState.RESERVED.value, UploadObjectState.FAILED.value),
        )
    )
    if asset_ids is not None:
        object_query = object_query.filter(asset_id__in=asset_ids)
    objects = list(object_query.order_by("asset_id")[: settings.UPLOAD_LEASE_PAGE_SIZE])
    leases = []
    for upload in objects:
        try:
            lease = object_store.presign_put(
                key=upload.object_key,
                content_length=upload.expected_bytes,
                content_md5=upload.content_md5,
                sha256=upload.asset.sha256,
                content_type=upload.content_type,
                expires_in_seconds=settings.UPLOAD_LEASE_TTL_SECONDS,
            )
        except ObjectStoreError as exc:
            raise IngestionError(
                "object_store_unavailable",
                "Upload leases are temporarily unavailable.",
                retryable=True,
            ) from exc
        upload.state = UploadObjectState.RESERVED.value
        upload.failure_code = ""
        upload.lease_expires_at = lease.expires_at
        upload.save(update_fields=("state", "failure_code", "lease_expires_at", "updated_at"))
        leases.append(
            {
                "asset_id": str(upload.asset_id),
                "variant": upload.variant,
                "url": lease.url,
                "headers": lease.headers,
                "expires_at": lease.expires_at.isoformat(),
            }
        )
    return leases


def verify_uploaded_object(
    *,
    session: DesktopSession,
    event_id: UUID,
    asset_id: UUID,
    object_store: S3ObjectStore,
    request=None,
) -> AssetObject:
    event = event_for_session(session, event_id)
    upload = _manageable_upload(
        session=session,
        event=event,
        asset_id=asset_id,
    )
    if upload.state == UploadObjectState.VERIFIED.value:
        return upload
    if upload.state == UploadObjectState.EXCLUDED.value:
        _delete_unverified_object(object_store, upload.object_key)
        raise IngestionError("asset_excluded", "The asset was explicitly excluded.")
    try:
        head = object_store.head(upload.object_key)
    except ObjectStoreError as exc:
        raise IngestionError(
            "object_store_unavailable",
            "The uploaded object could not be inspected; retry verification.",
            retryable=True,
        ) from exc
    if head is None:
        raise IngestionError(
            "upload_missing", "The uploaded object is not present.", retryable=True
        )
    mismatch = ""
    with transaction.atomic():
        locked_event = Event.objects.select_for_update().get(pk=event.pk)
        locked_batch = ContributionBatch.objects.select_for_update().get(pk=upload.asset.batch_id)
        locked = (
            AssetObject.objects.select_for_update()
            .select_related(
                "asset",
                "asset__batch",
                "asset__batch__installation",
                "asset__batch__installation__event",
            )
            .get(pk=upload.pk)
        )
        if locked.state == UploadObjectState.VERIFIED.value:
            return locked
        if locked.state == UploadObjectState.EXCLUDED.value:
            _delete_unverified_object(object_store, locked.object_key)
            raise IngestionError("asset_excluded", "The asset was explicitly excluded.")
        mismatch = _object_mismatch(locked, head)
        if mismatch:
            _delete_unverified_object(object_store, locked.object_key)
            locked.state = UploadObjectState.FAILED.value
            locked.failure_code = mismatch
            locked.lease_expires_at = None
            locked.save(update_fields=("state", "failure_code", "lease_expires_at", "updated_at"))
        else:
            now = timezone.now()
            locked.state = UploadObjectState.VERIFIED.value
            locked.etag = head.etag
            locked.verified_at = now
            locked.failure_code = ""
            locked.save(
                update_fields=("state", "etag", "verified_at", "failure_code", "updated_at")
            )
            locked_event.verified_original_bytes += locked.expected_bytes
            locked_event.verified_original_count += 1
            locked_event.save(
                update_fields=(
                    "verified_original_bytes",
                    "verified_original_count",
                    "updated_at",
                )
            )
            _complete_batch_if_terminal(locked_batch, now=now)
    if mismatch:
        raise IngestionError(
            mismatch,
            "The uploaded object did not match its immutable manifest.",
        )
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=session.user,
        event_installation=upload.asset.batch.installation,
        action=AuditAction.ASSET_UPLOAD_VERIFIED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"asset_id": str(upload.asset_id), "variant": upload.variant},
    )
    return locked


def publication_status(event: Event) -> dict:
    included_assets = Asset.objects.filter(
        batch__installation__event=event,
        batch__state=ContributionState.COMPLETE.value,
        batch__sub_event__is_archived=False,
        gallery_excluded_at__isnull=True,
        variant_objects__variant=AssetVariant.ORIGINAL.value,
        variant_objects__state=UploadObjectState.VERIFIED.value,
    )
    included_asset_count = included_assets.count()
    included_asset_ids = included_assets.values("id")
    expected_derivatives = included_asset_count * len(DERIVATIVE_VARIANTS)
    verified_derivatives = AssetObject.objects.filter(
        asset_id__in=included_asset_ids,
        variant__in=tuple(variant.value for variant in DERIVATIVE_VARIANTS),
        state=UploadObjectState.VERIFIED.value,
    ).count()
    terminal_faces = FaceAnalysis.objects.filter(
        asset_id__in=included_asset_ids,
        state__in=(FaceAnalysisState.INDEXED, FaceAnalysisState.NO_USABLE_FACE),
        source_sha256=F("asset__sha256"),
        model_id=event.face_model_id,
    ).count()
    in_flight_by_installation: dict[object, dict] = {}
    in_flight_batches = ContributionBatch.objects.filter(
        installation__event=event,
        state=ContributionState.RESERVED.value,
    ).select_related("installation")
    in_flight_batches = in_flight_batches.annotate(
        remaining_photo_count=Count(
            "assets__variant_objects",
            filter=(
                Q(assets__variant_objects__variant=AssetVariant.ORIGINAL.value)
                & ~Q(
                    assets__variant_objects__state__in=(
                        UploadObjectState.VERIFIED.value,
                        UploadObjectState.EXCLUDED.value,
                    )
                )
            ),
        )
    )
    for batch in in_flight_batches:
        entry = in_flight_by_installation.setdefault(
            batch.installation_id,
            {"installation": batch.installation, "photo_count": 0},
        )
        entry["photo_count"] += max(batch.remaining_photo_count, 1)
    has_policy = PreviewPolicy.objects.filter(event=event).exists()
    has_active_sub_event = event.sub_events.filter(is_archived=False).exists()
    return {
        "included_photo_count": included_asset_count,
        "missing_derivative_count": expected_derivatives - verified_derivatives,
        "missing_face_count": included_asset_count - terminal_faces,
        "in_flight": tuple(in_flight_by_installation.values()),
        "ready": (
            bool(included_asset_count)
            and expected_derivatives == verified_derivatives
            and included_asset_count == terminal_faces
            and has_policy
            and has_active_sub_event
        ),
    }


@transaction.atomic
def commit_publication_snapshot(
    *,
    event_id: UUID,
    actor,
    object_store: S3ObjectStore,
    request=None,
) -> tuple[Event, IngestionManifest]:
    event = Event.objects.select_for_update().select_related("photographer").get(pk=event_id)
    if event.state == EventState.PUBLISHED.value:
        if event.current_ingestion_manifest is None:
            raise IngestionError(
                "publication_snapshot_missing",
                "The published event has no committed publication snapshot.",
            )
        return event, event.current_ingestion_manifest
    if event.state in {
        EventState.ARCHIVED.value,
        EventState.CANCELLED.value,
        EventState.FAILED.value,
    }:
        raise IngestionError("event_not_publishable", "The event cannot be published now.")

    event.intake_state = IntakeState.CLOSED.value
    status = publication_status(event)
    if not status["included_photo_count"]:
        raise IngestionError(
            "empty_manifest",
            "Complete at least one photo before publishing this event.",
        )
    if status["missing_derivative_count"]:
        raise IngestionError(
            "derivatives_required",
            f"{status['missing_derivative_count']} required preview or thumbnail is unfinished.",
        )
    if status["missing_face_count"]:
        raise IngestionError(
            "face_index_required",
            f"{status['missing_face_count']} completed photo still needs face analysis.",
        )
    if not PreviewPolicy.objects.filter(event=event).exists():
        raise IngestionError(
            "preview_policy_required",
            "Submit a completed batch before publishing so preview settings can be locked.",
        )
    if not event.sub_events.filter(is_archived=False).exists():
        raise IngestionError(
            "sub_event_required",
            "Create and retain at least one active sub-event before publication.",
        )
    if IngestionManifest.objects.filter(event=event, generation=event.intake_generation).exists():
        raise IngestionError(
            "publication_snapshot_exists",
            "Unpublish the event before creating another publication snapshot.",
        )

    incomplete_batches = list(
        ContributionBatch.objects.select_for_update().filter(
            installation__event=event,
            state=ContributionState.RESERVED.value,
        )
    )
    if incomplete_batches:
        incomplete_ids = [batch.id for batch in incomplete_batches]
        excluded_objects = list(
            AssetObject.objects.select_for_update()
            .select_related("asset")
            .filter(asset__batch_id__in=incomplete_ids)
        )
        originals = [
            upload
            for upload in excluded_objects
            if upload.variant == AssetVariant.ORIGINAL.value
            and upload.state != UploadObjectState.EXCLUDED.value
        ]
        event.reserved_original_bytes -= sum(upload.expected_bytes for upload in originals)
        event.reserved_original_count -= len(originals)
        verified_originals = [
            upload for upload in originals if upload.state == UploadObjectState.VERIFIED.value
        ]
        event.verified_original_bytes -= sum(upload.expected_bytes for upload in verified_originals)
        event.verified_original_count -= len(verified_originals)
        FaceAnalysis.objects.filter(asset__batch_id__in=incomplete_ids).delete()
        AssetObject.objects.filter(asset__batch_id__in=incomplete_ids).update(
            state=UploadObjectState.EXCLUDED.value,
            lease_expires_at=None,
            failure_code="",
            excluded_reason="Event published before this contribution completed.",
            excluded_by=actor,
            updated_at=timezone.now(),
        )
        ContributionBatch.objects.filter(pk__in=incomplete_ids).update(
            state=ContributionState.NOT_INCLUDED.value,
            updated_at=timezone.now(),
        )

    completed_batch_ids = list(
        ContributionBatch.objects.filter(
            installation__event=event,
            state=ContributionState.COMPLETE.value,
            sub_event__is_archived=False,
        ).values_list("id", flat=True)
    )
    document = _aggregate_manifest_document(event, batch_ids=completed_batch_ids)
    content = _canonical_json(document)
    content_md5 = base64.b64encode(hashlib.md5(content, usedforsecurity=False).digest()).decode()
    object_key = ingestion_manifest_key(event.id, event.intake_generation)
    content_sha256 = hashlib.sha256(content).hexdigest()
    try:
        object_store.put_immutable(
            key=object_key,
            body=content,
            content_length=len(content),
            content_md5=content_md5,
            sha256=content_sha256,
            content_type="application/json",
        )
    except ObjectAlreadyExists:
        try:
            head = object_store.head(object_key)
        except ObjectStoreError as exc:
            raise IngestionError(
                "object_store_unavailable",
                "The publication snapshot could not be verified; try publishing again.",
                retryable=True,
            ) from exc
        if (
            head is None
            or head.content_length != len(content)
            or head.metadata.get("openfotos-sha256") != content_sha256
        ):
            raise IngestionError(
                "manifest_object_conflict",
                "The immutable publication snapshot contains different content.",
            ) from None
    except ObjectStoreError as exc:
        raise IngestionError(
            "object_store_unavailable",
            "The publication snapshot could not be stored; try publishing again.",
            retryable=True,
        ) from exc

    committed_at = timezone.now()
    manifest = IngestionManifest.objects.create(
        event=event,
        generation=event.intake_generation,
        object_key=object_key,
        content_sha256=content_sha256,
        document=document,
        asset_count=document["summary"]["verified_asset_count"],
        original_bytes=document["summary"]["verified_original_bytes"],
        excluded_asset_count=document["summary"]["excluded_asset_count"],
        state=IngestionManifestState.COMMITTED.value,
        committed_at=committed_at,
    )
    event.current_ingestion_manifest = manifest
    event.derivatives_ready_generation = event.intake_generation
    event.face_index_ready_generation = event.intake_generation
    event.state = EventState.PUBLISHED.value
    event.save(
        update_fields=(
            "intake_state",
            "current_ingestion_manifest",
            "derivatives_ready_generation",
            "face_index_ready_generation",
            "reserved_original_bytes",
            "verified_original_bytes",
            "reserved_original_count",
            "verified_original_count",
            "state",
            "updated_at",
        )
    )
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=actor,
        action=AuditAction.EVENT_PUBLICATION_SNAPSHOT,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={
            "manifest_id": str(manifest.id),
            "generation": manifest.generation,
            "asset_count": manifest.asset_count,
            "excluded_batch_count": len(incomplete_batches),
        },
    )
    return event, manifest


@transaction.atomic
def close_intake(*, session: DesktopSession, event_id: UUID, request=None) -> Event:
    """Internal generation control retained for reconciliation; not exposed by the API."""
    event = _photographer_event(session, event_id)
    locked = Event.objects.select_for_update().get(pk=event.pk)
    if locked.intake_state == IntakeState.CLOSED.value:
        return locked
    if locked.state not in {EventState.DRAFT.value, EventState.UPLOADING.value}:
        raise IngestionError("event_not_uploading", "The event intake cannot be closed now.")
    locked.intake_state = IntakeState.CLOSED.value
    locked.save(update_fields=("intake_state", "updated_at"))
    record_audit(
        photographer=locked.photographer,
        event=locked,
        actor=session.user,
        action=AuditAction.EVENT_INTAKE_CLOSED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"generation": locked.intake_generation},
    )
    return locked


@transaction.atomic
def revoke_installation(
    *, session: DesktopSession, event_id: UUID, installation_id: UUID, request=None
) -> EventInstallation:
    event = _photographer_event(session, event_id)
    try:
        installation = EventInstallation.objects.select_for_update().get(
            pk=installation_id, event=event
        )
    except EventInstallation.DoesNotExist as exc:
        raise IngestionError("installation_not_found", "The workstation is unavailable.") from exc
    if installation.status == InstallationStatus.REVOKED.value:
        return installation
    now = timezone.now()
    installation.status = InstallationStatus.REVOKED.value
    installation.revoked_at = now
    installation.save(update_fields=("status", "revoked_at"))
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=session.user,
        event_installation=installation,
        action=AuditAction.EVENT_INSTALLATION_REVOKED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"installation_id": str(installation.id)},
    )
    return installation


def cancel_batch(
    *,
    session: DesktopSession,
    event_id: UUID,
    batch_id: UUID,
    object_store: S3ObjectStore,
    request=None,
) -> ContributionBatch:
    event = _photographer_event(session, event_id)
    with transaction.atomic():
        locked_event = Event.objects.select_for_update().get(pk=event.pk)
        try:
            locked = (
                ContributionBatch.objects.select_for_update()
                .select_related("installation")
                .get(pk=batch_id, installation__event=locked_event)
            )
        except ContributionBatch.DoesNotExist as exc:
            raise IngestionError("batch_not_found", "The contribution is unavailable.") from exc
        if locked.state == ContributionState.CANCELLED.value:
            return locked
        uploads = list(
            AssetObject.objects.select_for_update().filter(asset__batch=locked).order_by("pk")
        )
        if any(upload.state == UploadObjectState.VERIFIED.value for upload in uploads):
            raise IngestionError(
                "batch_has_verified_assets",
                "A contribution with verified originals cannot be cancelled.",
            )
        now = timezone.now()
        if any(
            upload.lease_expires_at is not None and upload.lease_expires_at > now
            for upload in uploads
        ):
            raise IngestionError(
                "upload_lease_active",
                "Wait for active upload leases to expire before cancelling this contribution.",
                retryable=True,
            )
        for upload in uploads:
            _delete_unverified_object(object_store, upload.object_key)
        releasable = sum(
            upload.expected_bytes
            for upload in uploads
            if upload.state != UploadObjectState.EXCLUDED.value
        )
        releasable_count = sum(
            upload.state != UploadObjectState.EXCLUDED.value for upload in uploads
        )
        AssetObject.objects.filter(asset__batch=locked).exclude(
            state=UploadObjectState.EXCLUDED.value
        ).update(
            state=UploadObjectState.EXCLUDED.value,
            excluded_reason="Contribution cancelled by the photographer.",
            excluded_by=session.user,
            failure_code="",
            updated_at=now,
        )
        locked.state = ContributionState.CANCELLED.value
        locked.cancelled_at = now
        locked.save(update_fields=("state", "cancelled_at", "updated_at"))
        locked_event.reserved_original_bytes -= releasable
        locked_event.reserved_original_count -= releasable_count
        locked_event.save(
            update_fields=(
                "reserved_original_bytes",
                "reserved_original_count",
                "updated_at",
            )
        )
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=session.user,
        event_installation=locked.installation,
        action=AuditAction.CONTRIBUTION_CANCELLED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={
            "batch_id": str(locked.id),
            "released_original_bytes": releasable,
            "released_original_count": releasable_count,
        },
    )
    return locked


@transaction.atomic
def reopen_intake(*, session: DesktopSession, event_id: UUID, request=None) -> Event:
    """Internal generation control retained for reconciliation; not exposed by the API."""
    event = _photographer_event(session, event_id)
    locked = Event.objects.select_for_update().get(pk=event.pk)
    if locked.intake_state == IntakeState.OPEN.value:
        return locked
    try:
        reopened_state = state_for_reopened_intake(EventState(locked.state))
    except LifecycleViolation as exc:
        raise IngestionError(exc.code, str(exc)) from exc
    previous_generation = locked.intake_generation
    locked.intake_generation += 1
    locked.intake_state = IntakeState.OPEN.value
    locked.current_ingestion_manifest = None
    locked.derivatives_ready_generation = None
    locked.face_index_ready_generation = None
    locked.state = reopened_state.value
    locked.save(
        update_fields=(
            "intake_generation",
            "intake_state",
            "current_ingestion_manifest",
            "derivatives_ready_generation",
            "face_index_ready_generation",
            "state",
            "updated_at",
        )
    )
    record_audit(
        photographer=locked.photographer,
        event=locked,
        actor=session.user,
        action=AuditAction.EVENT_INTAKE_REOPENED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={
            "from_generation": previous_generation,
            "to_generation": locked.intake_generation,
        },
    )
    return locked


def exclude_asset(
    *,
    session: DesktopSession,
    event_id: UUID,
    asset_id: UUID,
    reason: str,
    object_store: S3ObjectStore,
    request=None,
) -> AssetObject:
    event = _photographer_event(session, event_id)
    normalized_reason = reason.strip()
    if not 1 <= len(normalized_reason) <= 240 or any(
        ord(character) < 32 for character in normalized_reason
    ):
        raise IngestionError("invalid_exclusion_reason", "An exclusion reason is required.")
    with transaction.atomic():
        locked_event = Event.objects.select_for_update().get(pk=event.pk)
        try:
            batch_id = (
                Asset.objects.only("batch_id")
                .get(
                    pk=asset_id,
                    batch__installation__event=locked_event,
                )
                .batch_id
            )
        except Asset.DoesNotExist as exc:
            raise IngestionError("asset_not_found", "The asset is unavailable.") from exc
        locked_batch = ContributionBatch.objects.select_for_update().get(pk=batch_id)
        locked = (
            AssetObject.objects.select_for_update()
            .select_related("asset__batch")
            .get(
                asset_id=asset_id,
                variant=AssetVariant.ORIGINAL.value,
            )
        )
        if locked.state == UploadObjectState.VERIFIED.value:
            raise IngestionError("asset_immutable", "A verified original cannot be excluded.")
        if locked.state == UploadObjectState.EXCLUDED.value:
            return locked
        if locked.lease_expires_at is not None and locked.lease_expires_at > timezone.now():
            raise IngestionError(
                "upload_lease_active",
                "Wait for the active upload lease to expire before excluding this asset.",
                retryable=True,
            )
        _delete_unverified_object(object_store, locked.object_key)
        locked.state = UploadObjectState.EXCLUDED.value
        locked.excluded_reason = normalized_reason
        locked.excluded_by = session.user
        locked.failure_code = ""
        locked.save(
            update_fields=(
                "state",
                "excluded_reason",
                "excluded_by",
                "failure_code",
                "updated_at",
            )
        )
        locked_event.reserved_original_bytes -= locked.expected_bytes
        locked_event.reserved_original_count -= 1
        locked_event.save(
            update_fields=(
                "reserved_original_bytes",
                "reserved_original_count",
                "updated_at",
            )
        )
        _complete_batch_if_terminal(locked_batch, now=timezone.now())
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=session.user,
        event_installation=locked.asset.batch.installation,
        action=AuditAction.ASSET_EXCLUDED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"asset_id": str(asset_id)},
    )
    return locked


def finalize_ingestion(
    *,
    session: DesktopSession,
    event_id: UUID,
    object_store: S3ObjectStore,
    request=None,
) -> IngestionManifest:
    """Commit an internal generation manifest; the desktop API no longer exposes this step."""
    from .derivative_services import refresh_derivative_readiness
    from .face_services import refresh_face_index_readiness

    event = _photographer_event(session, event_id)
    with transaction.atomic():
        locked_event = Event.objects.select_for_update().get(pk=event.pk)
        if locked_event.intake_state != IntakeState.CLOSED.value:
            raise IngestionError("intake_open", "Close event intake before finalization.")
        if locked_event.state == EventState.PROCESSING.value:
            current = locked_event.current_ingestion_manifest
            if current and current.generation == locked_event.intake_generation:
                transaction.on_commit(lambda: refresh_derivative_readiness(locked_event.id))
                transaction.on_commit(lambda: refresh_face_index_readiness(locked_event.id))
                return current
        try:
            finalized_state = state_for_finalized_ingestion(EventState(locked_event.state))
        except LifecycleViolation as exc:
            raise IngestionError(exc.code, str(exc)) from exc
        nonterminal = AssetObject.objects.filter(
            asset__batch__installation__event=locked_event,
            variant=AssetVariant.ORIGINAL.value,
        ).exclude(state__in=(UploadObjectState.VERIFIED.value, UploadObjectState.EXCLUDED.value))
        if nonterminal.exists():
            raise IngestionError(
                "contributions_not_terminal",
                "Every reserved original must be verified or explicitly excluded.",
            )
        if not AssetObject.objects.filter(
            asset__batch__installation__event=locked_event,
            variant=AssetVariant.ORIGINAL.value,
            state=UploadObjectState.VERIFIED.value,
        ).exists():
            raise IngestionError("empty_manifest", "At least one verified original is required.")
        manifest = IngestionManifest.objects.filter(
            event=locked_event,
            generation=locked_event.intake_generation,
        ).first()
        if manifest is None:
            document = _aggregate_manifest_document(locked_event)
            content = _canonical_json(document)
            manifest = IngestionManifest.objects.create(
                event=locked_event,
                generation=locked_event.intake_generation,
                object_key=ingestion_manifest_key(locked_event.id, locked_event.intake_generation),
                content_sha256=hashlib.sha256(content).hexdigest(),
                document=document,
                asset_count=document["summary"]["verified_asset_count"],
                original_bytes=document["summary"]["verified_original_bytes"],
                excluded_asset_count=document["summary"]["excluded_asset_count"],
            )
    content = _canonical_json(manifest.document)
    content_md5 = base64.b64encode(hashlib.md5(content, usedforsecurity=False).digest()).decode()
    try:
        object_store.put_immutable(
            key=manifest.object_key,
            body=content,
            content_length=len(content),
            content_md5=content_md5,
            sha256=manifest.content_sha256,
            content_type="application/json",
        )
    except ObjectAlreadyExists:
        try:
            head = object_store.head(manifest.object_key)
        except ObjectStoreError as exc:
            raise IngestionError(
                "object_store_unavailable",
                "The existing manifest could not be verified; retry finalization.",
                retryable=True,
            ) from exc
        if (
            head is None
            or head.content_length != len(content)
            or head.metadata.get("openfotos-sha256") != manifest.content_sha256
        ):
            raise IngestionError(
                "manifest_object_conflict",
                "The immutable manifest key contains different content.",
            ) from None
    except ObjectStoreError as exc:
        raise IngestionError(
            "object_store_unavailable",
            "The manifest could not be stored; retry finalization.",
            retryable=True,
        ) from exc
    with transaction.atomic():
        locked_event = Event.objects.select_for_update().get(pk=event.pk)
        locked_manifest = IngestionManifest.objects.select_for_update().get(pk=manifest.pk)
        if (
            locked_event.intake_state != IntakeState.CLOSED.value
            or locked_event.intake_generation != locked_manifest.generation
        ):
            raise IngestionError(
                "stale_intake_generation",
                "Intake changed while finalization was running; finalize the current generation.",
            )
        now = timezone.now()
        locked_manifest.state = IngestionManifestState.COMMITTED.value
        locked_manifest.committed_at = now
        locked_manifest.save(update_fields=("state", "committed_at"))
        locked_event.current_ingestion_manifest = locked_manifest
        locked_event.state = finalized_state.value
        locked_event.save(update_fields=("current_ingestion_manifest", "state", "updated_at"))
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=session.user,
        action=AuditAction.EVENT_INGESTION_FINALIZED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"manifest_id": str(locked_manifest.id), "generation": locked_manifest.generation},
    )
    refresh_derivative_readiness(event.id)
    refresh_face_index_readiness(event.id)
    return locked_manifest


def _contribution_installation(
    *, session: DesktopSession, event: Event, label: str, request=None
) -> EventInstallation:
    try:
        return register_event_installation(
            session=session,
            event=event,
            label=label,
            request=request,
        )
    except DesktopAuthError as exc:
        raise IngestionError(exc.code, str(exc)) from exc


def _photographer_event(session: DesktopSession, event_id: UUID) -> Event:
    return event_for_session(session, event_id)


def _owned_batch(
    *,
    session: DesktopSession,
    event: Event,
    batch_id: UUID,
    for_update: bool = False,
) -> ContributionBatch:
    query = ContributionBatch.objects.select_related("installation", "sub_event")
    if for_update:
        query = query.select_for_update()
    try:
        batch = query.get(
            pk=batch_id,
            installation__event=event,
        )
    except ContributionBatch.DoesNotExist as exc:
        raise IngestionError("batch_not_found", "The contribution is unavailable.") from exc
    installation = _existing_contribution_installation(session=session, event=event)
    if batch.installation_id != installation.id:
        raise IngestionError("batch_not_found", "The contribution is unavailable.")
    if batch.sub_event.is_archived:
        raise IngestionError("sub_event_archived", "The sub-event is archived.")
    return batch


def _manageable_upload(
    *,
    session: DesktopSession,
    event: Event,
    asset_id: UUID,
) -> AssetObject:
    try:
        upload = AssetObject.objects.select_related(
            "asset__batch__installation", "asset__batch__sub_event"
        ).get(
            asset_id=asset_id,
            asset__batch__installation__event=event,
            variant=AssetVariant.ORIGINAL.value,
        )
    except AssetObject.DoesNotExist as exc:
        raise IngestionError("asset_not_found", "The asset is unavailable.") from exc
    if upload.asset.batch.sub_event.is_archived:
        raise IngestionError("sub_event_archived", "The sub-event is archived.")
    return upload


def _existing_contribution_installation(
    *, session: DesktopSession, event: Event
) -> EventInstallation:
    installation = EventInstallation.objects.filter(
        event=event,
        user=session.user,
        installation_id=session.installation_id,
        status=InstallationStatus.ACTIVE.value,
    ).first()
    if installation is None:
        raise IngestionError("batch_not_found", "The contribution is unavailable.")
    return installation


def _delete_unverified_object(object_store: S3ObjectStore, key: str) -> None:
    try:
        if object_store.head(key) is not None:
            object_store.delete(key)
    except ObjectStoreError as exc:
        raise IngestionError(
            "object_store_unavailable",
            "The unverified object could not be safely removed; retry this operation.",
            retryable=True,
        ) from exc


def _object_mismatch(upload: AssetObject, head) -> str:
    expected_etag = base64.b64decode(upload.content_md5).hex()
    if head.content_length != upload.expected_bytes:
        return "asset_size_mismatch"
    if head.content_type.lower().partition(";")[0] != upload.content_type:
        return "asset_content_type_mismatch"
    if head.metadata.get("openfotos-sha256") != upload.sha256:
        return "asset_checksum_mismatch"
    if head.etag.lower() != expected_etag:
        return "asset_checksum_mismatch"
    return ""


def _complete_batch_if_terminal(batch: ContributionBatch, *, now) -> None:
    remaining = AssetObject.objects.filter(asset__batch=batch).exclude(
        state__in=(UploadObjectState.VERIFIED.value, UploadObjectState.EXCLUDED.value)
    )
    if not remaining.exists():
        ContributionBatch.objects.filter(
            pk=batch.pk,
            state=ContributionState.RESERVED.value,
        ).update(state=ContributionState.COMPLETE.value, completed_at=now, updated_at=now)


def _aggregate_manifest_document(event: Event, *, batch_ids=None) -> dict:
    batches = (
        ContributionBatch.objects.select_related("sub_event")
        .filter(installation__event=event)
        .order_by("created_at", "id")
    )
    if batch_ids is not None:
        batches = batches.filter(pk__in=batch_ids)
    contributions = [
        {
            "batch_id": str(batch.id),
            "installation_id": str(batch.installation_id),
            "sub_event_id": str(batch.sub_event_id),
            "sub_event_name": batch.sub_event.name,
            "generation": batch.intake_generation,
            "state": batch.state,
            "asset_count": batch.declared_asset_count,
            "original_bytes": batch.declared_original_bytes,
            "manifest_sha256": batch.manifest_sha256,
        }
        for batch in batches
    ]
    objects = (
        AssetObject.objects.select_related("asset", "asset__batch")
        .filter(
            asset__batch__installation__event=event,
            variant=AssetVariant.ORIGINAL.value,
        )
        .order_by("asset_id")
    )
    if batch_ids is not None:
        objects = objects.filter(asset__batch_id__in=batch_ids)
    assets = [
        {
            "asset_id": str(upload.asset_id),
            "batch_id": str(upload.asset.batch_id),
            "filename": upload.asset.original_filename,
            "content_type": upload.content_type,
            "width": upload.asset.width,
            "height": upload.asset.height,
            "original_bytes": upload.expected_bytes,
            "sha256": upload.asset.sha256,
            "content_md5": upload.content_md5,
            "object_key": upload.object_key,
            "state": upload.state,
            "excluded_reason": upload.excluded_reason or None,
        }
        for upload in objects
    ]
    totals = objects.aggregate(
        verified_asset_count=Count("id", filter=Q(state=UploadObjectState.VERIFIED.value)),
        verified_original_bytes=Sum(
            "expected_bytes",
            filter=Q(state=UploadObjectState.VERIFIED.value),
            default=0,
        ),
        excluded_asset_count=Count("id", filter=Q(state=UploadObjectState.EXCLUDED.value)),
    )
    return {
        "format": "openfotos-ingestion-manifest-v2",
        "event_id": str(event.id),
        "generation": event.intake_generation,
        "created_at": event.updated_at.isoformat(),
        "processing_profile_id": event.processing_profile_id,
        "summary": totals,
        "contributions": contributions,
        "assets": assets,
    }


def _canonical_json(document: dict) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
