#!/usr/bin/env python3
"""Download the accepted OpenCV models and install them only after SHA-256 verification."""

import argparse
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

CHUNK_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
UPSTREAM_ROOT = "https://github.com/opencv/opencv_zoo/raw/main/models"


@dataclass(frozen=True, slots=True)
class Artifact:
    filename: str
    sha256: str
    url: str


ARTIFACTS = (
    Artifact(
        filename="face_detection_yunet_2023mar.onnx",
        sha256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        url=f"{UPSTREAM_ROOT}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    ),
    Artifact(
        filename="face_recognition_sface_2021dec.onnx",
        sha256="0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
        url=f"{UPSTREAM_ROOT}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
    ),
)


def fetch_models(destination: Path, *, opener=urlopen) -> None:
    destination.mkdir(parents=True, exist_ok=True, mode=0o755)
    for artifact in ARTIFACTS:
        target = destination / artifact.filename
        if _sha256(target) == artifact.sha256:
            continue
        _download_verified(artifact, target, opener=opener)


def _download_verified(artifact: Artifact, target: Path, *, opener) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{artifact.filename}-",
        suffix=".part",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    written = 0
    try:
        request = Request(artifact.url, headers={"User-Agent": "OneNodeAI-Studio-model-fetch/1"})
        with opener(request, timeout=120) as response, os.fdopen(descriptor, "wb") as output:
            final_url = response.geturl()
            if urlparse(final_url).scheme != "https":
                raise RuntimeError(f"Model download for {artifact.filename} left HTTPS.")
            while chunk := response.read(CHUNK_BYTES):
                written += len(chunk)
                if written > MAX_ARTIFACT_BYTES:
                    raise RuntimeError(f"Model download for {artifact.filename} is too large.")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != artifact.sha256:
            raise RuntimeError(f"Model hash mismatch for {artifact.filename}.")
        temporary.chmod(0o444)
        os.replace(temporary, target)
    except Exception:
        os.close(descriptor) if _descriptor_is_open(descriptor) else None
        temporary.unlink(missing_ok=True)
        raise


def _descriptor_is_open(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
        return True
    except OSError:
        return False


def _sha256(path: Path) -> str:
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    arguments = parser.parse_args()
    fetch_models(arguments.destination)
    for artifact in ARTIFACTS:
        print(f"Verified {artifact.filename} ({artifact.sha256})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
