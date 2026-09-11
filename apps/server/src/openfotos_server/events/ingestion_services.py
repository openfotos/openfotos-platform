"""Transactional contribution reservation and aggregate reconciliation."""

import base64
import hashlib
import json
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from openfotos_contracts import (
    ContributionInput,
    ContributionState,
    DeviceStatus,
    EventState,
    IngestionManifestState,
    IntakeState,
    UploadObjectState,
)
from openfotos_storage import AssetVariant, asset_key, ingestion_manifest_key
from openfotos_storage.backend import ObjectAlreadyExists, ObjectStoreError, S3ObjectStore

from .audit import record_audit
from .desktop_auth import DesktopAuthError, register_lead_device
from .models import (
    Asset,
    AssetObject,
    AuditAction,
    AuditResult,
    ContributionBatch,
    DesktopSession,
    Event,
    IngestionManifest,
    PhotographerMembership,
    UploaderDevice,
    UploaderInvitation,
)


class IngestionError(ValueError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def visible_events(session: DesktopSession) -> list[Event]:
    if session.user_id is not None:
        return list(
            Event.objects.filter(
                photographer=session.photographer,
                photographer__memberships__user=session.user,
                photographer__memberships__is_active=True,
            ).distinct()
        )
    if session.device is None:
        return []
    return list(Event.objects.filter(pk=session.device.event_id))


def event_for_session(session: DesktopSession, event_id: UUID) -> Event:
    try:
        event = Event.objects.select_related("photographer").get(
            pk=event_id,
            photographer=session.photographer,
        )
    except Event.DoesNotExist as exc:
        raise IngestionError("event_not_found", "The event is unavailable.") from exc
    if session.user_id is not None:
        membership = PhotographerMembership.objects.filter(
            photographer=event.photographer,
            user=session.user,
            is_active=True,
        ).exists()
        if not membership:
            raise IngestionError("event_not_found", "The event is unavailable.")
    elif session.device is None or session.device.event_id != event.id:
        raise IngestionError("event_not_found", "The event is unavailable.")
    return event


@transaction.atomic
def reserve_contribution(
    *,
    session: DesktopSession,
    event_id: UUID,
    contribution: ContributionInput,
    request=None,
) -> ContributionBatch:
    event = event_for_session(session, event_id)
    device = _contribution_device(
        session=session,
        event=event,
        label=contribution.device_label,
    )
    locked_event = Event.objects.select_for_update().get(pk=event.pk)
    manifest_sha256 = hashlib.sha256(contribution.canonical_bytes()).hexdigest()
    existing = ContributionBatch.objects.filter(pk=contribution.batch_id).first()
    if existing is not None:
        if (
            existing.device_id == device.id
            and existing.device.event_id == locked_event.id
            and existing.manifest_sha256 == manifest_sha256
        ):
            return existing
        raise IngestionError("idempotency_conflict", "The contribution ID is already in use.")
    if locked_event.intake_state != IntakeState.OPEN.value:
        raise IngestionError("intake_closed", "The event is not accepting new contributions.")
    if locked_event.state not in {EventState.DRAFT.value, EventState.UPLOADING.value}:
        raise IngestionError("event_not_uploading", "The event is not accepting uploads.")
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
    if Asset.objects.filter(pk__in=(asset.id for asset in contribution.assets)).exists():
        raise IngestionError("idempotency_conflict", "One or more asset IDs are already in use.")

    batch = ContributionBatch.objects.create(
        id=contribution.batch_id,
        device=device,
        intake_generation=locked_event.intake_generation,
        label=contribution.label,
        processing_profile_id=contribution.processing_profile_id,
        declared_asset_count=len(contribution.assets),
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
                object_key=asset_key(locked_event.id, asset.id, AssetVariant.ORIGINAL),
                expected_bytes=item.size_bytes,
                content_md5=item.content_md5,
            )
            for asset, item in zip(assets, contribution.assets, strict=True)
        ]
    )
    locked_event.reserved_original_bytes += contribution.original_bytes
    if locked_event.state == EventState.DRAFT.value:
        locked_event.state = EventState.UPLOADING.value
    locked_event.save(update_fields=("reserved_original_bytes", "state", "updated_at"))
    record_audit(
        photographer=locked_event.photographer,
        event=locked_event,
        actor=session.user,
        uploader_device=device,
        action=AuditAction.CONTRIBUTION_RESERVED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={
            "batch_id": str(batch.id),
            "asset_count": batch.declared_asset_count,
            "original_bytes": batch.declared_original_bytes,
            "generation": batch.intake_generation,
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
    upload = _owned_upload(
        session=session,
        event=event,
        asset_id=asset_id,
        allow_lead=True,
    )
    if upload.state == UploadObjectState.VERIFIED.value:
        return upload
    if upload.state == UploadObjectState.EXCLUDED.value:
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
                "asset", "asset__batch", "asset__batch__device", "asset__batch__device__event"
            )
            .get(pk=upload.pk)
        )
        if locked.state == UploadObjectState.VERIFIED.value:
            return locked
        if locked.state == UploadObjectState.EXCLUDED.value:
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
            locked_event.save(update_fields=("verified_original_bytes", "updated_at"))
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
        uploader_device=upload.asset.batch.device,
        action=AuditAction.ASSET_UPLOAD_VERIFIED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"asset_id": str(upload.asset_id), "variant": upload.variant},
    )
    return locked


@transaction.atomic
def close_intake(*, session: DesktopSession, event_id: UUID, request=None) -> Event:
    event = _lead_event(session, event_id)
    locked = Event.objects.select_for_update().get(pk=event.pk)
    if locked.intake_state == IntakeState.CLOSED.value:
        return locked
    if locked.state not in {EventState.DRAFT.value, EventState.UPLOADING.value}:
        raise IngestionError("event_not_uploading", "The event intake cannot be closed now.")
    now = timezone.now()
    locked.intake_state = IntakeState.CLOSED.value
    locked.save(update_fields=("intake_state", "updated_at"))
    UploaderInvitation.objects.filter(
        event=locked,
        revoked_at__isnull=True,
        closed_at__isnull=True,
    ).update(closed_at=now)
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
def revoke_invitation(
    *, session: DesktopSession, event_id: UUID, invitation_id: UUID, request=None
) -> UploaderInvitation:
    event = _lead_event(session, event_id)
    try:
        invitation = UploaderInvitation.objects.select_for_update().get(
            pk=invitation_id,
            event=event,
        )
    except UploaderInvitation.DoesNotExist as exc:
        raise IngestionError("invitation_not_found", "The invitation is unavailable.") from exc
    if invitation.revoked_at is not None:
        return invitation
    invitation.revoked_at = timezone.now()
    invitation.save(update_fields=("revoked_at",))
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=session.user,
        action=AuditAction.UPLOADER_INVITATION_REVOKED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"invitation_id": str(invitation.id)},
    )
    return invitation


@transaction.atomic
def revoke_device(
    *, session: DesktopSession, event_id: UUID, device_id: UUID, request=None
) -> UploaderDevice:
    event = _lead_event(session, event_id)
    try:
        device = UploaderDevice.objects.select_for_update().get(pk=device_id, event=event)
    except UploaderDevice.DoesNotExist as exc:
        raise IngestionError("device_not_found", "The device is unavailable.") from exc
    if device.status == DeviceStatus.REVOKED.value:
        return device
    now = timezone.now()
    device.status = DeviceStatus.REVOKED.value
    device.revoked_at = now
    device.save(update_fields=("status", "revoked_at"))
    device.sessions.filter(revoked_at__isnull=True).update(revoked_at=now)
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=session.user,
        uploader_device=device,
        action=AuditAction.UPLOADER_DEVICE_REVOKED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"device_id": str(device.id)},
    )
    return device


def cancel_batch(
    *,
    session: DesktopSession,
    event_id: UUID,
    batch_id: UUID,
    object_store: S3ObjectStore,
    request=None,
) -> ContributionBatch:
    event = _lead_event(session, event_id)
    with transaction.atomic():
        locked_event = Event.objects.select_for_update().get(pk=event.pk)
        try:
            locked = (
                ContributionBatch.objects.select_for_update()
                .select_related("device")
                .get(pk=batch_id, device__event=locked_event)
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
        AssetObject.objects.filter(asset__batch=locked).exclude(
            state=UploadObjectState.EXCLUDED.value
        ).update(
            state=UploadObjectState.EXCLUDED.value,
            excluded_reason="Contribution cancelled by the event lead.",
            excluded_by=session.user,
            failure_code="",
            updated_at=now,
        )
        locked.state = ContributionState.CANCELLED.value
        locked.cancelled_at = now
        locked.save(update_fields=("state", "cancelled_at", "updated_at"))
        locked_event.reserved_original_bytes -= releasable
        locked_event.save(update_fields=("reserved_original_bytes", "updated_at"))
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=session.user,
        uploader_device=locked.device,
        action=AuditAction.CONTRIBUTION_CANCELLED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"batch_id": str(locked.id), "released_original_bytes": releasable},
    )
    return locked


@transaction.atomic
def reopen_intake(*, session: DesktopSession, event_id: UUID, request=None) -> Event:
    event = _lead_event(session, event_id)
    locked = Event.objects.select_for_update().get(pk=event.pk)
    if locked.intake_state == IntakeState.OPEN.value:
        return locked
    if locked.state in {
        EventState.PUBLISHED.value,
        EventState.ARCHIVED.value,
        EventState.CANCELLED.value,
    }:
        raise IngestionError("event_not_reopenable", "Published or closed events cannot reopen.")
    previous_generation = locked.intake_generation
    locked.intake_generation += 1
    locked.intake_state = IntakeState.OPEN.value
    locked.current_ingestion_manifest = None
    locked.derivatives_ready_generation = None
    locked.state = EventState.UPLOADING.value
    locked.save(
        update_fields=(
            "intake_generation",
            "intake_state",
            "current_ingestion_manifest",
            "derivatives_ready_generation",
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
    event = _lead_event(session, event_id)
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
                    batch__device__event=locked_event,
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
        locked_event.save(update_fields=("reserved_original_bytes", "updated_at"))
        _complete_batch_if_terminal(locked_batch, now=timezone.now())
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=session.user,
        uploader_device=locked.asset.batch.device,
        action=AuditAction.ASSET_EXCLUDED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"asset_id": str(asset_id), "reason": normalized_reason},
    )
    return locked


def finalize_ingestion(
    *,
    session: DesktopSession,
    event_id: UUID,
    object_store: S3ObjectStore,
    request=None,
) -> IngestionManifest:
    event = _lead_event(session, event_id)
    with transaction.atomic():
        locked_event = Event.objects.select_for_update().get(pk=event.pk)
        if locked_event.intake_state != IntakeState.CLOSED.value:
            raise IngestionError("intake_open", "Close event intake before finalization.")
        if locked_event.state == EventState.PROCESSING.value:
            current = locked_event.current_ingestion_manifest
            if current and current.generation == locked_event.intake_generation:
                return current
        if locked_event.state != EventState.UPLOADING.value:
            raise IngestionError("event_not_uploading", "The event cannot be finalized now.")
        nonterminal = AssetObject.objects.filter(
            asset__batch__device__event=locked_event,
            variant=AssetVariant.ORIGINAL.value,
        ).exclude(state__in=(UploadObjectState.VERIFIED.value, UploadObjectState.EXCLUDED.value))
        if nonterminal.exists():
            raise IngestionError(
                "contributions_not_terminal",
                "Every reserved original must be verified or explicitly excluded.",
            )
        if not AssetObject.objects.filter(
            asset__batch__device__event=locked_event,
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
        locked_event.state = EventState.PROCESSING.value
        locked_event.save(update_fields=("current_ingestion_manifest", "state", "updated_at"))
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=session.user,
        action=AuditAction.EVENT_INGESTION_FINALIZED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={
            "manifest_id": str(locked_manifest.id),
            "generation": locked_manifest.generation,
            "asset_count": locked_manifest.asset_count,
            "original_bytes": locked_manifest.original_bytes,
            "excluded_asset_count": locked_manifest.excluded_asset_count,
        },
    )
    return locked_manifest


def _contribution_device(*, session: DesktopSession, event: Event, label: str) -> UploaderDevice:
    if session.user_id is not None:
        try:
            return register_lead_device(session=session, event=event, label=label)
        except DesktopAuthError as exc:
            raise IngestionError(exc.code, str(exc)) from exc
    if session.device_id is None:
        raise IngestionError("event_not_found", "The event is unavailable.")
    device = UploaderDevice.objects.filter(
        pk=session.device_id,
        event=event,
        status=DeviceStatus.ACTIVE.value,
    ).first()
    if device is None:
        raise IngestionError("event_not_found", "The event is unavailable.")
    return device


def _lead_event(session: DesktopSession, event_id: UUID) -> Event:
    event = event_for_session(session, event_id)
    if session.user_id is None:
        raise IngestionError("lead_required", "Only the event lead may perform this action.")
    return event


def _owned_batch(
    *,
    session: DesktopSession,
    event: Event,
    batch_id: UUID,
    for_update: bool = False,
) -> ContributionBatch:
    query = ContributionBatch.objects.select_related("device")
    if for_update:
        query = query.select_for_update()
    try:
        batch = query.get(
            pk=batch_id,
            device__event=event,
        )
    except ContributionBatch.DoesNotExist as exc:
        raise IngestionError("batch_not_found", "The contribution is unavailable.") from exc
    device = _existing_contribution_device(session=session, event=event)
    if batch.device_id != device.id:
        raise IngestionError("batch_not_found", "The contribution is unavailable.")
    return batch


def _owned_upload(
    *,
    session: DesktopSession,
    event: Event,
    asset_id: UUID,
    allow_lead: bool = False,
) -> AssetObject:
    try:
        upload = AssetObject.objects.select_related("asset__batch__device").get(
            asset_id=asset_id,
            asset__batch__device__event=event,
            variant=AssetVariant.ORIGINAL.value,
        )
    except AssetObject.DoesNotExist as exc:
        raise IngestionError("asset_not_found", "The asset is unavailable.") from exc
    if allow_lead and session.user_id is not None:
        return upload
    device = _existing_contribution_device(session=session, event=event)
    if upload.asset.batch.device_id != device.id:
        raise IngestionError("asset_not_found", "The asset is unavailable.")
    return upload


def _existing_contribution_device(*, session: DesktopSession, event: Event) -> UploaderDevice:
    if session.user_id is not None:
        device = UploaderDevice.objects.filter(
            event=event,
            installation_id=session.installation_id,
            status=DeviceStatus.ACTIVE.value,
        ).first()
        if device is None:
            raise IngestionError("batch_not_found", "The contribution is unavailable.")
        return device
    if session.device_id is None:
        raise IngestionError("event_not_found", "The event is unavailable.")
    device = UploaderDevice.objects.filter(
        pk=session.device_id,
        event=event,
        status=DeviceStatus.ACTIVE.value,
    ).first()
    if device is None:
        raise IngestionError("event_not_found", "The event is unavailable.")
    return device


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
    if head.content_type.lower().partition(";")[0] != "image/jpeg":
        return "asset_content_type_mismatch"
    if head.metadata.get("openfotos-sha256") != upload.asset.sha256:
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


def _aggregate_manifest_document(event: Event) -> dict:
    batches = ContributionBatch.objects.filter(device__event=event).order_by("created_at", "id")
    contributions = [
        {
            "batch_id": str(batch.id),
            "device_id": str(batch.device_id),
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
            asset__batch__device__event=event,
            variant=AssetVariant.ORIGINAL.value,
        )
        .order_by("asset_id")
    )
    assets = [
        {
            "asset_id": str(upload.asset_id),
            "batch_id": str(upload.asset.batch_id),
            "filename": upload.asset.original_filename,
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
        "format": "openfotos-ingestion-manifest-v1",
        "event_id": str(event.id),
        "generation": event.intake_generation,
        "created_at": timezone.now().isoformat(),
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
