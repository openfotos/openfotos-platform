"""Provider-independent, fail-closed face model contracts."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER_PATTERN = re.compile(r"[a-z0-9][a-z0-9._+-]{0,127}\Z")
MAX_EMBEDDING_DIMENSIONS = 4096


class EmbeddingNormalization(StrEnum):
    L2 = "l2"


class DistanceMetric(StrEnum):
    COSINE = "cosine"


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    filename: str
    sha256: str
    license_id: str

    def __post_init__(self) -> None:
        if (
            not self.filename
            or len(self.filename) > 255
            or "/" in self.filename
            or "\\" in self.filename
        ):
            raise ValueError("model artifact filename must be a basename")
        if not _SHA256_PATTERN.fullmatch(self.sha256):
            raise ValueError("model artifact sha256 must be a lowercase hex digest")
        if not _IDENTIFIER_PATTERN.fullmatch(self.license_id):
            raise ValueError("model artifact license id is invalid")


@dataclass(frozen=True, slots=True)
class ModelRights:
    status: str
    basis: str
    permission_evidence_private: bool

    def __post_init__(self) -> None:
        if not _IDENTIFIER_PATTERN.fullmatch(self.status):
            raise ValueError("model rights status is invalid")
        if (
            not self.basis
            or len(self.basis) > 500
            or any(ord(character) < 32 for character in self.basis)
        ):
            raise ValueError("model rights basis is invalid")
        if not isinstance(self.permission_evidence_private, bool):
            raise ValueError("model rights evidence flag must be a boolean")


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    id: str
    detector: str
    recognizer: str
    embedding_dimensions: int
    artifacts: tuple[ModelArtifact, ...]
    preprocessing: str
    detector_input_size: tuple[int, int]
    rights: ModelRights

    def __post_init__(self) -> None:
        for field, value in (
            ("model id", self.id),
            ("detector id", self.detector),
            ("recognizer id", self.recognizer),
            ("preprocessing id", self.preprocessing),
        ):
            if not _IDENTIFIER_PATTERN.fullmatch(value):
                raise ValueError(f"{field} is invalid")
        if not 1 <= self.embedding_dimensions <= MAX_EMBEDDING_DIMENSIONS:
            raise ValueError("embedding dimensions are outside the supported range")
        if len(self.detector_input_size) != 2 or any(
            isinstance(size, bool) or not isinstance(size, int) or size < 1
            for size in self.detector_input_size
        ):
            raise ValueError("detector input size must contain two positive integers")
        if not self.artifacts:
            raise ValueError("at least one model artifact is required")
        filenames = tuple(artifact.filename for artifact in self.artifacts)
        if len(filenames) != len(set(filenames)):
            raise ValueError("model artifact filenames must be unique")

    @property
    def artifact_sha256(self) -> tuple[str, ...]:
        return tuple(artifact.sha256 for artifact in self.artifacts)


@dataclass(frozen=True, slots=True)
class FaceQualityRules:
    detector_confidence_threshold: float
    minimum_face_size_pixels: int
    maximum_faces_per_photo: int

    def __post_init__(self) -> None:
        if not math.isfinite(self.detector_confidence_threshold) or not (
            0.0 < self.detector_confidence_threshold <= 1.0
        ):
            raise ValueError("detector confidence threshold must be in (0, 1]")
        if self.minimum_face_size_pixels < 1:
            raise ValueError("minimum face size must be positive")
        if not 1 <= self.maximum_faces_per_photo <= 1000:
            raise ValueError("maximum faces per photo is outside the supported range")


@dataclass(frozen=True, slots=True)
class FaceModelContract:
    model: ModelIdentity
    normalization: EmbeddingNormalization
    normalization_tolerance: float
    distance_metric: DistanceMetric
    maximum_distance: float
    quality: FaceQualityRules

    def __post_init__(self) -> None:
        if not math.isfinite(self.normalization_tolerance) or not (
            0.0 < self.normalization_tolerance <= 0.01
        ):
            raise ValueError("normalization tolerance must be in (0, 0.01]")
        if not math.isfinite(self.maximum_distance) or not 0.0 <= self.maximum_distance <= 2.0:
            raise ValueError("cosine distance threshold must be in [0, 2]")

    def validate_result(self, raw: object) -> Embedding:
        if not isinstance(raw, dict):
            raise ValueError("face embedding result must be an object")
        expected_fields = {"model_id", "artifact_sha256", "normalization", "values"}
        if set(raw) != expected_fields:
            raise ValueError("face embedding result fields do not match the contract")
        if raw["model_id"] != self.model.id:
            raise ValueError("face embedding model id does not match the accepted contract")
        artifact_sha256 = raw["artifact_sha256"]
        if not isinstance(artifact_sha256, list) or tuple(artifact_sha256) != (
            self.model.artifact_sha256
        ):
            raise ValueError("face embedding model hash does not match the accepted contract")
        if raw["normalization"] != self.normalization.value:
            raise ValueError("face embedding normalization does not match the accepted contract")
        values = _embedding_values(raw["values"], dimensions=self.model.embedding_dimensions)
        _validate_normalization(
            values,
            normalization=self.normalization,
            tolerance=self.normalization_tolerance,
        )
        return Embedding(values=values, model=self.model)


@dataclass(frozen=True, slots=True)
class Embedding:
    values: tuple[float, ...]
    model: ModelIdentity

    def __post_init__(self) -> None:
        values = _embedding_values(self.values, dimensions=self.model.embedding_dimensions)
        object.__setattr__(self, "values", values)


@dataclass(frozen=True, slots=True)
class DetectedFace:
    embedding: Embedding
    detector_confidence: float
    bounding_box_width: int
    bounding_box_height: int

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.detector_confidence)
            or not 0.0 <= self.detector_confidence <= 1.0
        ):
            raise ValueError("detector confidence must be finite and in [0, 1]")
        if self.bounding_box_width < 1 or self.bounding_box_height < 1:
            raise ValueError("face bounding box dimensions must be positive")


class FaceEngine(Protocol):
    @property
    def model(self) -> ModelIdentity: ...

    @property
    def runtime_versions(self) -> dict[str, str]: ...

    def detect_and_embed(self, image_bytes: bytes) -> Sequence[DetectedFace]: ...


def normalized_embedding(values: Sequence[float], *, model: ModelIdentity) -> Embedding:
    parsed = _embedding_values(values, dimensions=model.embedding_dimensions)
    norm = math.sqrt(math.fsum(value * value for value in parsed))
    if norm == 0.0:
        raise ValueError("embedding norm must be non-zero")
    return Embedding(values=tuple(value / norm for value in parsed), model=model)


def _embedding_values(raw: object, *, dimensions: int) -> tuple[float, ...]:
    if not isinstance(raw, (list, tuple)):
        raise ValueError("embedding values must be a sequence")
    if len(raw) != dimensions:
        raise ValueError("embedding dimensions do not match the model contract")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in raw):
        raise ValueError("embedding values must be numbers")
    values = tuple(float(value) for value in raw)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("embedding values must be finite")
    return values


def _validate_normalization(
    values: tuple[float, ...],
    *,
    normalization: EmbeddingNormalization,
    tolerance: float,
) -> None:
    if normalization is not EmbeddingNormalization.L2:
        raise ValueError("unsupported embedding normalization")
    norm = math.sqrt(math.fsum(value * value for value in values))
    if abs(norm - 1.0) > tolerance:
        raise ValueError("embedding normalization does not match the accepted contract")
