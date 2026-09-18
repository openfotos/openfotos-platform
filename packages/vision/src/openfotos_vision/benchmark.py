"""Reproducible, redacted Session 6 face-engine benchmark."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import random
import resource
import sys
import tempfile
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy

from .contracts import FaceEngine, FaceQualityRules
from .engines import (
    ACCEPTED_FACE_MODEL_CONTRACT,
    FaceEngineError,
    InsightFaceEngine,
    OpenCvSFaceEngine,
)

REPORT_FORMAT = "openfotos-face-benchmark-v1"
DEFAULT_SAMPLE_SIZE = 1000
DEFAULT_SAMPLE_SEED = 20260914
DEFAULT_RUNTIME_THREADS = 4
MINIMUM_REPRESENTATIVE_PHOTOS = 500
MAXIMUM_REPRESENTATIVE_PHOTOS = 1000
DENSE_GROUP_MINIMUM_USABLE_FACES = 10
MINIMUM_DENSE_GROUP_PHOTOS = 10
TARGET_PHOTOS = 10_000
TARGET_USABLE_FACES = 100_000
MAXIMUM_EVENT_HOURS = 12.0
MAXIMUM_RSS_MIB = 2048.0
TARGET_RECALL = 0.98
MAXIMUM_FALSE_ACCEPT_RATE = 0.0001
NORMALIZATION_TOLERANCE = ACCEPTED_FACE_MODEL_CONTRACT.normalization_tolerance
DEFAULT_QUALITY_RULES = ACCEPTED_FACE_MODEL_CONTRACT.quality
ProgressReporter = Callable[[str, int, int], None]


@dataclass(frozen=True, slots=True)
class LfwImage:
    identity: int
    path: Path


@dataclass(frozen=True, slots=True)
class LfwDataset:
    calibration: tuple[LfwImage, ...]
    holdout: tuple[LfwImage, ...]


@dataclass(frozen=True, slots=True)
class SplitObservations:
    vectors_by_identity: dict[int, tuple[numpy.ndarray | None, ...]]
    detected_faces: int
    usable_faces: int
    images_without_detection: int
    images_without_usable_face: int
    images_with_multiple_faces: int
    processing_failures: int
    sample_elapsed_seconds: float
    sample_images: int


@dataclass(frozen=True, slots=True)
class RepresentativeDataset:
    images: tuple[Path, ...]
    eligible_images: int
    evaluated_bytes: int
    sample_seed: int


@dataclass(frozen=True, slots=True)
class MatchingEvidence:
    report_sha256: str
    holdout_recall: float
    false_accept_rate: float
    maximum_distance: float
    gate_passed: bool


@dataclass(frozen=True, slots=True)
class RepresentativeObservations:
    detected_faces: int
    usable_faces: int
    faces_below_confidence: int
    faces_below_minimum_size: int
    images_without_detection: int
    images_without_usable_face: int
    images_with_multiple_faces: int
    images_with_at_least_ten_usable_faces: int
    maximum_usable_faces_in_one_image: int
    processing_failures: int
    elapsed_seconds: float
    elapsed_by_image: tuple[float, ...]
    detected_faces_by_image: tuple[int, ...]
    usable_face_histogram: dict[str, int]


def load_lfw_dataset(*, metadata_directory: Path, image_directory: Path) -> LfwDataset:
    return LfwDataset(
        calibration=_load_lfw_split(metadata_directory / "peopleDevTrain.csv", image_directory),
        holdout=_load_lfw_split(metadata_directory / "peopleDevTest.csv", image_directory),
    )


def run_lfw_benchmark(
    dataset: LfwDataset,
    engine: FaceEngine,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    sample_seed: int = DEFAULT_SAMPLE_SEED,
    runtime_threads: int = DEFAULT_RUNTIME_THREADS,
    quality: FaceQualityRules = DEFAULT_QUALITY_RULES,
    progress: ProgressReporter | None = None,
) -> dict[str, object]:
    if sample_size < 1:
        raise ValueError("sample size must be positive")
    all_images = dataset.calibration + dataset.holdout
    if not all_images:
        raise ValueError("the LFW benchmark dataset is empty")
    selected = _sample_indexes(len(all_images), min(sample_size, len(all_images)), sample_seed)
    calibration_indexes = {index for index in selected if index < len(dataset.calibration)}
    holdout_indexes = {
        index - len(dataset.calibration) for index in selected if index >= len(dataset.calibration)
    }

    started = time.perf_counter()
    calibration = _observe_split(
        dataset.calibration,
        engine,
        quality=quality,
        sample_indexes=calibration_indexes,
        phase="calibration",
        progress=progress,
    )
    holdout = _observe_split(
        dataset.holdout,
        engine,
        quality=quality,
        sample_indexes=holdout_indexes,
        phase="holdout",
        progress=progress,
    )
    total_elapsed = time.perf_counter() - started

    calibration_matrix, calibration_identities = _present_matrix(calibration.vectors_by_identity)
    maximum_distance = _threshold_for_false_accept_rate(
        calibration_matrix,
        calibration_identities,
        maximum_false_accept_rate=MAXIMUM_FALSE_ACCEPT_RATE,
    )
    calibration_metrics = _matching_metrics(
        calibration.vectors_by_identity,
        maximum_distance=maximum_distance,
    )
    holdout_metrics = _matching_metrics(
        holdout.vectors_by_identity,
        maximum_distance=maximum_distance,
    )

    sample_elapsed = calibration.sample_elapsed_seconds + holdout.sample_elapsed_seconds
    measured_sample = calibration.sample_images + holdout.sample_images
    images_per_second = measured_sample / sample_elapsed if sample_elapsed else 0.0
    projected_one_face_hours = (
        TARGET_PHOTOS / images_per_second / 3600.0 if images_per_second else None
    )
    peak_rss_mib = _peak_rss_mib()
    matching_passes = (
        holdout_metrics["recall"] >= TARGET_RECALL
        and holdout_metrics["false_accept_rate"] <= MAXIMUM_FALSE_ACCEPT_RATE
    )
    preliminary_time_passes = (
        projected_one_face_hours is not None and projected_one_face_hours <= MAXIMUM_EVENT_HOURS
    )
    memory_passes = peak_rss_mib <= MAXIMUM_RSS_MIB
    reasons = ["representative_wedding_benchmark_pending", "dense_group_benchmark_pending"]
    if not matching_passes:
        reasons.append("lfw_matching_gate_failed")
    if not preliminary_time_passes:
        reasons.append("preliminary_time_gate_failed")
    if not memory_passes:
        reasons.append("memory_gate_failed")
    if calibration.processing_failures or holdout.processing_failures:
        reasons.append("image_processing_failures")

    return {
        "format": REPORT_FORMAT,
        "status": "preliminary",
        "redacted": True,
        "decision": {"go": False, "reasons": reasons},
        "targets": {
            "photos_per_event": TARGET_PHOTOS,
            "usable_faces_per_event": TARGET_USABLE_FACES,
            "maximum_event_hours": MAXIMUM_EVENT_HOURS,
            "maximum_rss_mib": MAXIMUM_RSS_MIB,
            "minimum_holdout_recall": TARGET_RECALL,
            "maximum_false_accept_rate": MAXIMUM_FALSE_ACCEPT_RATE,
        },
        "hardware": _hardware_report(),
        "dataset": {
            "id": "lfw-deepfunneled-dev-split",
            "calibration_images": len(dataset.calibration),
            "holdout_images": len(dataset.holdout),
            "sample_seed": sample_seed,
        },
        "model": _model_report(engine, maximum_distance=maximum_distance),
        "quality": {
            "detector_confidence_threshold": quality.detector_confidence_threshold,
            "minimum_face_size_pixels": quality.minimum_face_size_pixels,
            "maximum_faces_per_photo": quality.maximum_faces_per_photo,
        },
        "performance": {
            "runtime_threads": runtime_threads,
            "sample_images": measured_sample,
            "sample_elapsed_seconds": round(sample_elapsed, 3),
            "images_per_second": round(images_per_second, 4),
            "projected_10000_one_face_hours": (
                round(projected_one_face_hours, 3) if projected_one_face_hours is not None else None
            ),
            "full_evaluation_images": len(all_images),
            "full_evaluation_elapsed_seconds": round(total_elapsed, 3),
            "peak_rss_mib": round(peak_rss_mib, 1),
            "preliminary_time_gate_passed": preliminary_time_passes,
            "memory_gate_passed": memory_passes,
        },
        "detection": _detection_report(calibration, holdout),
        "matching": {
            "calibration": calibration_metrics,
            "holdout": holdout_metrics,
            "holdout_gate_passed": matching_passes,
            "limitation": "LFW is aligned and is not representative of dense wedding photos.",
        },
    }


def load_representative_dataset(
    *,
    image_directory: Path,
    maximum_images: int = MAXIMUM_REPRESENTATIVE_PHOTOS,
    sample_seed: int = DEFAULT_SAMPLE_SEED,
) -> RepresentativeDataset:
    if not image_directory.is_dir():
        raise ValueError("the representative image directory is missing")
    if not 1 <= maximum_images <= 10_000:
        raise ValueError("representative maximum images must be between 1 and 10000")
    candidates = []
    for path in image_directory.rglob("*"):
        if path.suffix.casefold() not in {".jpg", ".jpeg"}:
            continue
        if path.is_symlink():
            raise ValueError("representative image symlinks are not accepted")
        if path.is_file():
            candidates.append(path)
    candidates.sort(key=lambda path: path.relative_to(image_directory).as_posix().casefold())
    if not candidates:
        raise ValueError("the representative dataset contains no JPEG images")
    selected_indexes = sorted(
        _sample_indexes(len(candidates), min(maximum_images, len(candidates)), sample_seed)
    )
    selected = tuple(candidates[index] for index in selected_indexes)
    try:
        evaluated_bytes = sum(path.stat().st_size for path in selected)
    except OSError as exc:
        raise ValueError("a representative image cannot be inspected") from exc
    return RepresentativeDataset(
        images=selected,
        eligible_images=len(candidates),
        evaluated_bytes=evaluated_bytes,
        sample_seed=sample_seed,
    )


def load_matching_evidence(report_path: Path, engine: FaceEngine) -> MatchingEvidence:
    try:
        encoded = report_path.read_bytes()
        report = json.loads(encoded)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("the matching evidence report cannot be read") from exc
    if not isinstance(report, dict) or report.get("format") != REPORT_FORMAT:
        raise ValueError("the matching evidence report format is invalid")
    if report.get("redacted") is not True:
        raise ValueError("matching evidence must be a redacted report")
    model = report.get("model")
    if not isinstance(model, dict) or model.get("id") != engine.model.id:
        raise ValueError("matching evidence uses a different model id")
    if (
        model.get("embedding_dimensions") != engine.model.embedding_dimensions
        or model.get("preprocessing") != engine.model.preprocessing
        or model.get("detector_input_size") != list(engine.model.detector_input_size)
        or model.get("normalization") != "l2"
        or model.get("distance_metric") != "cosine"
    ):
        raise ValueError("matching evidence uses a different model contract")
    artifacts = model.get("artifacts")
    if (
        not isinstance(artifacts, list)
        or tuple(
            artifact.get("sha256") if isinstance(artifact, dict) else None for artifact in artifacts
        )
        != engine.model.artifact_sha256
    ):
        raise ValueError("matching evidence uses different model artifacts")
    matching = report.get("matching")
    if not isinstance(matching, dict):
        raise ValueError("matching evidence has no matching result")
    holdout = matching.get("holdout")
    if not isinstance(holdout, dict):
        raise ValueError("matching evidence has no holdout result")
    recall = _finite_report_number(holdout.get("recall"), field="holdout recall")
    false_accept_rate = _finite_report_number(
        holdout.get("false_accept_rate"), field="holdout false-accept rate"
    )
    maximum_distance = _finite_report_number(
        model.get("recommended_maximum_distance"), field="matching threshold"
    )
    if not 0.0 <= recall <= 1.0 or not 0.0 <= false_accept_rate <= 1.0:
        raise ValueError("matching evidence rates are outside [0, 1]")
    if not 0.0 <= maximum_distance <= 2.0:
        raise ValueError("matching evidence threshold is outside [0, 2]")
    reported_gate = matching.get("holdout_gate_passed") is True
    computed_gate = recall >= TARGET_RECALL and false_accept_rate <= MAXIMUM_FALSE_ACCEPT_RATE
    if reported_gate != computed_gate:
        raise ValueError("matching evidence gate is internally inconsistent")
    return MatchingEvidence(
        report_sha256=hashlib.sha256(encoded).hexdigest(),
        holdout_recall=recall,
        false_accept_rate=false_accept_rate,
        maximum_distance=maximum_distance,
        gate_passed=computed_gate,
    )


def run_representative_benchmark(
    dataset: RepresentativeDataset,
    engine: FaceEngine,
    matching_evidence: MatchingEvidence,
    *,
    runtime_threads: int = DEFAULT_RUNTIME_THREADS,
    quality: FaceQualityRules = DEFAULT_QUALITY_RULES,
    progress: ProgressReporter | None = None,
) -> dict[str, object]:
    if not dataset.images:
        raise ValueError("the representative benchmark dataset is empty")
    observations = _observe_representative(
        dataset.images,
        engine,
        quality=quality,
        progress=progress,
    )
    projection = _representative_runtime_projection(observations)
    projected_hours = projection["projected_10000_photos_100000_usable_faces_hours"]
    peak_rss_mib = _peak_rss_mib()
    photo_count_passes = len(dataset.images) >= MINIMUM_REPRESENTATIVE_PHOTOS
    dense_group_passes = (
        observations.images_with_at_least_ten_usable_faces >= MINIMUM_DENSE_GROUP_PHOTOS
    )
    time_passes = isinstance(projected_hours, float) and projected_hours <= MAXIMUM_EVENT_HOURS
    memory_passes = peak_rss_mib <= MAXIMUM_RSS_MIB
    reasons = []
    if not matching_evidence.gate_passed:
        reasons.append("lfw_matching_gate_failed")
    if not photo_count_passes:
        reasons.append("representative_photo_count_below_500")
    if not dense_group_passes:
        reasons.append("dense_group_coverage_below_10_photos")
    if not time_passes:
        reasons.append("event_projection_gate_failed")
    if not memory_passes:
        reasons.append("memory_gate_failed")
    if observations.processing_failures:
        reasons.append("image_processing_failures")

    images_per_second = (
        len(dataset.images) / observations.elapsed_seconds if observations.elapsed_seconds else 0.0
    )
    return {
        "format": REPORT_FORMAT,
        "status": "preliminary",
        "redacted": True,
        "decision": {"go": not reasons, "reasons": reasons},
        "targets": {
            "minimum_representative_photos": MINIMUM_REPRESENTATIVE_PHOTOS,
            "maximum_representative_photos": MAXIMUM_REPRESENTATIVE_PHOTOS,
            "minimum_dense_group_photos": MINIMUM_DENSE_GROUP_PHOTOS,
            "dense_group_minimum_usable_faces": DENSE_GROUP_MINIMUM_USABLE_FACES,
            "photos_per_event": TARGET_PHOTOS,
            "usable_faces_per_event": TARGET_USABLE_FACES,
            "maximum_event_hours": MAXIMUM_EVENT_HOURS,
            "maximum_rss_mib": MAXIMUM_RSS_MIB,
        },
        "hardware": _hardware_report(),
        "dataset": {
            "id": "consented-representative-wedding",
            "consent_attested": True,
            "eligible_images": dataset.eligible_images,
            "evaluated_images": len(dataset.images),
            "evaluated_bytes": dataset.evaluated_bytes,
            "sample_seed": dataset.sample_seed,
            "photo_count_gate_passed": photo_count_passes,
        },
        "matching_evidence": {
            "report_sha256": matching_evidence.report_sha256,
            "holdout_recall": matching_evidence.holdout_recall,
            "false_accept_rate": matching_evidence.false_accept_rate,
            "gate_passed": matching_evidence.gate_passed,
        },
        "model": _model_report(
            engine,
            maximum_distance=matching_evidence.maximum_distance,
        ),
        "quality": {
            "detector_confidence_threshold": quality.detector_confidence_threshold,
            "minimum_face_size_pixels": quality.minimum_face_size_pixels,
            "maximum_faces_per_photo": quality.maximum_faces_per_photo,
        },
        "performance": {
            "runtime_threads": runtime_threads,
            "evaluated_images": len(dataset.images),
            "elapsed_seconds": round(observations.elapsed_seconds, 3),
            "images_per_second": round(images_per_second, 4),
            **projection,
            "peak_rss_mib": round(peak_rss_mib, 1),
            "event_projection_gate_passed": time_passes,
            "memory_gate_passed": memory_passes,
        },
        "detection": {
            "detected_faces": observations.detected_faces,
            "usable_faces": observations.usable_faces,
            "faces_below_confidence": observations.faces_below_confidence,
            "faces_below_minimum_size": observations.faces_below_minimum_size,
            "images_without_detection": observations.images_without_detection,
            "images_without_usable_face": observations.images_without_usable_face,
            "images_with_multiple_faces": observations.images_with_multiple_faces,
            "images_with_at_least_10_usable_faces": (
                observations.images_with_at_least_ten_usable_faces
            ),
            "maximum_usable_faces_in_one_image": (observations.maximum_usable_faces_in_one_image),
            "usable_faces_per_image_histogram": observations.usable_face_histogram,
            "dense_group_coverage_gate_passed": dense_group_passes,
            "processing_failures": observations.processing_failures,
        },
        "limitations": [
            "The representative dataset has no identity labels, so it cannot measure "
            "uniqueness, recall, or false accepts.",
            "The 100000-face runtime is a regression projection beyond the observed "
            "per-image face counts.",
        ],
    }


def _observe_representative(
    images: tuple[Path, ...],
    engine: FaceEngine,
    *,
    quality: FaceQualityRules,
    progress: ProgressReporter | None,
) -> RepresentativeObservations:
    detected_faces = 0
    usable_faces = 0
    faces_below_confidence = 0
    faces_below_minimum_size = 0
    images_without_detection = 0
    images_without_usable_face = 0
    images_with_multiple_faces = 0
    images_with_at_least_ten_usable_faces = 0
    maximum_usable_faces_in_one_image = 0
    processing_failures = 0
    elapsed_by_image = []
    detected_faces_by_image = []
    usable_counts = []
    benchmark_started = time.perf_counter()
    for index, path in enumerate(images):
        image_started = time.perf_counter()
        try:
            detected = tuple(engine.detect_and_embed(path.read_bytes()))
        except (FaceEngineError, OSError, ValueError):
            processing_failures += 1
            detected = ()
        elapsed_by_image.append(time.perf_counter() - image_started)
        detected_faces_by_image.append(len(detected))
        detected_faces += len(detected)
        images_without_detection += not detected
        images_with_multiple_faces += len(detected) > 1
        faces_below_confidence += sum(
            face.detector_confidence < quality.detector_confidence_threshold for face in detected
        )
        faces_below_minimum_size += sum(
            min(face.bounding_box_width, face.bounding_box_height)
            < quality.minimum_face_size_pixels
            for face in detected
        )
        usable = tuple(face for face in detected if _is_usable(face, quality))
        usable_count = len(usable)
        usable_counts.append(usable_count)
        usable_faces += usable_count
        images_without_usable_face += not usable
        images_with_at_least_ten_usable_faces += usable_count >= DENSE_GROUP_MINIMUM_USABLE_FACES
        maximum_usable_faces_in_one_image = max(
            maximum_usable_faces_in_one_image,
            usable_count,
        )
        if usable_count > quality.maximum_faces_per_photo:
            processing_failures += 1
        processed = index + 1
        if progress is not None and (processed % 10 == 0 or processed == len(images)):
            progress("representative", processed, len(images))
    return RepresentativeObservations(
        detected_faces=detected_faces,
        usable_faces=usable_faces,
        faces_below_confidence=faces_below_confidence,
        faces_below_minimum_size=faces_below_minimum_size,
        images_without_detection=images_without_detection,
        images_without_usable_face=images_without_usable_face,
        images_with_multiple_faces=images_with_multiple_faces,
        images_with_at_least_ten_usable_faces=images_with_at_least_ten_usable_faces,
        maximum_usable_faces_in_one_image=maximum_usable_faces_in_one_image,
        processing_failures=processing_failures,
        elapsed_seconds=time.perf_counter() - benchmark_started,
        elapsed_by_image=tuple(elapsed_by_image),
        detected_faces_by_image=tuple(detected_faces_by_image),
        usable_face_histogram=_usable_face_histogram(usable_counts),
    )


def _representative_runtime_projection(
    observations: RepresentativeObservations,
) -> dict[str, float | int | None]:
    elapsed = numpy.asarray(observations.elapsed_by_image, dtype=numpy.float64)
    detected = numpy.asarray(observations.detected_faces_by_image, dtype=numpy.float64)
    mean_elapsed = float(numpy.mean(elapsed))
    mean_detected = float(numpy.mean(detected))
    centered = detected - mean_detected
    variance = float(centered @ centered)
    if variance > 0.0:
        seconds_per_face = float(centered @ (elapsed - mean_elapsed) / variance)
        seconds_per_photo = mean_elapsed - seconds_per_face * mean_detected
    else:
        seconds_per_photo = mean_elapsed
        seconds_per_face = 0.0
    if seconds_per_face < 0.0:
        seconds_per_photo = mean_elapsed
        seconds_per_face = 0.0
    elif seconds_per_photo < 0.0:
        seconds_per_photo = 0.0
        denominator = float(detected @ detected)
        seconds_per_face = float(detected @ elapsed / denominator) if denominator else 0.0

    projected_hours = None
    projected_detected_faces = None
    if observations.usable_faces > 0:
        detected_per_usable = observations.detected_faces / observations.usable_faces
        projected_detected_faces = TARGET_USABLE_FACES * detected_per_usable
        projected_seconds = (
            TARGET_PHOTOS * seconds_per_photo + projected_detected_faces * seconds_per_face
        )
        projected_hours = projected_seconds / 3600.0
    return {
        "runtime_model_seconds_per_photo": round(seconds_per_photo, 6),
        "runtime_model_seconds_per_detected_face": round(seconds_per_face, 6),
        "runtime_model_observed_maximum_detected_faces": int(numpy.max(detected)),
        "projected_detected_faces_for_100000_usable_faces": (
            round(projected_detected_faces) if projected_detected_faces is not None else None
        ),
        "projected_10000_photos_100000_usable_faces_hours": (
            round(projected_hours, 3) if projected_hours is not None else None
        ),
    }


def _usable_face_histogram(counts: list[int]) -> dict[str, int]:
    return {
        "0": sum(count == 0 for count in counts),
        "1": sum(count == 1 for count in counts),
        "2-4": sum(2 <= count <= 4 for count in counts),
        "5-9": sum(5 <= count <= 9 for count in counts),
        "10-19": sum(10 <= count <= 19 for count in counts),
        "20+": sum(count >= 20 for count in counts),
    }


def _finite_report_number(raw: object, *, field: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise ValueError(f"matching evidence {field} must be finite")
    return float(raw)


def _load_lfw_split(csv_path: Path, image_directory: Path) -> tuple[LfwImage, ...]:
    if not csv_path.is_file() or not image_directory.is_dir():
        raise ValueError("required LFW benchmark inputs are missing")
    images = []
    with csv_path.open(newline="", encoding="utf-8") as metadata:
        rows = csv.DictReader(metadata)
        if rows.fieldnames != ["name", "images"]:
            raise ValueError("the LFW people metadata has an unexpected format")
        for identity, row in enumerate(rows):
            try:
                count = int(row["images"])
            except (TypeError, ValueError) as exc:
                raise ValueError("the LFW people metadata has an invalid image count") from exc
            if count < 1:
                raise ValueError("the LFW people metadata has an invalid image count")
            person = row["name"]
            for image_number in range(1, count + 1):
                path = image_directory / person / f"{person}_{image_number:04d}.jpg"
                if not path.is_file():
                    raise ValueError("an image declared by the LFW metadata is missing")
                images.append(LfwImage(identity=identity, path=path))
    return tuple(images)


def _sample_indexes(population: int, sample_size: int, seed: int) -> set[int]:
    return set(random.Random(seed).sample(range(population), sample_size))


def _observe_split(
    images: tuple[LfwImage, ...],
    engine: FaceEngine,
    *,
    quality: FaceQualityRules,
    sample_indexes: set[int],
    phase: str,
    progress: ProgressReporter | None,
) -> SplitObservations:
    vectors: defaultdict[int, list[numpy.ndarray | None]] = defaultdict(list)
    detected_faces = 0
    usable_faces = 0
    images_without_detection = 0
    images_without_usable_face = 0
    images_with_multiple_faces = 0
    processing_failures = 0
    sample_elapsed = 0.0
    sample_images = 0
    for index, image in enumerate(images):
        started = time.perf_counter()
        try:
            detected = tuple(engine.detect_and_embed(image.path.read_bytes()))
        except (FaceEngineError, OSError, ValueError):
            processing_failures += 1
            detected = ()
        elapsed = time.perf_counter() - started
        if index in sample_indexes:
            sample_elapsed += elapsed
            sample_images += 1
        detected_faces += len(detected)
        images_without_detection += not detected
        images_with_multiple_faces += len(detected) > 1
        usable = tuple(face for face in detected if _is_usable(face, quality))
        usable_faces += len(usable)
        images_without_usable_face += not usable
        if len(usable) > quality.maximum_faces_per_photo:
            processing_failures += 1
            vectors[image.identity].append(None)
        else:
            prominent = max(
                usable,
                key=lambda face: (
                    face.bounding_box_width * face.bounding_box_height,
                    face.detector_confidence,
                ),
                default=None,
            )
            vectors[image.identity].append(
                numpy.asarray(prominent.embedding.values, dtype=numpy.float32)
                if prominent is not None
                else None
            )
        processed = index + 1
        if progress is not None and (processed % 500 == 0 or processed == len(images)):
            progress(phase, processed, len(images))
    return SplitObservations(
        vectors_by_identity={identity: tuple(values) for identity, values in vectors.items()},
        detected_faces=detected_faces,
        usable_faces=usable_faces,
        images_without_detection=images_without_detection,
        images_without_usable_face=images_without_usable_face,
        images_with_multiple_faces=images_with_multiple_faces,
        processing_failures=processing_failures,
        sample_elapsed_seconds=sample_elapsed,
        sample_images=sample_images,
    )


def _is_usable(face: object, quality: FaceQualityRules) -> bool:
    return bool(
        face.detector_confidence >= quality.detector_confidence_threshold
        and min(face.bounding_box_width, face.bounding_box_height)
        >= quality.minimum_face_size_pixels
    )


def _positive_pair_count(vectors_by_identity: dict[int, tuple[numpy.ndarray | None, ...]]) -> int:
    return sum(len(vectors) * (len(vectors) - 1) // 2 for vectors in vectors_by_identity.values())


def _positive_distances(
    vectors_by_identity: dict[int, tuple[numpy.ndarray | None, ...]],
) -> numpy.ndarray:
    distances = []
    for vectors in vectors_by_identity.values():
        present = [vector for vector in vectors if vector is not None]
        if len(present) < 2:
            continue
        matrix = numpy.stack(present)
        similarities = matrix @ matrix.T
        rows, columns = numpy.triu_indices(len(present), k=1)
        distances.append(numpy.clip(1.0 - similarities[rows, columns], 0.0, 2.0))
    if not distances:
        return numpy.empty(0, dtype=numpy.float32)
    return numpy.concatenate(distances)


def _threshold_for_false_accept_rate(
    matrix: numpy.ndarray,
    identities: numpy.ndarray,
    *,
    maximum_false_accept_rate: float,
    block_size: int = 256,
) -> float:
    negative_comparisons = _negative_comparison_count(identities)
    if negative_comparisons < 1:
        raise ValueError("the calibration split has no cross-identity comparisons")
    allowed_false_accepts = math.floor(negative_comparisons * maximum_false_accept_rate)
    if allowed_false_accepts >= negative_comparisons:
        return 2.0
    closest = _closest_negative_distances(
        matrix,
        identities,
        count=allowed_false_accepts + 1,
        block_size=block_size,
    )
    first_rejected_distance = numpy.max(closest)
    return float(
        numpy.nextafter(
            numpy.float32(first_rejected_distance),
            numpy.float32(-numpy.inf),
        )
    )


def _closest_negative_distances(
    matrix: numpy.ndarray,
    identities: numpy.ndarray,
    *,
    count: int,
    block_size: int,
) -> numpy.ndarray:
    closest = numpy.empty(0, dtype=numpy.float32)
    columns = numpy.arange(len(matrix))
    for start in range(0, len(matrix), block_size):
        stop = min(start + block_size, len(matrix))
        similarities = matrix[start:stop] @ matrix.T
        rows = numpy.arange(start, stop)[:, None]
        candidates = (columns > rows) & (identities[None, :] != identities[start:stop, None])
        distances = numpy.clip(1.0 - similarities[candidates], 0.0, 2.0)
        if len(distances) > count:
            distances = numpy.partition(distances, count - 1)[:count]
        closest = numpy.concatenate((closest, distances))
        if len(closest) > count:
            closest = numpy.partition(closest, count - 1)[:count]
    if len(closest) < count:
        raise ValueError("the calibration split has too few cross-identity comparisons")
    return closest


def _matching_metrics(
    vectors_by_identity: dict[int, tuple[numpy.ndarray | None, ...]],
    *,
    maximum_distance: float,
) -> dict[str, int | float]:
    positive_pair_count = _positive_pair_count(vectors_by_identity)
    positive_distances = _positive_distances(vectors_by_identity)
    true_accepts = int(numpy.count_nonzero(positive_distances <= maximum_distance))
    matrix, identities = _present_matrix(vectors_by_identity)
    negative_comparisons, false_accepts = _negative_matches(
        matrix,
        identities,
        maximum_distance=maximum_distance,
    )
    recall = true_accepts / positive_pair_count if positive_pair_count else 0.0
    false_accept_rate = false_accepts / negative_comparisons if negative_comparisons else 0.0
    expected_false_comparisons = false_accept_rate * TARGET_USABLE_FACES
    return {
        "positive_pairs": positive_pair_count,
        "positive_pairs_with_two_usable_faces": len(positive_distances),
        "true_accepts": true_accepts,
        "recall": round(recall, 8),
        "negative_comparisons": negative_comparisons,
        "false_accepts": false_accepts,
        "false_accept_rate": round(false_accept_rate, 10),
        "estimated_false_comparisons_per_100000_faces": round(expected_false_comparisons, 3),
    }


def _present_matrix(
    vectors_by_identity: dict[int, tuple[numpy.ndarray | None, ...]],
) -> tuple[numpy.ndarray, numpy.ndarray]:
    vectors = []
    identities = []
    for identity, values in vectors_by_identity.items():
        for value in values:
            if value is not None:
                vectors.append(value)
                identities.append(identity)
    if not vectors:
        return numpy.empty((0, 0), dtype=numpy.float32), numpy.empty(0, dtype=numpy.int32)
    return numpy.stack(vectors), numpy.asarray(identities, dtype=numpy.int32)


def _negative_matches(
    matrix: numpy.ndarray,
    identities: numpy.ndarray,
    *,
    maximum_distance: float,
    block_size: int = 256,
) -> tuple[int, int]:
    if len(matrix) < 2:
        return 0, 0
    false_accepts = 0
    similarity_threshold = 1.0 - maximum_distance
    for start in range(0, len(matrix), block_size):
        stop = min(start + block_size, len(matrix))
        similarities = matrix[start:stop] @ matrix.T
        for local_row, global_row in enumerate(range(start, stop)):
            later = slice(global_row + 1, len(matrix))
            different_identity = identities[later] != identities[global_row]
            false_accepts += int(
                numpy.count_nonzero(
                    different_identity
                    & (similarities[local_row, global_row + 1 :] >= similarity_threshold)
                )
            )
    return _negative_comparison_count(identities), false_accepts


def _negative_comparison_count(identities: numpy.ndarray) -> int:
    total_pairs = len(identities) * (len(identities) - 1) // 2
    identity_counts = numpy.unique(identities, return_counts=True)[1]
    positive_pairs = int(numpy.sum(identity_counts * (identity_counts - 1) // 2))
    return total_pairs - positive_pairs


def _detection_report(calibration: SplitObservations, holdout: SplitObservations) -> dict[str, int]:
    return {
        "detected_faces": calibration.detected_faces + holdout.detected_faces,
        "usable_faces": calibration.usable_faces + holdout.usable_faces,
        "images_without_detection": (
            calibration.images_without_detection + holdout.images_without_detection
        ),
        "images_without_usable_face": (
            calibration.images_without_usable_face + holdout.images_without_usable_face
        ),
        "images_with_multiple_faces": (
            calibration.images_with_multiple_faces + holdout.images_with_multiple_faces
        ),
        "processing_failures": calibration.processing_failures + holdout.processing_failures,
    }


def _model_report(engine: FaceEngine, *, maximum_distance: float) -> dict[str, object]:
    return {
        "id": engine.model.id,
        "detector": engine.model.detector,
        "recognizer": engine.model.recognizer,
        "embedding_dimensions": engine.model.embedding_dimensions,
        "artifacts": [
            {
                "filename": artifact.filename,
                "sha256": artifact.sha256,
                "license_id": artifact.license_id,
            }
            for artifact in engine.model.artifacts
        ],
        "licensing": {
            "status": engine.model.rights.status,
            "basis": engine.model.rights.basis,
            "permission_evidence_private": engine.model.rights.permission_evidence_private,
        },
        "preprocessing": engine.model.preprocessing,
        "detector_input_size": list(engine.model.detector_input_size),
        "normalization": "l2",
        "normalization_tolerance": NORMALIZATION_TOLERANCE,
        "distance_metric": "cosine",
        "recommended_maximum_distance": round(maximum_distance, 8),
        "runtime_versions": engine.runtime_versions,
    }


def _peak_rss_mib() -> float:
    maximum_rss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return maximum_rss / (1024 * 1024) if platform.system() == "Darwin" else maximum_rss / 1024


def _hardware_report() -> dict[str, object]:
    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu": _cpu_model(),
        "logical_cpu_count": os.cpu_count(),
        "memory_mib": _physical_memory_mib(),
        "numpy": numpy.__version__,
    }


def _cpu_model() -> str:
    if platform.system() == "Linux":
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("model name"):
                    return line.partition(":")[2].strip()
        except OSError:
            pass
    return platform.processor() or "unknown"


def _physical_memory_mib() -> int | None:
    try:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 * 1024))
    except (OSError, ValueError):
        return None


def _build_engine(arguments: argparse.Namespace) -> FaceEngine:
    common = {
        "detector_path": arguments.detector_model,
        "recognizer_path": arguments.recognizer_model,
        "detector_input_size": (arguments.detector_size, arguments.detector_size),
        "detector_floor": arguments.detector_floor,
        "runtime_threads": arguments.runtime_threads,
    }
    if arguments.engine == "sface":
        return OpenCvSFaceEngine(**common)
    return InsightFaceEngine(**common)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lfw-metadata", type=Path)
    parser.add_argument("--lfw-images", type=Path)
    parser.add_argument("--representative-images", type=Path)
    parser.add_argument("--matching-report", type=Path)
    parser.add_argument("--consent-confirmed", action="store_true")
    parser.add_argument("--engine", choices=("sface", "insightface"), required=True)
    parser.add_argument("--detector-model", type=Path, required=True)
    parser.add_argument("--recognizer-model", type=Path, required=True)
    parser.add_argument("--detector-size", type=int, default=640)
    parser.add_argument("--detector-floor", type=float, default=0.5)
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--runtime-threads", type=int, default=DEFAULT_RUNTIME_THREADS)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    representative_mode = arguments.representative_images is not None
    if representative_mode:
        if arguments.lfw_metadata is not None or arguments.lfw_images is not None:
            parser.error("representative and LFW inputs cannot be combined")
        if arguments.matching_report is None:
            parser.error("--matching-report is required for representative data")
        if not arguments.consent_confirmed:
            parser.error("--consent-confirmed is required for representative data")
        mode = "representative"
    else:
        if arguments.lfw_metadata is None or arguments.lfw_images is None:
            parser.error("--lfw-metadata and --lfw-images are required for LFW data")
        if arguments.matching_report is not None or arguments.consent_confirmed:
            parser.error("matching evidence and consent flags apply only to representative data")
        mode = "lfw"
    engine = _build_engine(arguments)
    if representative_mode:
        dataset = load_representative_dataset(
            image_directory=arguments.representative_images,
            maximum_images=arguments.sample_size,
        )
        matching_evidence = load_matching_evidence(arguments.matching_report, engine)
    else:
        dataset = load_lfw_dataset(
            metadata_directory=arguments.lfw_metadata,
            image_directory=arguments.lfw_images,
        )
        matching_evidence = None
    print(
        f"face benchmark started: mode={mode} candidate={arguments.engine} pid={os.getpid()}",
        file=sys.stderr,
        flush=True,
    )
    if representative_mode:
        report = run_representative_benchmark(
            dataset,
            engine,
            matching_evidence,
            runtime_threads=arguments.runtime_threads,
            progress=_print_progress,
        )
    else:
        report = run_lfw_benchmark(
            dataset,
            engine,
            sample_size=arguments.sample_size,
            runtime_threads=arguments.runtime_threads,
            progress=_print_progress,
        )
    _write_report(arguments.output, report)
    print(
        f"face benchmark completed: mode={mode} candidate={arguments.engine} pid={os.getpid()}",
        file=sys.stderr,
        flush=True,
    )
    return 0


def _print_progress(phase: str, processed: int, total: int) -> None:
    print(
        f"face benchmark progress: phase={phase} processed={processed}/{total}",
        file=sys.stderr,
        flush=True,
    )


def _write_report(output: Path, report: dict[str, object]) -> None:
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(report, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.replace(output)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
