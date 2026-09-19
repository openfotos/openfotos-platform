"""Private, hash-verified acquisition of the accepted face-model artifacts."""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from openfotos_vision import (
    ACCEPTED_FACE_MODEL_CONTRACT,
    ACCEPTED_SFACE_DETECTOR_FLOOR,
    OpenCvSFaceEngine,
)

from .paths import user_data_directory

_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_UPSTREAM_ROOT = "https://github.com/opencv/opencv_zoo/raw/main/models"


class FaceModelSetupError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class FaceModelPaths:
    detector: Path
    recognizer: Path


@dataclass(frozen=True, slots=True)
class _ArtifactSource:
    filename: str
    sha256: str
    url: str


_ARTIFACT_SOURCES = (
    _ArtifactSource(
        filename=ACCEPTED_FACE_MODEL_CONTRACT.model.artifacts[0].filename,
        sha256=ACCEPTED_FACE_MODEL_CONTRACT.model.artifacts[0].sha256,
        url=(f"{_UPSTREAM_ROOT}/face_detection_yunet/face_detection_yunet_2023mar.onnx"),
    ),
    _ArtifactSource(
        filename=ACCEPTED_FACE_MODEL_CONTRACT.model.artifacts[1].filename,
        sha256=ACCEPTED_FACE_MODEL_CONTRACT.model.artifacts[1].sha256,
        url=(f"{_UPSTREAM_ROOT}/face_recognition_sface/face_recognition_sface_2021dec.onnx"),
    ),
)


class FaceModelStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = (
            Path(directory)
            if directory
            else (user_data_directory() / "models" / ACCEPTED_FACE_MODEL_CONTRACT.model.id)
        )
        self._state = threading.Condition()
        self._download_active = False

    @property
    def download_in_progress(self) -> bool:
        with self._state:
            return self._download_active

    def wait_for_download(self, timeout: float | None = None) -> bool:
        """Block while an automatic download runs, then report whether models are ready."""
        with self._state:
            finished = self._state.wait_for(lambda: not self._download_active, timeout)
        return finished and self.verified_paths() is not None

    def verified_paths(self) -> FaceModelPaths | None:
        paths = tuple(self.directory / artifact.filename for artifact in _ARTIFACT_SOURCES)
        if not all(
            path.is_file() and _file_sha256(path) == artifact.sha256
            for path, artifact in zip(paths, _ARTIFACT_SOURCES, strict=True)
        ):
            return None
        return FaceModelPaths(detector=paths[0], recognizer=paths[1])

    def require_paths(self) -> FaceModelPaths:
        paths = self.verified_paths()
        if paths is None:
            raise FaceModelSetupError(
                "face_model_setup_required",
                "The accepted face models have not finished downloading. Retry the download.",
            )
        return paths

    def download(
        self,
        *,
        client: httpx.Client | None = None,
        progress: Callable[[int, int], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> FaceModelPaths:
        with self._state:
            while self._download_active:
                self._state.wait()
            existing = self.verified_paths()
            if existing is not None:
                return existing
            self._download_active = True
        try:
            return self._run_download(
                client=client,
                progress=progress,
                is_cancelled=is_cancelled,
            )
        finally:
            with self._state:
                self._download_active = False
                self._state.notify_all()

    def _run_download(
        self,
        *,
        client: httpx.Client | None,
        progress: Callable[[int, int], None] | None,
        is_cancelled: Callable[[], bool] | None,
    ) -> FaceModelPaths:
        self._prepare_directory()
        owns_client = client is None
        active_client = client or httpx.Client(
            timeout=httpx.Timeout(120, connect=15),
            follow_redirects=True,
        )
        temporary: list[tuple[Path, Path]] = []
        try:
            for index, artifact in enumerate(_ARTIFACT_SOURCES, start=1):
                temporary.append(
                    (
                        self._download_verified(active_client, artifact, is_cancelled),
                        self.directory / artifact.filename,
                    )
                )
                if progress is not None:
                    progress(index, len(_ARTIFACT_SOURCES))
            _install_temporaries(temporary)
        finally:
            for temporary_path, _ in temporary:
                temporary_path.unlink(missing_ok=True)
            if owns_client:
                active_client.close()
        return self.require_paths()

    def _prepare_directory(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.directory.chmod(0o700)

    def _download_verified(
        self,
        client: httpx.Client,
        artifact: _ArtifactSource,
        is_cancelled: Callable[[], bool] | None,
    ) -> Path:
        try:
            with client.stream("GET", artifact.url) as response:
                response.raise_for_status()
                if response.url.scheme != "https":
                    raise FaceModelSetupError(
                        "face_model_download_insecure",
                        "The face-model download left the required HTTPS transport.",
                    )
                return self._write_verified_stream(
                    response.iter_bytes(),
                    artifact,
                    is_cancelled=is_cancelled,
                )
        except FaceModelSetupError:
            raise
        except httpx.HTTPError as exc:
            raise FaceModelSetupError(
                "face_model_download_failed",
                "The accepted face models could not be downloaded. Try again or locate them.",
            ) from exc

    def _write_verified_stream(
        self,
        source,
        artifact: _ArtifactSource,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Path:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{artifact.filename}-", suffix=".part", dir=self.directory
        )
        temporary = Path(temporary_name)
        if os.name != "nt":
            temporary.chmod(0o600)
        digest = hashlib.sha256()
        written = 0
        try:
            with os.fdopen(descriptor, "wb") as destination:
                iterator = (
                    iter(lambda: source.read(_DOWNLOAD_CHUNK_BYTES), b"")
                    if hasattr(source, "read")
                    else iter(source)
                )
                for chunk in iterator:
                    if is_cancelled is not None and is_cancelled():
                        raise FaceModelSetupError(
                            "face_model_download_cancelled",
                            "The face-model download was cancelled.",
                        )
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > _MAX_ARTIFACT_BYTES:
                        raise FaceModelSetupError(
                            "face_model_too_large",
                            "A face-model download exceeded the accepted size limit.",
                        )
                    digest.update(chunk)
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            if digest.hexdigest() != artifact.sha256:
                raise FaceModelSetupError(
                    "face_model_hash_mismatch",
                    "A face-model file did not match the accepted SHA-256 hash.",
                )
            return temporary
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


def create_accepted_face_engine(store: FaceModelStore | None = None) -> OpenCvSFaceEngine:
    active_store = store or FaceModelStore()
    # Only the face-embedding stage reaches this factory, so this is the one place that waits
    # for the automatic launch download instead of failing mid-batch.
    active_store.wait_for_download()
    paths = active_store.require_paths()
    return OpenCvSFaceEngine(
        detector_path=paths.detector,
        recognizer_path=paths.recognizer,
        detector_floor=ACCEPTED_SFACE_DETECTOR_FLOOR,
        runtime_threads=1,
    )


def _install_temporaries(values: list[tuple[Path, Path]]) -> None:
    for temporary, destination in values:
        os.replace(temporary, destination)
        if os.name != "nt":
            destination.chmod(0o600)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(_DOWNLOAD_CHUNK_BYTES), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()
