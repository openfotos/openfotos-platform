import math
from uuid import uuid4

import pytest

from openfotos_vision import (
    ACCEPTED_FACE_MODEL_CONTRACT,
    ACCEPTED_SFACE_DETECTOR_FLOOR,
    DetectedFace,
    FaceAnalysisDocument,
    FaceAnalysisDocumentError,
    FaceAnalysisStatus,
    build_face_analysis_document,
    normalized_embedding,
)


def _face(*, confidence: float = 0.9, width: int = 80, height: int = 60) -> DetectedFace:
    return DetectedFace(
        embedding=normalized_embedding(
            (1.0, *([0.0] * 127)),
            model=ACCEPTED_FACE_MODEL_CONTRACT.model,
        ),
        detector_confidence=confidence,
        bounding_box_width=width,
        bounding_box_height=height,
    )


def _document(*faces: DetectedFace) -> FaceAnalysisDocument:
    return build_face_analysis_document(
        asset_id=uuid4(),
        source_sha256="a" * 64,
        detected_faces=faces,
        contract=ACCEPTED_FACE_MODEL_CONTRACT,
        detector_floor=ACCEPTED_SFACE_DETECTOR_FLOOR,
    )


def test_document_filters_unusable_detections_and_round_trips_strictly() -> None:
    document = _document(
        _face(),
        _face(confidence=0.79),
        _face(width=39),
    )

    assert document.detected_face_count == 3
    assert document.usable_face_count == 1
    assert document.status is FaceAnalysisStatus.INDEXED
    assert (
        FaceAnalysisDocument.from_dict(
            document.as_dict(),
            accepted_contract=ACCEPTED_FACE_MODEL_CONTRACT,
            accepted_detector_floor=ACCEPTED_SFACE_DETECTOR_FLOOR,
        )
        == document
    )
    assert len(document.document_sha256) == 64


def test_zero_usable_faces_is_a_successful_terminal_document() -> None:
    document = _document(_face(confidence=0.7))

    assert document.status is FaceAnalysisStatus.NO_USABLE_FACE
    assert document.detected_face_count == 1
    assert document.usable_face_count == 0


@pytest.mark.parametrize(
    ("change", "code"),
    (
        (lambda value: value.update({"unexpected": True}), "invalid_face_analysis"),
        (
            lambda value: value["model_contract"].update({"model_id": "other-model"}),
            "model_contract_mismatch",
        ),
        (lambda value: value.update({"usable_face_count": 2}), "invalid_face_counts"),
        (lambda value: value["faces"][0].update({"ordinal": 1}), "invalid_face_ordinal"),
        (
            lambda value: value["faces"][0].update({"values": [math.inf, *([0.0] * 127)]}),
            "invalid_embedding",
        ),
    ),
)
def test_document_rejects_malformed_or_incompatible_payloads(change, code: str) -> None:
    value = _document(_face()).as_dict()
    change(value)

    with pytest.raises(FaceAnalysisDocumentError) as raised:
        FaceAnalysisDocument.from_dict(
            value,
            accepted_contract=ACCEPTED_FACE_MODEL_CONTRACT,
            accepted_detector_floor=ACCEPTED_SFACE_DETECTOR_FLOOR,
        )

    assert raised.value.code == code


def test_document_rejects_more_than_the_accepted_usable_face_limit() -> None:
    with pytest.raises(FaceAnalysisDocumentError) as raised:
        _document(*(_face() for _ in range(101)))

    assert raised.value.code == "too_many_usable_faces"
