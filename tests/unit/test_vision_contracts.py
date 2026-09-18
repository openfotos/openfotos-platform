import math

import pytest

from openfotos_vision import (
    ACCEPTED_FACE_MODEL_CONTRACT,
    ACCEPTED_SFACE_DETECTOR_FLOOR,
    ACCEPTED_SFACE_MAXIMUM_DISTANCE,
    DistanceMetric,
    Embedding,
    EmbeddingNormalization,
    FaceModelContract,
    FaceQualityRules,
    ModelArtifact,
    ModelIdentity,
    ModelRights,
    normalized_embedding,
)


def test_accepted_sface_contract_is_fully_pinned() -> None:
    contract = ACCEPTED_FACE_MODEL_CONTRACT

    assert contract.model.id == "opencv-yunet-2023mar-sface-2021dec"
    assert contract.model.embedding_dimensions == 128
    assert contract.model.detector_input_size == (640, 640)
    assert contract.model.preprocessing == "exif-srgb-letterbox-opencv-sface-v1"
    assert contract.model.artifact_sha256 == (
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    )
    assert contract.normalization is EmbeddingNormalization.L2
    assert contract.normalization_tolerance == 0.001
    assert contract.distance_metric is DistanceMetric.COSINE
    assert contract.maximum_distance == ACCEPTED_SFACE_MAXIMUM_DISTANCE == 0.55514365
    assert ACCEPTED_SFACE_DETECTOR_FLOOR == 0.5
    assert contract.quality == FaceQualityRules(
        detector_confidence_threshold=0.8,
        minimum_face_size_pixels=40,
        maximum_faces_per_photo=100,
    )

    embedding = contract.validate_result(
        {
            "model_id": contract.model.id,
            "artifact_sha256": list(contract.model.artifact_sha256),
            "normalization": "l2",
            "values": [1.0, *([0.0] * 127)],
        }
    )
    assert embedding.model is contract.model


def _model() -> ModelIdentity:
    return ModelIdentity(
        id="scrfd-r50-v1",
        detector="scrfd-2.5gf",
        recognizer="w600k-r50",
        embedding_dimensions=2,
        artifacts=(ModelArtifact(filename="model.onnx", sha256="a" * 64, license_id="mit"),),
        preprocessing="insightface-bgr-112-v1",
        detector_input_size=(640, 640),
        rights=ModelRights(
            status="redistributable",
            basis="Synthetic test fixture",
            permission_evidence_private=False,
        ),
    )


def _contract() -> FaceModelContract:
    return FaceModelContract(
        model=_model(),
        normalization=EmbeddingNormalization.L2,
        normalization_tolerance=0.001,
        distance_metric=DistanceMetric.COSINE,
        maximum_distance=0.4,
        quality=FaceQualityRules(
            detector_confidence_threshold=0.8,
            minimum_face_size_pixels=40,
            maximum_faces_per_photo=100,
        ),
    )


def _result() -> dict[str, object]:
    return {
        "model_id": "scrfd-r50-v1",
        "artifact_sha256": ["a" * 64],
        "normalization": "l2",
        "values": [0.6, 0.8],
    }


def test_embedding_dimensions_must_match_the_model() -> None:
    with pytest.raises(ValueError, match="dimensions"):
        Embedding(values=(0.1,), model=_model())


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("model_id", "other-model", "model id"),
        ("artifact_sha256", ["b" * 64], "model hash"),
        ("normalization", "none", "normalization"),
        ("values", [1.0], "dimensions"),
        ("values", [math.inf, 0.0], "finite"),
        ("values", [0.5, 0.5], "normalization"),
    ),
)
def test_result_must_match_every_accepted_model_invariant(
    field: str, value: object, message: str
) -> None:
    result = _result()
    result[field] = value

    with pytest.raises(ValueError, match=message):
        _contract().validate_result(result)


def test_result_contract_rejects_extra_fields() -> None:
    result = _result()
    result["asset_id"] = "not-part-of-session-6"

    with pytest.raises(ValueError, match="fields"):
        _contract().validate_result(result)


def test_matching_result_produces_an_embedding() -> None:
    embedding = _contract().validate_result(_result())

    assert embedding.model == _model()
    assert embedding.values == (0.6, 0.8)


def test_normalized_embedding_rejects_a_zero_vector() -> None:
    with pytest.raises(ValueError, match="non-zero"):
        normalized_embedding((0.0, 0.0), model=_model())
