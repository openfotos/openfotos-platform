import json
from pathlib import Path

import pytest

from openfotos_vision import (
    ACCEPTED_FACE_MODEL_CONTRACT,
    DetectedFace,
    FaceQualityRules,
    ModelArtifact,
    ModelIdentity,
    ModelRights,
    normalized_embedding,
)
from openfotos_vision.benchmark import (
    load_lfw_dataset,
    load_matching_evidence,
    load_representative_dataset,
    run_lfw_benchmark,
    run_representative_benchmark,
)


class FakeEngine:
    def __init__(self) -> None:
        self._model = ModelIdentity(
            id="synthetic-face-engine-v1",
            detector="synthetic-detector-v1",
            recognizer="synthetic-recognizer-v1",
            embedding_dimensions=2,
            artifacts=(
                ModelArtifact(filename="synthetic.onnx", sha256="a" * 64, license_id="mit"),
            ),
            preprocessing="synthetic-preprocessing-v1",
            detector_input_size=(640, 640),
            rights=ModelRights(
                status="redistributable",
                basis="Synthetic test fixture",
                permission_evidence_private=False,
            ),
        )

    @property
    def model(self) -> ModelIdentity:
        return self._model

    @property
    def runtime_versions(self) -> dict[str, str]:
        return {"fake": "1.0"}

    def detect_and_embed(self, image_bytes: bytes) -> tuple[DetectedFace, ...]:
        value = (1.0, 0.0) if image_bytes == b"first" else (0.0, 1.0)
        return (
            DetectedFace(
                embedding=normalized_embedding(value, model=self.model),
                detector_confidence=0.99,
                bounding_box_width=100,
                bounding_box_height=100,
            ),
        )


def _write_split(root: Path, name: str) -> None:
    (root / name).write_text("name,images\nPrivate_One,2\nPrivate_Two,2\n", encoding="utf-8")


def _write_images(root: Path) -> None:
    for identity, content in (("Private_One", b"first"), ("Private_Two", b"second")):
        directory = root / identity
        directory.mkdir()
        for number in (1, 2):
            (directory / f"{identity}_{number:04d}.jpg").write_bytes(content)


def test_lfw_report_is_strictly_aggregate_and_redacted(tmp_path: Path) -> None:
    metadata = tmp_path / "private-metadata"
    images = tmp_path / "private-images"
    metadata.mkdir()
    images.mkdir()
    _write_split(metadata, "peopleDevTrain.csv")
    _write_split(metadata, "peopleDevTest.csv")
    _write_images(images)
    dataset = load_lfw_dataset(metadata_directory=metadata, image_directory=images)

    report = run_lfw_benchmark(
        dataset,
        FakeEngine(),
        sample_size=4,
        runtime_threads=1,
        quality=FaceQualityRules(
            detector_confidence_threshold=0.8,
            minimum_face_size_pixels=40,
            maximum_faces_per_photo=10,
        ),
    )

    rendered = str(report)
    assert report["format"] == "openfotos-face-benchmark-v1"
    assert report["redacted"] is True
    assert report["matching"]["holdout"]["recall"] == 1.0
    assert report["matching"]["holdout"]["false_accepts"] == 0
    assert report["model"]["licensing"] == {
        "status": "redistributable",
        "basis": "Synthetic test fixture",
        "permission_evidence_private": False,
    }
    assert "Private_One" not in rendered
    assert "private-images" not in rendered


def test_representative_report_combines_matching_evidence_without_private_names(
    tmp_path: Path,
) -> None:
    private_images = tmp_path / "Private Wedding Name"
    private_images.mkdir()
    (private_images / "Secret Person One.jpg").write_bytes(b"first")
    (private_images / "Secret Person Two.jpeg").write_bytes(b"second")
    (private_images / "notes.txt").write_text("not media", encoding="utf-8")
    engine = FakeEngine()
    matching_report = tmp_path / "matching.json"
    _write_matching_report(matching_report, engine)

    dataset = load_representative_dataset(image_directory=private_images, maximum_images=2)
    evidence = load_matching_evidence(matching_report, engine)
    report = run_representative_benchmark(dataset, engine, evidence, runtime_threads=1)

    rendered = json.dumps(report)
    assert report["dataset"]["eligible_images"] == 2
    assert report["dataset"]["id"] == "consented-representative-wedding"
    assert report["matching_evidence"]["gate_passed"] is True
    assert report["model"]["recommended_maximum_distance"] == 0.5
    assert report["performance"]["projected_10000_photos_100000_usable_faces_hours"] is not None
    assert report["decision"] == {
        "go": False,
        "reasons": [
            "representative_photo_count_below_500",
            "dense_group_coverage_below_10_photos",
        ],
    }
    assert "Private Wedding Name" not in rendered
    assert "Secret Person" not in rendered


def test_matching_evidence_rejects_a_different_artifact(tmp_path: Path) -> None:
    engine = FakeEngine()
    matching_report = tmp_path / "matching.json"
    _write_matching_report(matching_report, engine, artifact_sha256="b" * 64)

    with pytest.raises(ValueError, match="different model artifacts"):
        load_matching_evidence(matching_report, engine)


@pytest.mark.parametrize(
    "filename",
    [
        "lfw-sface.json",
        "lfw-buffalo-m.json",
        "representative-sface.json",
        "representative-expansion-sface.json",
    ],
)
def test_committed_face_reports_are_aggregate_and_redacted(filename: str) -> None:
    repository = Path(__file__).parents[2]
    report_path = repository / "docs" / "session6" / "reports" / filename
    report = json.loads(report_path.read_text(encoding="utf-8"))
    rendered = json.dumps(report)

    assert report["format"] == "openfotos-face-benchmark-v1"
    assert report["status"] == "preliminary"
    assert report["redacted"] is True
    assert not {"path", "name", "identity", "embedding", "embeddings"} & _nested_keys(report)
    assert "/home/" not in rendered
    assert "lfw_dataset" not in rendered
    assert ".jpg" not in rendered.lower()


def test_accepted_representative_report_passes_the_frozen_contract() -> None:
    repository = Path(__file__).parents[2]
    report_path = (
        repository / "docs" / "session6" / "reports" / "representative-expansion-sface.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    contract = ACCEPTED_FACE_MODEL_CONTRACT

    assert report["decision"] == {"go": True, "reasons": []}
    assert report["matching_evidence"]["gate_passed"] is True
    assert report["performance"]["event_projection_gate_passed"] is True
    assert report["performance"]["memory_gate_passed"] is True
    assert report["detection"]["dense_group_coverage_gate_passed"] is True
    assert report["detection"]["processing_failures"] == 0
    assert report["model"]["id"] == contract.model.id
    assert (
        tuple(artifact["sha256"] for artifact in report["model"]["artifacts"])
        == contract.model.artifact_sha256
    )
    assert report["model"]["recommended_maximum_distance"] == contract.maximum_distance


def _nested_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            nested_key for nested in value.values() for nested_key in _nested_keys(nested)
        }
    if isinstance(value, list):
        return {nested_key for nested in value for nested_key in _nested_keys(nested)}
    return set()


def _write_matching_report(
    path: Path,
    engine: FakeEngine,
    *,
    artifact_sha256: str = "a" * 64,
) -> None:
    report = {
        "format": "openfotos-face-benchmark-v1",
        "redacted": True,
        "model": {
            "id": engine.model.id,
            "artifacts": [{"sha256": artifact_sha256}],
            "embedding_dimensions": engine.model.embedding_dimensions,
            "preprocessing": engine.model.preprocessing,
            "detector_input_size": list(engine.model.detector_input_size),
            "normalization": "l2",
            "distance_metric": "cosine",
            "recommended_maximum_distance": 0.5,
        },
        "matching": {
            "holdout": {"recall": 0.99, "false_accept_rate": 0.00001},
            "holdout_gate_passed": True,
        },
    }
    path.write_text(json.dumps(report), encoding="utf-8")
