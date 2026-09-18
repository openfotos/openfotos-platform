"""Strict per-photo face-analysis documents for desktop-to-server transfer."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from .contracts import DetectedFace, Embedding, FaceModelContract

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
MAX_DETECTED_FACES_PER_PHOTO = 5_000


class FaceAnalysisStatus(StrEnum):
    INDEXED = "indexed"
    NO_USABLE_FACE = "no_usable_face"


class FaceAnalysisDocumentError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class FaceEmbeddingResult:
    ordinal: int
    detector_confidence: float
    bounding_box_width: int
    bounding_box_height: int
    embedding: Embedding

    def as_dict(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "detector_confidence": self.detector_confidence,
            "bounding_box_width": self.bounding_box_width,
            "bounding_box_height": self.bounding_box_height,
            "values": list(self.embedding.values),
        }


@dataclass(frozen=True, slots=True)
class FaceAnalysisDocument:
    asset_id: UUID
    source_sha256: str
    contract: FaceModelContract
    detector_floor: float
    detected_face_count: int
    status: FaceAnalysisStatus
    faces: tuple[FaceEmbeddingResult, ...]

    def __post_init__(self) -> None:
        if not _SHA256_PATTERN.fullmatch(self.source_sha256):
            raise FaceAnalysisDocumentError(
                "invalid_source_checksum", "The face-analysis source checksum is invalid."
            )
        if not math.isfinite(self.detector_floor) or not 0.0 < self.detector_floor <= 1.0:
            raise FaceAnalysisDocumentError(
                "invalid_model_contract", "The detector floor is invalid."
            )
        if not 0 <= self.detected_face_count <= MAX_DETECTED_FACES_PER_PHOTO:
            raise FaceAnalysisDocumentError(
                "invalid_face_counts", "The detected-face count is outside the contract."
            )
        if self.detected_face_count < len(self.faces):
            raise FaceAnalysisDocumentError(
                "invalid_face_counts", "The usable-face count exceeds the detected-face count."
            )
        if len(self.faces) > self.contract.quality.maximum_faces_per_photo:
            raise FaceAnalysisDocumentError(
                "too_many_usable_faces", "The photo exceeds the usable-face safety limit."
            )
        expected_status = (
            FaceAnalysisStatus.INDEXED if self.faces else FaceAnalysisStatus.NO_USABLE_FACE
        )
        if self.status is not expected_status:
            raise FaceAnalysisDocumentError(
                "invalid_terminal_status", "The terminal face-analysis status is inconsistent."
            )
        for expected_ordinal, face in enumerate(self.faces):
            if face.ordinal != expected_ordinal:
                raise FaceAnalysisDocumentError(
                    "invalid_face_ordinal", "Face ordinals must be contiguous and zero-based."
                )
            if face.embedding.model != self.contract.model:
                raise FaceAnalysisDocumentError(
                    "invalid_model_contract", "A face uses a different model contract."
                )
            if (
                face.detector_confidence < self.contract.quality.detector_confidence_threshold
                or min(face.bounding_box_width, face.bounding_box_height)
                < self.contract.quality.minimum_face_size_pixels
            ):
                raise FaceAnalysisDocumentError(
                    "invalid_face_quality", "A persisted face does not meet the quality contract."
                )

    @property
    def usable_face_count(self) -> int:
        return len(self.faces)

    @property
    def document_sha256(self) -> str:
        canonical = json.dumps(
            self.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        return hashlib.sha256(canonical).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "asset_id": str(self.asset_id),
            "source_sha256": self.source_sha256,
            "model_contract": model_contract_dict(
                self.contract, detector_floor=self.detector_floor
            ),
            "detected_face_count": self.detected_face_count,
            "usable_face_count": self.usable_face_count,
            "status": self.status.value,
            "faces": [face.as_dict() for face in self.faces],
        }

    @classmethod
    def from_dict(
        cls,
        raw: object,
        *,
        accepted_contract: FaceModelContract,
        accepted_detector_floor: float,
    ) -> FaceAnalysisDocument:
        expected_fields = {
            "asset_id",
            "source_sha256",
            "model_contract",
            "detected_face_count",
            "usable_face_count",
            "status",
            "faces",
        }
        if not isinstance(raw, dict) or set(raw) != expected_fields:
            raise FaceAnalysisDocumentError(
                "invalid_face_analysis", "Face-analysis fields do not match the contract."
            )
        try:
            asset_id = UUID(raw["asset_id"])
        except (TypeError, ValueError, AttributeError) as exc:
            raise FaceAnalysisDocumentError(
                "invalid_face_analysis", "The face-analysis asset ID is invalid."
            ) from exc
        source_sha256 = raw["source_sha256"]
        if not isinstance(source_sha256, str):
            raise FaceAnalysisDocumentError(
                "invalid_source_checksum", "The face-analysis source checksum is invalid."
            )
        expected_model = model_contract_dict(
            accepted_contract, detector_floor=accepted_detector_floor
        )
        if not _same_json_value(raw["model_contract"], expected_model):
            raise FaceAnalysisDocumentError(
                "model_contract_mismatch",
                "The face-analysis model does not match the accepted contract.",
            )
        detected_face_count = _integer(
            raw["detected_face_count"], field="detected_face_count", minimum=0
        )
        usable_face_count = _integer(raw["usable_face_count"], field="usable_face_count", minimum=0)
        try:
            status = FaceAnalysisStatus(raw["status"])
        except (TypeError, ValueError) as exc:
            raise FaceAnalysisDocumentError(
                "invalid_terminal_status", "The face-analysis terminal status is invalid."
            ) from exc
        raw_faces = raw["faces"]
        if not isinstance(raw_faces, list) or len(raw_faces) != usable_face_count:
            raise FaceAnalysisDocumentError(
                "invalid_face_counts", "The usable-face count does not match the face list."
            )
        faces = tuple(
            _face_from_dict(value, ordinal=ordinal, contract=accepted_contract)
            for ordinal, value in enumerate(raw_faces)
        )
        return cls(
            asset_id=asset_id,
            source_sha256=source_sha256,
            contract=accepted_contract,
            detector_floor=accepted_detector_floor,
            detected_face_count=detected_face_count,
            status=status,
            faces=faces,
        )


def build_face_analysis_document(
    *,
    asset_id: UUID,
    source_sha256: str,
    detected_faces: tuple[DetectedFace, ...],
    contract: FaceModelContract,
    detector_floor: float,
) -> FaceAnalysisDocument:
    usable = tuple(
        face
        for face in detected_faces
        if face.detector_confidence >= contract.quality.detector_confidence_threshold
        and min(face.bounding_box_width, face.bounding_box_height)
        >= contract.quality.minimum_face_size_pixels
    )
    faces = tuple(
        FaceEmbeddingResult(
            ordinal=ordinal,
            detector_confidence=face.detector_confidence,
            bounding_box_width=face.bounding_box_width,
            bounding_box_height=face.bounding_box_height,
            embedding=face.embedding,
        )
        for ordinal, face in enumerate(usable)
    )
    return FaceAnalysisDocument(
        asset_id=asset_id,
        source_sha256=source_sha256,
        contract=contract,
        detector_floor=detector_floor,
        detected_face_count=len(detected_faces),
        status=FaceAnalysisStatus.INDEXED if faces else FaceAnalysisStatus.NO_USABLE_FACE,
        faces=faces,
    )


def model_contract_dict(contract: FaceModelContract, *, detector_floor: float) -> dict[str, object]:
    return {
        "model_id": contract.model.id,
        "artifact_sha256": list(contract.model.artifact_sha256),
        "preprocessing": contract.model.preprocessing,
        "detector_input_size": list(contract.model.detector_input_size),
        "detector_floor": detector_floor,
        "embedding_dimensions": contract.model.embedding_dimensions,
        "normalization": contract.normalization.value,
        "normalization_tolerance": contract.normalization_tolerance,
        "distance_metric": contract.distance_metric.value,
        "maximum_distance": contract.maximum_distance,
        "quality": {
            "detector_confidence_threshold": contract.quality.detector_confidence_threshold,
            "minimum_face_size_pixels": contract.quality.minimum_face_size_pixels,
            "maximum_faces_per_photo": contract.quality.maximum_faces_per_photo,
        },
    }


def _face_from_dict(
    raw: object, *, ordinal: int, contract: FaceModelContract
) -> FaceEmbeddingResult:
    expected_fields = {
        "ordinal",
        "detector_confidence",
        "bounding_box_width",
        "bounding_box_height",
        "values",
    }
    if not isinstance(raw, dict) or set(raw) != expected_fields:
        raise FaceAnalysisDocumentError(
            "invalid_face_analysis", "Face result fields do not match the contract."
        )
    parsed_ordinal = _integer(raw["ordinal"], field="ordinal", minimum=0)
    if parsed_ordinal != ordinal:
        raise FaceAnalysisDocumentError(
            "invalid_face_ordinal", "Face ordinals must be contiguous and zero-based."
        )
    confidence = raw["detector_confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise FaceAnalysisDocumentError(
            "invalid_face_quality", "Detector confidence must be numeric."
        )
    confidence = float(confidence)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise FaceAnalysisDocumentError(
            "invalid_face_quality", "Detector confidence must be finite and in [0, 1]."
        )
    width = _integer(raw["bounding_box_width"], field="bounding_box_width", minimum=1)
    height = _integer(raw["bounding_box_height"], field="bounding_box_height", minimum=1)
    try:
        embedding = contract.validate_result(
            {
                "model_id": contract.model.id,
                "artifact_sha256": list(contract.model.artifact_sha256),
                "normalization": contract.normalization.value,
                "values": raw["values"],
            }
        )
    except ValueError as exc:
        raise FaceAnalysisDocumentError("invalid_embedding", str(exc)) from exc
    return FaceEmbeddingResult(
        ordinal=parsed_ordinal,
        detector_confidence=confidence,
        bounding_box_width=width,
        bounding_box_height=height,
        embedding=embedding,
    )


def _integer(raw: object, *, field: str, minimum: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < minimum:
        raise FaceAnalysisDocumentError(
            "invalid_face_analysis", f"{field} must be an integer of at least {minimum}."
        )
    return raw


def _same_json_value(actual: Any, expected: Any) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(actual) == set(expected) and all(
            _same_json_value(actual[key], expected[key]) for key in expected
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _same_json_value(left, right) for left, right in zip(actual, expected, strict=True)
        )
    return bool(actual == expected)
