"""Strict face-index ingestion, readiness, rebuild, and scoped vector queries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from django.db import transaction
from django.db.models import F, Min
from django.utils import timezone
from pgvector.django import CosineDistance

from openfotos_contracts import (
    AssetVariant,
    EventState,
    IngestionManifestState,
    UploadObjectState,
)
from openfotos_vision import (
    ACCEPTED_FACE_MODEL_CONTRACT,
    Embedding,
    FaceAnalysisDocument,
)

from .audit import record_audit
from .ingestion_services import (
    IngestionError,
    asset_for_processing_session,
    event_for_session,
)
from .models import (
    Asset,
    AssetObject,
    AuditAction,
    AuditResult,
    DesktopSession,
    Event,
    FaceAnalysis,
    FaceAnalysisState,
    FaceEmbedding,
)

_TERMINAL_STATES = (
    FaceAnalysisState.INDEXED,
    FaceAnalysisState.NO_USABLE_FACE,
)
_PROCESSING_CLOSED_STATES = {
    EventState.PUBLISHED.value,
    EventState.ARCHIVED.value,
    EventState.CANCELLED.value,
}


@dataclass(frozen=True, slots=True)
class FaceSearchResult:
    asset_id: UUID
    distance: float


def submit_face_analysis(
    *,
    session: DesktopSession,
    event_id: UUID,
    sub_event_id: UUID,
    document: FaceAnalysisDocument,
    request=None,
) -> FaceAnalysis:
    event = event_for_session(session, event_id)
    conflict = False
    replayed = False
    with transaction.atomic():
        locked_event = Event.objects.select_for_update().get(pk=event.pk)
        _require_processing_open(locked_event)
        asset = asset_for_processing_session(
            session=session,
            event=locked_event,
            asset_id=document.asset_id,
            sub_event_id=sub_event_id,
            for_update=True,
        )
        _require_verified_original(asset)
        _require_matching_document(locked_event, asset, document)

        analysis = FaceAnalysis.objects.select_for_update().filter(asset=asset).first()
        if analysis is not None and analysis.state in _TERMINAL_STATES:
            if analysis.document_sha256 == document.document_sha256:
                replayed = True
            else:
                analysis.state = FaceAnalysisState.CONFLICT
                analysis.detected_face_count = 0
                analysis.usable_face_count = 0
                analysis.failure_code = "face_analysis_conflict"
                analysis.completed_at = None
                analysis.attempt_count = F("attempt_count") + 1
                analysis.save(
                    update_fields=(
                        "state",
                        "detected_face_count",
                        "usable_face_count",
                        "failure_code",
                        "completed_at",
                        "attempt_count",
                        "updated_at",
                    )
                )
                locked_event.face_index_ready_generation = None
                locked_event.save(update_fields=("face_index_ready_generation", "updated_at"))
                conflict = True
        elif analysis is not None and analysis.state == FaceAnalysisState.CONFLICT:
            conflict = True
        elif analysis is not None and analysis.attempt_count >= 5:
            raise IngestionError(
                "face_analysis_attempts_exhausted",
                "Reset this photo before retrying face analysis.",
            )
        elif not replayed:
            created = analysis is None
            if analysis is None:
                analysis = FaceAnalysis(
                    asset=asset,
                    model_id=locked_event.face_model_id,
                    artifact_sha256=list(ACCEPTED_FACE_MODEL_CONTRACT.model.artifact_sha256),
                )
            analysis.embeddings.all().delete()
            analysis.state = document.status.value
            analysis.source_sha256 = document.source_sha256
            analysis.model_id = document.contract.model.id
            analysis.artifact_sha256 = list(document.contract.model.artifact_sha256)
            analysis.document_sha256 = document.document_sha256
            analysis.detected_face_count = document.detected_face_count
            analysis.usable_face_count = document.usable_face_count
            analysis.attempt_count = 1 if created else F("attempt_count") + 1
            analysis.failure_code = ""
            analysis.completed_at = timezone.now()
            analysis.save()
            analysis.refresh_from_db()
            FaceEmbedding.objects.bulk_create(
                [
                    FaceEmbedding(
                        analysis=analysis,
                        event=locked_event,
                        face_ordinal=face.ordinal,
                        detector_confidence=face.detector_confidence,
                        bounding_box_width=face.bounding_box_width,
                        bounding_box_height=face.bounding_box_height,
                        model_id=document.contract.model.id,
                        vector=list(face.embedding.values),
                    )
                    for face in document.faces
                ]
            )
            locked_event.face_index_ready_generation = None
            locked_event.save(update_fields=("face_index_ready_generation", "updated_at"))

    if replayed:
        refresh_face_index_readiness(event.id)
        return analysis

    if conflict:
        record_audit(
            photographer=event.photographer,
            event=event,
            actor=session.user,
            event_installation=asset.batch.installation,
            action=AuditAction.FACE_ANALYSIS_CONFLICT,
            result=AuditResult.DENIED,
            request=request,
            metadata={"asset_id": str(asset.id)},
        )
        raise IngestionError(
            "face_analysis_conflict",
            "This photo has a different stored analysis and needs an explicit reset.",
        )

    record_audit(
        photographer=event.photographer,
        event=event,
        actor=session.user,
        event_installation=asset.batch.installation,
        action=AuditAction.FACE_ANALYSIS_COMPLETED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={
            "asset_id": str(asset.id),
            "status": analysis.state,
            "detected_face_count": analysis.detected_face_count,
            "usable_face_count": analysis.usable_face_count,
        },
    )
    refresh_face_index_readiness(event.id)
    return analysis


def report_face_analysis_failure(
    *,
    session: DesktopSession,
    event_id: UUID,
    sub_event_id: UUID,
    asset_id: UUID,
    code: str,
    request=None,
) -> FaceAnalysis:
    normalized_code = code.strip()
    if not re.fullmatch(r"[a-z0-9_]{1,64}", normalized_code):
        raise IngestionError(
            "invalid_failure_code", "A stable face-analysis failure code is required."
        )
    event = event_for_session(session, event_id)
    with transaction.atomic():
        locked_event = Event.objects.select_for_update().get(pk=event.pk)
        _require_processing_open(locked_event)
        asset = asset_for_processing_session(
            session=session,
            event=locked_event,
            asset_id=asset_id,
            sub_event_id=sub_event_id,
            for_update=True,
        )
        _require_verified_original(asset)
        analysis, _ = FaceAnalysis.objects.select_for_update().get_or_create(
            asset=asset,
            defaults={
                "model_id": locked_event.face_model_id,
                "source_sha256": asset.sha256,
                "artifact_sha256": list(ACCEPTED_FACE_MODEL_CONTRACT.model.artifact_sha256),
            },
        )
        if analysis.state in _TERMINAL_STATES:
            return analysis
        if analysis.state == FaceAnalysisState.CONFLICT:
            raise IngestionError(
                "face_analysis_conflict",
                "This photo needs an explicit face-analysis reset.",
            )
        if analysis.attempt_count >= 5:
            return analysis
        analysis.embeddings.all().delete()
        analysis.state = FaceAnalysisState.FAILED
        analysis.usable_face_count = 0
        analysis.detected_face_count = 0
        analysis.failure_code = normalized_code
        analysis.completed_at = None
        analysis.attempt_count = F("attempt_count") + 1
        analysis.save(
            update_fields=(
                "state",
                "usable_face_count",
                "detected_face_count",
                "failure_code",
                "completed_at",
                "attempt_count",
                "updated_at",
            )
        )
        analysis.refresh_from_db()
        locked_event.face_index_ready_generation = None
        locked_event.save(update_fields=("face_index_ready_generation", "updated_at"))
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=session.user,
        event_installation=asset.batch.installation,
        action=AuditAction.FACE_ANALYSIS_FAILED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"asset_id": str(asset.id), "code": normalized_code},
    )
    return analysis


def reset_face_analysis(*, event: Event, asset_id: UUID, actor, request=None) -> FaceAnalysis:
    with transaction.atomic():
        locked_event = Event.objects.select_for_update().get(pk=event.pk)
        _require_processing_open(locked_event)
        try:
            analysis = (
                FaceAnalysis.objects.select_for_update()
                .select_related("asset__batch__installation")
                .get(asset_id=asset_id, asset__batch__installation__event=locked_event)
            )
        except FaceAnalysis.DoesNotExist as exc:
            raise IngestionError(
                "face_analysis_not_found", "The face analysis is unavailable."
            ) from exc
        analysis.embeddings.all().delete()
        analysis.state = FaceAnalysisState.PENDING
        analysis.source_sha256 = analysis.asset.sha256
        analysis.model_id = locked_event.face_model_id
        analysis.artifact_sha256 = list(ACCEPTED_FACE_MODEL_CONTRACT.model.artifact_sha256)
        analysis.document_sha256 = ""
        analysis.detected_face_count = 0
        analysis.usable_face_count = 0
        analysis.attempt_count = 0
        analysis.failure_code = ""
        analysis.completed_at = None
        analysis.save()
        locked_event.face_index_ready_generation = None
        locked_event.save(update_fields=("face_index_ready_generation", "updated_at"))
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=actor,
        event_installation=analysis.asset.batch.installation,
        action=AuditAction.FACE_ANALYSIS_RESET,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"asset_id": str(asset_id)},
    )
    return analysis


@transaction.atomic
def refresh_face_index_readiness(event_id: UUID) -> bool:
    event = Event.objects.select_for_update().get(pk=event_id)
    manifest = event.current_ingestion_manifest
    if (
        manifest is None
        or manifest.state != IngestionManifestState.COMMITTED.value
        or manifest.generation != event.intake_generation
    ):
        if event.face_index_ready_generation is not None:
            event.face_index_ready_generation = None
            event.save(update_fields=("face_index_ready_generation", "updated_at"))
        return False
    visible_asset_ids = list(
        Asset.objects.filter(
            batch__installation__event=event,
            batch__sub_event__is_archived=False,
            variant_objects__variant=AssetVariant.ORIGINAL.value,
            variant_objects__state=UploadObjectState.VERIFIED.value,
            gallery_excluded_at__isnull=True,
        )
        .distinct()
        .values_list("id", flat=True)
    )
    ready_count = FaceAnalysis.objects.filter(
        asset_id__in=visible_asset_ids,
        state__in=_TERMINAL_STATES,
        source_sha256=F("asset__sha256"),
        model_id=event.face_model_id,
    ).count()
    ready = bool(visible_asset_ids) and ready_count == len(visible_asset_ids)
    generation = event.intake_generation if ready else None
    if event.face_index_ready_generation != generation:
        event.face_index_ready_generation = generation
        event.save(update_fields=("face_index_ready_generation", "updated_at"))
    return ready


def search_face_index(
    *,
    event_id: UUID,
    embedding: Embedding,
    sub_event_id: UUID | None = None,
) -> list[FaceSearchResult]:
    validated = ACCEPTED_FACE_MODEL_CONTRACT.validate_result(
        {
            "model_id": embedding.model.id,
            "artifact_sha256": list(embedding.model.artifact_sha256),
            "normalization": ACCEPTED_FACE_MODEL_CONTRACT.normalization.value,
            "values": list(embedding.values),
        }
    )
    distance = CosineDistance("vector", list(validated.values))
    query = FaceEmbedding.objects.filter(
        event_id=event_id,
        model_id=ACCEPTED_FACE_MODEL_CONTRACT.model.id,
        analysis__state=FaceAnalysisState.INDEXED,
        analysis__asset__batch__installation__event_id=event_id,
        analysis__asset__batch__sub_event__event_id=event_id,
        analysis__asset__batch__sub_event__is_archived=False,
        analysis__asset__gallery_excluded_at__isnull=True,
        analysis__asset__variant_objects__variant=AssetVariant.ORIGINAL.value,
        analysis__asset__variant_objects__state=UploadObjectState.VERIFIED.value,
    )
    if sub_event_id is not None:
        query = query.filter(analysis__asset__batch__sub_event_id=sub_event_id)
    rows = (
        query.values("analysis_id")
        .annotate(best_distance=Min(distance))
        .filter(best_distance__lte=ACCEPTED_FACE_MODEL_CONTRACT.maximum_distance)
        .order_by("best_distance", "analysis_id")
    )
    return [
        FaceSearchResult(asset_id=row["analysis_id"], distance=float(row["best_distance"]))
        for row in rows
    ]


def _require_processing_open(event: Event) -> None:
    if event.state in _PROCESSING_CLOSED_STATES:
        raise IngestionError("event_not_processing", "This event no longer accepts face analysis.")


def _require_verified_original(asset: Asset) -> None:
    verified = AssetObject.objects.filter(
        asset=asset,
        variant=AssetVariant.ORIGINAL.value,
        state=UploadObjectState.VERIFIED.value,
    ).exists()
    if not verified:
        raise IngestionError(
            "original_not_verified", "Verify the immutable original before face analysis."
        )


def _require_matching_document(event: Event, asset: Asset, document: FaceAnalysisDocument) -> None:
    if document.contract != ACCEPTED_FACE_MODEL_CONTRACT or (
        event.face_model_id != document.contract.model.id
    ):
        raise IngestionError(
            "model_contract_mismatch",
            "The face-analysis model does not match the event contract.",
        )
    if asset.sha256 != document.source_sha256:
        raise IngestionError(
            "source_checksum_mismatch",
            "The face-analysis source does not match the uploaded original.",
        )
