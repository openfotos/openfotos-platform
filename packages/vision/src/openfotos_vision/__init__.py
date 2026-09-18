"""Replaceable face-processing interfaces for OpenFotos."""

from .analysis import (
    FaceAnalysisDocument,
    FaceAnalysisDocumentError,
    FaceAnalysisStatus,
    FaceEmbeddingResult,
    build_face_analysis_document,
    model_contract_dict,
)
from .contracts import (
    DetectedFace,
    DistanceMetric,
    Embedding,
    EmbeddingNormalization,
    FaceEngine,
    FaceModelContract,
    FaceQualityRules,
    ModelArtifact,
    ModelIdentity,
    ModelRights,
    normalized_embedding,
)
from .engines import (
    ACCEPTED_FACE_MODEL_CONTRACT,
    ACCEPTED_SFACE_DETECTOR_FLOOR,
    ACCEPTED_SFACE_MAXIMUM_DISTANCE,
    ACCEPTED_SFACE_MODEL,
    FaceEngineError,
    InsightFaceEngine,
    OpenCvSFaceEngine,
    artifact_identity,
)
from .images import FaceImageError, decode_srgb_bgr

__all__ = [
    "ACCEPTED_FACE_MODEL_CONTRACT",
    "ACCEPTED_SFACE_DETECTOR_FLOOR",
    "ACCEPTED_SFACE_MAXIMUM_DISTANCE",
    "ACCEPTED_SFACE_MODEL",
    "DetectedFace",
    "DistanceMetric",
    "Embedding",
    "EmbeddingNormalization",
    "FaceAnalysisDocument",
    "FaceAnalysisDocumentError",
    "FaceAnalysisStatus",
    "FaceEmbeddingResult",
    "FaceEngine",
    "FaceEngineError",
    "FaceImageError",
    "FaceModelContract",
    "FaceQualityRules",
    "InsightFaceEngine",
    "ModelArtifact",
    "ModelIdentity",
    "ModelRights",
    "OpenCvSFaceEngine",
    "artifact_identity",
    "build_face_analysis_document",
    "decode_srgb_bgr",
    "model_contract_dict",
    "normalized_embedding",
]
