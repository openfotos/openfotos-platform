import hashlib
from pathlib import Path

import pytest

from openfotos_vision import FaceEngineError, OpenCvSFaceEngine, artifact_identity


def test_artifact_is_verified_before_use(tmp_path: Path) -> None:
    artifact = tmp_path / "candidate.onnx"
    artifact.write_bytes(b"synthetic model bytes")
    expected_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()

    identity = artifact_identity(
        artifact,
        expected_sha256=expected_sha256,
        license_id="mit",
    )

    assert identity.filename == "candidate.onnx"
    assert identity.sha256 == expected_sha256


def test_artifact_hash_mismatch_fails_without_exposing_the_path(tmp_path: Path) -> None:
    artifact = tmp_path / "private-model-location.onnx"
    artifact.write_bytes(b"wrong bytes")

    with pytest.raises(FaceEngineError, match="hash") as raised:
        artifact_identity(
            artifact,
            expected_sha256="a" * 64,
            license_id="mit",
        )

    assert "private-model-location" not in str(raised.value)


def test_sface_engine_rejects_detector_geometry_outside_the_accepted_contract(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="input size differs"):
        OpenCvSFaceEngine(
            detector_path=tmp_path / "missing-detector.onnx",
            recognizer_path=tmp_path / "missing-recognizer.onnx",
            detector_input_size=(320, 320),
        )


def test_sface_engine_rejects_detector_floor_outside_the_accepted_contract(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="floor differs"):
        OpenCvSFaceEngine(
            detector_path=tmp_path / "missing-detector.onnx",
            recognizer_path=tmp_path / "missing-recognizer.onnx",
            detector_floor=0.6,
        )
