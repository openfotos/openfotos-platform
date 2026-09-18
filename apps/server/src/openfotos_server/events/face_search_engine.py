"""Lazy, serialized access to the accepted server-side reference-photo engine."""

from functools import lru_cache
from pathlib import Path
from threading import Lock

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from openfotos_vision import ACCEPTED_SFACE_DETECTOR_FLOOR, OpenCvSFaceEngine


class SerializedFaceEngine:
    def __init__(self, engine: OpenCvSFaceEngine) -> None:
        self._engine = engine
        self._lock = Lock()

    def detect_and_embed(self, image_bytes: bytes):
        with self._lock:
            return self._engine.detect_and_embed(image_bytes)


@lru_cache(maxsize=1)
def configured_face_search_engine() -> SerializedFaceEngine:
    if not settings.FACE_DETECTOR_MODEL_PATH or not settings.FACE_RECOGNIZER_MODEL_PATH:
        raise ImproperlyConfigured(
            "Face search is unavailable; configure FACE_DETECTOR_MODEL_PATH and "
            "FACE_RECOGNIZER_MODEL_PATH."
        )
    engine = OpenCvSFaceEngine(
        detector_path=Path(settings.FACE_DETECTOR_MODEL_PATH),
        recognizer_path=Path(settings.FACE_RECOGNIZER_MODEL_PATH),
        detector_floor=ACCEPTED_SFACE_DETECTOR_FLOOR,
        runtime_threads=1,
    )
    return SerializedFaceEngine(engine)
