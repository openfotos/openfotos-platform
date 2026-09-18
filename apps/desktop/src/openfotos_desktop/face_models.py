"""Private, hash-verified acquisition of the accepted face-model artifacts."""

from __future__ import annotations

import hashlib
import os
import tempfile
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
                "Download or locate the accepted face models in Desktop settings.",
            )
        return paths

    def install_from_directory(self, source_directory: Path) -> FaceModelPaths:
        source_directory = Path(source_directory)
        temporary: list[tuple[Path, Path]] = []
        self._prepare_directory()
        try:
            for artifact in _ARTIFACT_SOURCES:
                source = source_directory / artifact.filename
                temporary.append(
                    (
                        self._verified_temporary_copy(source, artifact),
                        self.directory / artifact.filename,
                    )
                )
            _install_temporaries(temporary)
        finally:
            for temporary_path, _ in temporary:
                temporary_path.unlink(missing_ok=True)
        return self.require_paths()

    def download(
        self,
        *,
        client: httpx.Client | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> FaceModelPaths:
        existing = self.verified_paths()
        if existing is not None:
            return existing
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
                        self._download_verified(active_client, artifact),
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

    def _verified_temporary_copy(self, source: Path, artifact: _ArtifactSource) -> Path:
        if not source.is_file():
            raise FaceModelSetupError(
                "face_model_missing",
                f"The selected folder does not contain {artifact.filename}.",
            )
        try:
            with source.open("rb") as input_file:
                return self._write_verified_stream(input_file, artifact)
        except OSError as exc:
            raise FaceModelSetupError(
                "face_model_unreadable", "A selected face-model file could not be read."
            ) from exc

    def _download_verified(self, client: httpx.Client, artifact: _ArtifactSource) -> Path:
        try:
            with client.stream("GET", artifact.url) as response:
                response.raise_for_status()
                if response.url.scheme != "https":
                    raise FaceModelSetupError(
                        "face_model_download_insecure",
                        "The face-model download left the required HTTPS transport.",
                    )
                return self._write_verified_stream(response.iter_bytes(), artifact)
        except FaceModelSetupError:
            raise
        except httpx.HTTPError as exc:
            raise FaceModelSetupError(
                "face_model_download_failed",
                "The accepted face models could not be downloaded. Try again or locate them.",
            ) from exc

    def _write_verified_stream(self, source, artifact: _ArtifactSource) -> Path:
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
    paths = (store or FaceModelStore()).require_paths()
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
