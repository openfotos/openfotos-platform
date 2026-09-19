import hashlib
import threading
from pathlib import Path

import httpx
import pytest

import openfotos_desktop.face_models as face_models
from openfotos_desktop.face_models import FaceModelSetupError, FaceModelStore


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


def test_wait_for_download_reports_readiness_and_blocks_active_downloads(
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
    release = threading.Event()
    started = threading.Event()

    def response(request: httpx.Request) -> httpx.Response:
        started.set()
        assert release.wait(5)
        filename = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=contents[filename], request=request)

    store = FaceModelStore(tmp_path / "private")
    assert store.wait_for_download(0.01) is False
    download = threading.Thread(
        target=lambda: store.download(client=httpx.Client(transport=httpx.MockTransport(response)))
    )
    download.start()
    assert started.wait(5)
    waiter_result: list[bool] = []
    waiter = threading.Thread(
        target=lambda: waiter_result.append(store.wait_for_download(5)),
    )
    waiter.start()
    assert waiter.is_alive()
    release.set()
    download.join(5)
    waiter.join(5)

    assert waiter_result == [True]
    assert store.wait_for_download(0.01) is True
    assert store.verified_paths() is not None


def test_cancelled_download_leaves_no_partial_artifact(tmp_path: Path) -> None:
    store = FaceModelStore(tmp_path / "private")
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"partial", request=request)
        )
    )

    with pytest.raises(FaceModelSetupError) as raised:
        store.download(client=client, is_cancelled=lambda: True)

    assert raised.value.code == "face_model_download_cancelled"
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
