"""Controlled event-media retention purge with exact object verification."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from openfotos_storage.backend import ObjectStoreError, S3ObjectStore

from .audit import record_audit
from .models import (
    Asset,
    AssetObject,
    AuditAction,
    AuditResult,
    ContributionBatch,
    Event,
    FaceAnalysis,
    FaceSearchResultSet,
    GuestCapability,
    IngestionManifest,
    OwnerCapability,
    PortalCapability,
    PreviewPolicy,
)


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

    keys = set(
        AssetObject.objects.filter(asset__batch__installation__event=event).values_list(
            "object_key", flat=True
        )
    )
    keys.update(event.ingestion_manifests.values_list("object_key", flat=True))
    policy = PreviewPolicy.objects.filter(event=event).first()
    if policy is not None and policy.mark_object_key:
        keys.add(policy.mark_object_key)
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

    with transaction.atomic():
        locked = Event.objects.select_for_update().select_related("photographer").get(pk=event.pk)
        if locked.media_purged_at is not None:
            return PurgeResult(locked.id, len(keys), 0)
        if locked.purge_after is None or locked.purge_after > checked_at:
            raise RetentionError(
                "retention_not_due", "The event retention boundary changed before deletion."
            )
        assets = Asset.objects.filter(batch__installation__event=locked)
        asset_count = assets.count()
        FaceSearchResultSet.objects.filter(event=locked).delete()
        GuestCapability.objects.filter(owner__event=locked).delete()
        OwnerCapability.objects.filter(event=locked).delete()
        PortalCapability.objects.filter(event=locked).delete()
        FaceAnalysis.objects.filter(asset__batch__installation__event=locked).delete()
        AssetObject.objects.filter(asset__batch__installation__event=locked).delete()
        locked.current_ingestion_manifest = None
        locked.save(update_fields=("current_ingestion_manifest", "updated_at"))
        IngestionManifest.objects.filter(event=locked).delete()
        PreviewPolicy.objects.filter(event=locked).delete()
        assets.update(
            original_filename="purged",
            sha256="0" * 64,
            captured_at=None,
            gallery_position=None,
            derivative_failure_code="",
            derivative_failure_at=None,
        )
        ContributionBatch.objects.filter(installation__event=locked).update(
            manifest_sha256="0" * 64,
            declared_original_bytes=0,
        )
        locked.reserved_original_bytes = 0
        locked.verified_original_bytes = 0
        locked.derivatives_ready_generation = None
        locked.face_index_ready_generation = None
        locked.share_access_version = uuid4()
        locked.media_purged_at = checked_at
        locked.save(
            update_fields=(
                "reserved_original_bytes",
                "verified_original_bytes",
                "derivatives_ready_generation",
                "face_index_ready_generation",
                "share_access_version",
                "media_purged_at",
                "updated_at",
            )
        )
        record_audit(
            photographer=locked.photographer,
            event=locked,
            action=AuditAction.EVENT_MEDIA_PURGED,
            result=AuditResult.SUCCEEDED,
            metadata={
                "deleted_object_count": len(keys),
                "redacted_asset_count": asset_count,
            },
        )
    return PurgeResult(event.id, len(keys), asset_count)
