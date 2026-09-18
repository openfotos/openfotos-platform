import hashlib
from pathlib import Path

import httpx
import pytest

import openfotos_desktop.face_models as face_models
from openfotos_desktop.face_models import FaceModelSetupError, FaceModelStore
from openfotos_vision import ACCEPTED_FACE_MODEL_CONTRACT


def _write_accepted_source(directory: Path) -> None:
    directory.mkdir()
    for index, artifact in enumerate(ACCEPTED_FACE_MODEL_CONTRACT.model.artifacts):
        content = f"synthetic-{index}".encode()
        (directory / artifact.filename).write_bytes(content)


def test_located_models_are_verified_before_atomic_install(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "private"
    _write_accepted_source(source)
    artifacts = ACCEPTED_FACE_MODEL_CONTRACT.model.artifacts
    expected = tuple(
        hashlib.sha256((source / artifact.filename).read_bytes()).hexdigest()
        for artifact in artifacts
    )
    monkeypatch.setattr(
        face_models,
        "_ARTIFACT_SOURCES",
        tuple(
            face_models._ArtifactSource(
                filename=value.filename,
                sha256=digest,
                url=value.url,
            )
            for value, digest in zip(
                face_models._ARTIFACT_SOURCES,
                expected,
                strict=True,
            )
        ),
    )

    paths = FaceModelStore(destination).install_from_directory(source)

    assert paths.detector.parent == destination
    assert paths.recognizer.parent == destination
    assert not list(destination.glob("*.part"))


def test_hash_mismatch_leaves_no_installed_or_partial_artifact(tmp_path: Path) -> None:
    store = FaceModelStore(tmp_path / "private")
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=b"not-an-accepted-model",
                request=request,
            )
        )
    )

    with pytest.raises(FaceModelSetupError) as raised:
        store.download(client=client)

    assert raised.value.code == "face_model_hash_mismatch"
    assert store.verified_paths() is None
    assert not list(store.directory.glob("*.part"))


def test_download_reports_progress_and_installs_only_verified_files(
    tmp_path: Path, monkeypatch
) -> None:
    contents = {
        artifact.filename: f"downloaded-{index}".encode()
        for index, artifact in enumerate(face_models._ARTIFACT_SOURCES)
    }
    monkeypatch.setattr(
        face_models,
        "_ARTIFACT_SOURCES",
        tuple(
            face_models._ArtifactSource(
                filename=artifact.filename,
                sha256=hashlib.sha256(contents[artifact.filename]).hexdigest(),
                url=artifact.url,
            )
            for artifact in face_models._ARTIFACT_SOURCES
        ),
    )

    def response(request: httpx.Request) -> httpx.Response:
        filename = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=contents[filename], request=request)

    progress = []
    store = FaceModelStore(tmp_path / "private")
    paths = store.download(
        client=httpx.Client(transport=httpx.MockTransport(response)),
        progress=lambda completed, total: progress.append((completed, total)),
    )

    assert progress == [(1, 2), (2, 2)]
    assert store.verified_paths() == paths
    assert not list(store.directory.glob("*.part"))
    for path in (paths.detector, paths.recognizer):
        assert path.stat().st_mode & 0o077 == 0
