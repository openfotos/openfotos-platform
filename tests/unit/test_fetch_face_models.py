import hashlib
import importlib.util
from io import BytesIO
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[2] / "scripts" / "fetch_face_models.py"
_SPEC = importlib.util.spec_from_file_location("fetch_face_models", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
fetch_face_models = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fetch_face_models)


class Response(BytesIO):
    def geturl(self):
        return "https://models.invalid/accepted.onnx"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_model_sources_use_githubs_lfs_aware_https_endpoint() -> None:
    assert fetch_face_models.UPSTREAM_ROOT == (
        "https://github.com/opencv/opencv_zoo/raw/main/models"
    )
    assert all(
        artifact.url.startswith(fetch_face_models.UPSTREAM_ROOT)
        for artifact in fetch_face_models.ARTIFACTS
    )


def test_verified_download_is_installed_atomically(tmp_path: Path, monkeypatch) -> None:
    content = b"synthetic accepted model"
    artifact = fetch_face_models.Artifact(
        filename="accepted.onnx",
        sha256=hashlib.sha256(content).hexdigest(),
        url="https://models.invalid/accepted.onnx",
    )
    monkeypatch.setattr(fetch_face_models, "ARTIFACTS", (artifact,))

    fetch_face_models.fetch_models(
        tmp_path,
        opener=lambda _request, timeout: Response(content),
    )

    installed = tmp_path / artifact.filename
    assert installed.read_bytes() == content
    assert installed.stat().st_mode & 0o222 == 0
    assert not list(tmp_path.glob("*.part"))


def test_hash_mismatch_leaves_no_model_or_partial_file(tmp_path: Path, monkeypatch) -> None:
    artifact = fetch_face_models.Artifact(
        filename="rejected.onnx",
        sha256="0" * 64,
        url="https://models.invalid/rejected.onnx",
    )
    monkeypatch.setattr(fetch_face_models, "ARTIFACTS", (artifact,))

    with pytest.raises(RuntimeError, match="hash mismatch"):
        fetch_face_models.fetch_models(
            tmp_path,
            opener=lambda _request, timeout: Response(b"wrong model"),
        )

    assert not (tmp_path / artifact.filename).exists()
    assert not list(tmp_path.glob("*.part"))
