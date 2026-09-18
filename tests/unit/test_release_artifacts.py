import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts" / "release_artifacts.py"


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=REPOSITORY,
        check=False,
        capture_output=True,
        text=True,
    )


def test_release_tag_must_exactly_match_project_version() -> None:
    accepted = _run("verify-tag", "v0.1.0")
    rejected = _run("verify-tag", "v0.1.1")

    assert accepted.returncode == 0
    assert rejected.returncode != 0
    assert "release tag must be v0.1.0" in rejected.stderr


def test_windows_archive_and_checksums_are_named_for_release(tmp_path: Path) -> None:
    source = tmp_path / "application"
    source.mkdir()
    (source / "OpenFotos.exe").write_bytes(b"synthetic executable")
    release = tmp_path / "release"
    release.mkdir()

    archived = _run("archive", "windows", str(source), str(release))
    checksummed = _run("checksums", str(release))

    assert archived.returncode == 0, archived.stderr
    assert checksummed.returncode == 0, checksummed.stderr
    artifact = release / "OpenFotos-0.1.0-windows-x64.zip"
    with zipfile.ZipFile(artifact) as bundle:
        assert bundle.read("OpenFotos/OpenFotos.exe") == b"synthetic executable"
    expected = hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert (release / "SHA256SUMS").read_text() == (
        f"{expected}  OpenFotos-0.1.0-windows-x64.zip\n"
    )
