"""CPU face engines used by the Session 6 benchmark."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

from .contracts import (
    DetectedFace,
    DistanceMetric,
    EmbeddingNormalization,
    FaceModelContract,
    FaceQualityRules,
    ModelArtifact,
    ModelIdentity,
    ModelRights,
    normalized_embedding,
)
from .images import FaceImageError, decode_srgb_bgr

YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
SFACE_SHA256 = "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"
BUFFALO_M_DETECTOR_SHA256 = "041f73f47371333d1d17a6fee6c8ab4e6aecabefe398ff32cca4e2d5eaee0af9"
BUFFALO_M_RECOGNIZER_SHA256 = "4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43"
ACCEPTED_SFACE_MAXIMUM_DISTANCE = 0.55514365
ACCEPTED_SFACE_DETECTOR_FLOOR = 0.5
ACCEPTED_SFACE_MODEL = ModelIdentity(
    id="opencv-yunet-2023mar-sface-2021dec",
    detector="yunet-2023mar",
    recognizer="sface-2021dec",
    embedding_dimensions=128,
    artifacts=(
        ModelArtifact(
            filename="face_detection_yunet_2023mar.onnx",
            sha256=YUNET_SHA256,
            license_id="mit",
        ),
        ModelArtifact(
            filename="face_recognition_sface_2021dec.onnx",
            sha256=SFACE_SHA256,
            license_id="apache-2.0",
        ),
    ),
    preprocessing="exif-srgb-letterbox-opencv-sface-v1",
    detector_input_size=(640, 640),
    rights=ModelRights(
        status="redistributable",
        basis="YuNet MIT and SFace Apache-2.0 model-directory licences",
        permission_evidence_private=False,
    ),
)
ACCEPTED_FACE_MODEL_CONTRACT = FaceModelContract(
    model=ACCEPTED_SFACE_MODEL,
    normalization=EmbeddingNormalization.L2,
    normalization_tolerance=0.001,
    distance_metric=DistanceMetric.COSINE,
    maximum_distance=ACCEPTED_SFACE_MAXIMUM_DISTANCE,
    quality=FaceQualityRules(
        detector_confidence_threshold=0.8,
        minimum_face_size_pixels=40,
        maximum_faces_per_photo=100,
    ),
)


class FaceEngineError(RuntimeError):
    """A local image or model could not produce a trustworthy face result."""


def artifact_identity(
    path: Path,
    *,
    expected_sha256: str,
    license_id: str,
) -> ModelArtifact:
    if not path.is_file():
        raise FaceEngineError("A required model artifact is missing.")
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != expected_sha256:
        raise FaceEngineError("A model artifact hash does not match the pinned candidate.")
    return ModelArtifact(filename=path.name, sha256=actual_sha256, license_id=license_id)


class OpenCvSFaceEngine:
    """The accepted YuNet and SFace engine through OpenCV's stable APIs."""

    def __init__(
        self,
        *,
        detector_path: Path,
        recognizer_path: Path,
        detector_input_size: tuple[int, int] = (640, 640),
        detector_floor: float = 0.5,
        runtime_threads: int = 1,
    ) -> None:
        import cv2

        if any(size < 1 for size in detector_input_size):
            raise ValueError("detector input dimensions must be positive")
        if not math.isfinite(detector_floor) or not 0.0 < detector_floor <= 1.0:
            raise ValueError("detector floor must be in (0, 1]")
        if detector_input_size != ACCEPTED_SFACE_MODEL.detector_input_size:
            raise ValueError("detector input size differs from the accepted SFace contract")
        if detector_floor != ACCEPTED_SFACE_DETECTOR_FLOOR:
            raise ValueError("detector floor differs from the accepted SFace contract")
        if runtime_threads < 1:
            raise ValueError("runtime threads must be positive")
        detector_artifact = artifact_identity(
            detector_path,
            expected_sha256=YUNET_SHA256,
            license_id="mit",
        )
        recognizer_artifact = artifact_identity(
            recognizer_path,
            expected_sha256=SFACE_SHA256,
            license_id="apache-2.0",
        )
        if (detector_artifact, recognizer_artifact) != ACCEPTED_SFACE_MODEL.artifacts:
            raise FaceEngineError(
                "Model artifact metadata differs from the accepted SFace contract."
            )
        cv2.setNumThreads(runtime_threads)
        self._cv2 = cv2
        self._input_size = detector_input_size
        self._detector = cv2.FaceDetectorYN.create(
            str(detector_path),
            "",
            detector_input_size,
            detector_floor,
            0.3,
            5000,
        )
        self._recognizer = cv2.FaceRecognizerSF.create(str(recognizer_path), "")
        self._model = ACCEPTED_SFACE_MODEL

    @property
    def model(self) -> ModelIdentity:
        return self._model

    @property
    def runtime_versions(self) -> dict[str, str]:
        return {"opencv": self._cv2.__version__}

    def detect_and_embed(self, image_bytes: bytes) -> tuple[DetectedFace, ...]:
        cv2 = self._cv2
        try:
            encoded = decode_srgb_bgr(image_bytes)
        except FaceImageError as exc:
            raise FaceEngineError("The benchmark image is not a decodable color image.") from exc
        detection_image, scale = self._detection_image(encoded)
        self._detector.setInputSize(self._input_size)
        _, detections = self._detector.detect(detection_image)
        if detections is None:
            return ()
        results = []
        for detection in detections:
            try:
                source_detection = detection.copy()
                source_detection[:-1] /= scale
                aligned = self._recognizer.alignCrop(encoded, source_detection)
                raw_embedding = self._recognizer.feature(aligned).reshape(-1)
                embedding = normalized_embedding(
                    tuple(float(value) for value in raw_embedding),
                    model=self.model,
                )
            except (ValueError, cv2.error) as exc:
                raise FaceEngineError("A detected face could not be embedded.") from exc
            results.append(
                DetectedFace(
                    embedding=embedding,
                    detector_confidence=float(detection[-1]),
                    bounding_box_width=max(1, round(float(source_detection[2]))),
                    bounding_box_height=max(1, round(float(source_detection[3]))),
                )
            )
        return tuple(results)

    def _detection_image(self, image: object) -> tuple[object, float]:
        import numpy

        source_height, source_width = image.shape[:2]
        target_width, target_height = self._input_size
        scale = min(target_width / source_width, target_height / source_height)
        resized_width = max(1, round(source_width * scale))
        resized_height = max(1, round(source_height * scale))
        resized = self._cv2.resize(image, (resized_width, resized_height))
        canvas = numpy.zeros((target_height, target_width, 3), dtype=numpy.uint8)
        canvas[:resized_height, :resized_width] = resized
        return canvas, scale


class InsightFaceEngine:
    """Direct SCRFD and ArcFace ONNX execution without unrelated model modules."""

    def __init__(
        self,
        *,
        detector_path: Path,
        recognizer_path: Path,
        detector_input_size: tuple[int, int] = (640, 640),
        detector_floor: float = 0.5,
        runtime_threads: int = 1,
    ) -> None:
        from insightface.model_zoo import get_model
        from onnxruntime import ExecutionMode, SessionOptions

        if any(size < 1 for size in detector_input_size):
            raise ValueError("detector input dimensions must be positive")
        if not math.isfinite(detector_floor) or not 0.0 < detector_floor <= 1.0:
            raise ValueError("detector floor must be in (0, 1]")
        if runtime_threads < 1:
            raise ValueError("runtime threads must be positive")
        detector_artifact = artifact_identity(
            detector_path,
            expected_sha256=BUFFALO_M_DETECTOR_SHA256,
            license_id="insightface-public-model",
        )
        recognizer_artifact = artifact_identity(
            recognizer_path,
            expected_sha256=BUFFALO_M_RECOGNIZER_SHA256,
            license_id="insightface-public-model",
        )
        session_options = SessionOptions()
        session_options.intra_op_num_threads = runtime_threads
        session_options.inter_op_num_threads = 1
        session_options.execution_mode = ExecutionMode.ORT_SEQUENTIAL
        providers = ["CPUExecutionProvider"]
        detector = get_model(str(detector_path), providers=providers, sess_options=session_options)
        recognizer = get_model(
            str(recognizer_path), providers=providers, sess_options=session_options
        )
        if detector is None or getattr(detector, "taskname", None) != "detection":
            raise FaceEngineError("The configured InsightFace detector artifact is invalid.")
        if recognizer is None or getattr(recognizer, "taskname", None) != "recognition":
            raise FaceEngineError("The configured InsightFace recognizer artifact is invalid.")
        detector.prepare(ctx_id=-1, input_size=detector_input_size, det_thresh=detector_floor)
        recognizer.prepare(ctx_id=-1)
        self._detector = detector
        self._recognizer = recognizer
        self._runtime_versions = {
            "insightface": __import__("insightface").__version__,
            "onnxruntime": __import__("onnxruntime").__version__,
        }
        self._model = ModelIdentity(
            id="insightface-buffalo_m-v0.7",
            detector="scrfd-2.5gf",
            recognizer="w600k-r50",
            embedding_dimensions=int(recognizer.output_shape[1]),
            artifacts=(detector_artifact, recognizer_artifact),
            preprocessing="exif-srgb-insightface-arcface-v1",
            detector_input_size=detector_input_size,
            rights=ModelRights(
                status="written-use-and-distribution-permission-confirmed",
                basis="Private OpenFotos grant confirmed by the product owner on 2026-09-14",
                permission_evidence_private=True,
            ),
        )

    @property
    def model(self) -> ModelIdentity:
        return self._model

    @property
    def runtime_versions(self) -> dict[str, str]:
        return dict(self._runtime_versions)

    def detect_and_embed(self, image_bytes: bytes) -> tuple[DetectedFace, ...]:
        from insightface.app.common import Face

        try:
            encoded = decode_srgb_bgr(image_bytes)
        except FaceImageError as exc:
            raise FaceEngineError("The benchmark image is not a decodable color image.") from exc
        bounding_boxes, landmarks = self._detector.detect(encoded, max_num=0)
        if bounding_boxes.shape[0] == 0:
            return ()
        if landmarks is None:
            raise FaceEngineError("The detector did not return alignment landmarks.")
        results = []
        for bounding_box, keypoints in zip(bounding_boxes, landmarks, strict=True):
            face = Face(
                bbox=bounding_box[:4],
                kps=keypoints,
                det_score=float(bounding_box[4]),
            )
            self._recognizer.get(encoded, face)
            if face.embedding is None:
                raise FaceEngineError("A detected face could not be embedded.")
            embedding = normalized_embedding(
                tuple(float(value) for value in face.embedding),
                model=self.model,
            )
            width = max(1, round(float(bounding_box[2] - bounding_box[0])))
            height = max(1, round(float(bounding_box[3] - bounding_box[1])))
            results.append(
                DetectedFace(
                    embedding=embedding,
                    detector_confidence=float(bounding_box[4]),
                    bounding_box_width=width,
                    bounding_box_height=height,
                )
            )
        return tuple(results)
