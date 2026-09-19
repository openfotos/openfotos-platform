import hashlib
import struct
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts" / "release_artifacts.py"


def _project_version() -> str:
    with (REPOSITORY / "pyproject.toml").open("rb") as source:
        return tomllib.load(source)["project"]["version"]


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=REPOSITORY,
        check=False,
        capture_output=True,
        text=True,
    )


def test_release_tag_must_exactly_match_project_version() -> None:
    version = _project_version()
    accepted = _run("verify-tag", f"v{version}")
    rejected = _run("verify-tag", f"v{version}-wrong")

    assert accepted.returncode == 0
    assert rejected.returncode != 0
    assert f"release tag must be v{version}" in rejected.stderr


def test_windows_archive_and_checksums_are_named_for_release(tmp_path: Path) -> None:
    version = _project_version()
    source = tmp_path / "application"
    source.mkdir()
    (source / "OneNodeAIStudio.exe").write_bytes(b"synthetic executable")
    release = tmp_path / "release"
    release.mkdir()

    archived = _run("archive", "windows", str(source), str(release))
    checksummed = _run("checksums", str(release))

    assert archived.returncode == 0, archived.stderr
    assert checksummed.returncode == 0, checksummed.stderr
    artifact = release / f"OneNodeAIStudio-{version}-windows-x64.zip"
    with zipfile.ZipFile(artifact) as bundle:
        assert bundle.read("OneNodeAIStudio/OneNodeAIStudio.exe") == b"synthetic executable"
    expected = hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert (release / "SHA256SUMS").read_text() == f"{expected}  {artifact.name}\n"


def test_installer_icons_are_valid_for_the_studio_identity() -> None:
    icons = REPOSITORY / "packaging" / "icons"
    ico = (icons / "onenodeai-studio.ico").read_bytes()
    icns = (icons / "onenodeai-studio.icns").read_bytes()

    assert ico[:4] == b"\x00\x00\x01\x00"
    count = struct.unpack("<H", ico[4:6])[0]
    assert count >= 5
    assert icns[:4] == b"icns"
    assert struct.unpack(">I", icns[4:8])[0] == len(icns)
    assert b"icp4" in icns and b"ic10" in icns


def test_icon_build_puts_the_extracted_mark_on_a_white_disc(tmp_path: Path) -> None:
    from PIL import Image, ImageDraw

    source = tmp_path / "logo.png"
    image = Image.new("RGB", (64, 64), (10, 10, 10))
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, 56, 56), outline=(240, 240, 240), width=4)
    draw.ellipse((28, 28, 36, 36), fill=(240, 240, 240))
    image.save(source)
    icons = tmp_path / "icons"
    runtime = tmp_path / "runtime.png"

    built = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY / "scripts" / "build_app_icons.py"),
            "--source",
            str(source),
            "--destination",
            str(icons),
            "--runtime-destination",
            str(runtime),
        ],
        cwd=REPOSITORY,
        check=False,
        capture_output=True,
        text=True,
    )

    assert built.returncode == 0, built.stderr
    with Image.open(runtime) as opened:
        badge = opened.convert("RGBA")
        assert badge.getpixel((0, 0))[3] == 0
        assert badge.getpixel((round(badge.width * 0.1), badge.height // 2)) == (
            255,
            255,
            255,
            255,
        )
        center = badge.getpixel((badge.width // 2, badge.height // 2))
        assert center[3] == 255
        assert max(center[:3]) < 60
    assert (icons / "onenodeai-studio.ico").read_bytes()[:4] == b"\x00\x00\x01\x00"
    assert (icons / "onenodeai-studio.icns").read_bytes()[:4] == b"icns"
