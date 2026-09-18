"""Controlled event-media retention purge with exact object verification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import uuid4

from django.db import models, transaction
from django.utils import timezone

from openfotos_contracts import IntakeState
from openfotos_storage.backend import ObjectStoreError, S3ObjectStore

from .audit import record_audit
from .models import (
    Asset,
    AssetObject,
    AuditAction,
    AuditResult,
    ConsentAttestation,
    ContributionBatch,
    Event,
    FaceAnalysis,
    FaceSearchResultSet,
    GuestCapability,
    IngestionManifest,
    OwnerCapability,
    PortalCapability,
    PreviewPolicy,
    SubEvent,
)

_INSTRUCTION_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{2,99}\Z")


class RetentionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PurgeResult:
    event_id: object
    deleted_object_count: int
    redacted_asset_count: int


def purge_event_media(*, event_id, object_store: S3ObjectStore, at=None) -> PurgeResult:
    checked_at = at or timezone.now()
    event = Event.objects.select_related("photographer").filter(pk=event_id).first()
    if event is None:
        raise RetentionError("event_not_found", "The event does not exist.")
    if event.media_purged_at is not None:
        return PurgeResult(event.id, 0, 0)
    if event.purge_after is None or event.purge_after > checked_at:
        raise RetentionError(
            "retention_not_due", "The event has not reached the end of its retention grace period."
        )

    keys = _event_object_keys(event, include_cover=False)
    _delete_and_verify_objects(object_store, keys)

    with transaction.atomic():
        locked = Event.objects.select_for_update().select_related("photographer").get(pk=event.pk)
        if locked.media_purged_at is not None:
            return PurgeResult(locked.id, len(keys), 0)
        if locked.purge_after is None or locked.purge_after > checked_at:
            raise RetentionError(
                "retention_not_due", "The event retention boundary changed before deletion."
            )
        asset_count = _purge_database_records(
            locked,
            checked_at=checked_at,
            erase_event_identity=False,
        )
        record_audit(
            photographer=locked.photographer,
            event=locked,
            action=AuditAction.EVENT_MEDIA_PURGED,
            result=AuditResult.SUCCEEDED,
            metadata={
                "mode": "retention",
                "deleted_object_count": len(keys),
                "redacted_asset_count": asset_count,
            },
        )
    return PurgeResult(event.id, len(keys), asset_count)


def erase_event_for_privacy(
    *,
    event_id,
    instruction_reference: str,
    object_store: S3ObjectStore,
    at=None,
) -> PurgeResult:
    """Quarantine immediately, then irreversibly erase an event before normal retention."""
    checked_at = at or timezone.now()
    if not _INSTRUCTION_REFERENCE.fullmatch(instruction_reference):
        raise RetentionError(
            "invalid_instruction_reference",
            "Use a 3-100 character internal reference containing no spaces or personal data.",
        )
    with transaction.atomic():
        try:
            locked = (
                Event.objects.select_for_update().select_related("photographer").get(pk=event_id)
            )
        except Event.DoesNotExist as exc:
            raise RetentionError("event_not_found", "The event does not exist.") from exc
        if locked.privacy_erased_at is not None:
            return PurgeResult(locked.id, 0, 0)
        if (
            locked.erasure_instruction_reference
            and locked.erasure_instruction_reference != instruction_reference
        ):
            raise RetentionError(
                "erasure_reference_conflict",
                "The event is already quarantined under a different instruction reference.",
            )
        if locked.erasure_requested_at is None:
            locked.state = "cancelled"
            locked.intake_state = IntakeState.CLOSED.value
            locked.share_access_version = uuid4()
            locked.erasure_requested_at = checked_at
            locked.erasure_instruction_reference = instruction_reference
            if locked.expires_at is None or locked.expires_at > checked_at:
                locked.expires_at = checked_at
            locked.save(
                update_fields=(
                    "state",
                    "intake_state",
                    "share_access_version",
                    "erasure_requested_at",
                    "erasure_instruction_reference",
                    "expires_at",
                    "updated_at",
                )
            )
            _delete_visitor_access(locked)
            record_audit(
                photographer=locked.photographer,
                event=locked,
                action=AuditAction.EVENT_CHANGED,
                result=AuditResult.SUCCEEDED,
                metadata={
                    "change": "privacy_erasure_requested",
                    "instruction_reference": instruction_reference,
                },
            )

    active_leases = AssetObject.objects.filter(
        asset__batch__installation__event_id=event_id,
        lease_expires_at__gt=checked_at,
    ).exists()
    if active_leases:
        raise RetentionError(
            "upload_lease_active",
            "The event is quarantined; retry erasure after all upload leases expire.",
        )

    event = Event.objects.select_related("photographer").get(pk=event_id)
    keys = _event_object_keys(event, include_cover=True)
    _delete_and_verify_objects(object_store, keys)
    with transaction.atomic():
        locked = Event.objects.select_for_update().select_related("photographer").get(pk=event_id)
        if locked.privacy_erased_at is not None:
            return PurgeResult(locked.id, len(keys), 0)
        if locked.erasure_instruction_reference != instruction_reference:
            raise RetentionError(
                "erasure_reference_conflict",
                "The erasure instruction changed before deletion completed.",
            )
        asset_count = _purge_database_records(
            locked,
            checked_at=checked_at,
            erase_event_identity=True,
        )
        record_audit(
            photographer=locked.photographer,
            event=locked,
            action=AuditAction.EVENT_MEDIA_PURGED,
            result=AuditResult.SUCCEEDED,
            metadata={
                "mode": "privacy_erasure",
                "instruction_reference": instruction_reference,
                "deleted_object_count": len(keys),
                "redacted_asset_count": asset_count,
            },
        )
    return PurgeResult(event.id, len(keys), asset_count)


def _event_object_keys(event: Event, *, include_cover: bool) -> set[str]:
    keys = set(
        AssetObject.objects.filter(asset__batch__installation__event=event).values_list(
            "object_key", flat=True
        )
    )
    keys.update(event.ingestion_manifests.values_list("object_key", flat=True))
    policy = PreviewPolicy.objects.filter(event=event).first()
    if policy is not None and policy.mark_object_key:
        keys.add(policy.mark_object_key)
    if include_cover and event.cover_object_key:
        keys.add(event.cover_object_key)
    return keys


def _delete_and_verify_objects(object_store: S3ObjectStore, keys: set[str]) -> None:
    for key in sorted(keys):
        try:
            object_store.delete(key)
            if object_store.head(key) is not None:
                raise RetentionError(
                    "object_deletion_unverified",
                    "An event object remained after deletion; the database was not changed.",
                )
        except ObjectStoreError as exc:
            raise RetentionError(
                "object_store_unavailable",
                "Event object deletion could not be verified; the database was not changed.",
            ) from exc


def _delete_visitor_access(event: Event) -> None:
    FaceSearchResultSet.objects.filter(event=event).delete()
    GuestCapability.objects.filter(owner__event=event).delete()
    OwnerCapability.objects.filter(event=event).delete()
    PortalCapability.objects.filter(event=event).delete()


def _purge_database_records(
    event: Event,
    *,
    checked_at,
    erase_event_identity: bool,
) -> int:
    assets = Asset.objects.filter(batch__installation__event=event)
    asset_count = assets.count()
    _delete_visitor_access(event)
    FaceAnalysis.objects.filter(asset__batch__installation__event=event).delete()
    AssetObject.objects.filter(asset__batch__installation__event=event).delete()
    event.current_ingestion_manifest = None
    event.save(update_fields=("current_ingestion_manifest", "updated_at"))
    IngestionManifest.objects.filter(event=event).delete()
    PreviewPolicy.objects.filter(event=event).delete()
    assets.update(
        original_filename="purged",
        sha256="0" * 64,
        captured_at=None,
        gallery_position=None,
        gallery_excluded_at=None,
        gallery_exclusion_reason="",
        gallery_excluded_by=None,
        derivative_failure_code="",
        derivative_failure_at=None,
    )
    batch_updates = {
        "manifest_sha256": "0" * 64,
        "declared_original_bytes": 0,
    }
    if erase_event_identity:
        batch_updates.update(label="", declared_asset_count=0)
    ContributionBatch.objects.filter(installation__event=event).update(**batch_updates)
    event.reserved_original_bytes = 0
    event.verified_original_bytes = 0
    event.reserved_original_count = 0
    event.verified_original_count = 0
    event.derivatives_ready_generation = None
    event.face_index_ready_generation = None
    event.share_access_version = uuid4()
    event.media_purged_at = checked_at
    update_fields = [
        "reserved_original_bytes",
        "verified_original_bytes",
        "reserved_original_count",
        "verified_original_count",
        "derivatives_ready_generation",
        "face_index_ready_generation",
        "share_access_version",
        "media_purged_at",
        "updated_at",
    ]
    if erase_event_identity:
        event.name = "Erased event"
        event.cover_object_key = ""
        event.cover_sha256 = ""
        event.cover_width = None
        event.cover_height = None
        event.first_published_at = None
        event.expires_at = None
        event.purge_after = None
        event.privacy_erased_at = checked_at
        update_fields.extend(
            (
                "name",
                "cover_object_key",
                "cover_sha256",
                "cover_width",
                "cover_height",
                "first_published_at",
                "expires_at",
                "purge_after",
                "privacy_erased_at",
            )
        )
        ConsentAttestation.objects.filter(event=event).delete()
        SubEvent.objects.filter(event=event).update(
            name=models.functions.Concat(
                models.Value("Erased "),
                models.functions.Cast("id", output_field=models.CharField()),
            )
        )
        event.installations.update(
            user=None,
            label="Erased workstation",
            status="revoked",
            revoked_at=checked_at,
        )
    event.save(update_fields=update_fields)
    return asset_count
