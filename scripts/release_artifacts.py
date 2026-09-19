"""Validate a release tag and assemble deterministic release metadata."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import tarfile
import tomllib
import zipfile
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]


def _project_version() -> str:
    with (REPOSITORY / "pyproject.toml").open("rb") as source:
        return tomllib.load(source)["project"]["version"]


def verify_tag(tag: str) -> None:
    expected = f"v{_project_version()}"
    if tag != expected:
        raise SystemExit(f"release tag must be {expected}, received {tag}")


def _replace_output(path: Path) -> None:
    if path.exists():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def archive_windows(source: Path, destination: Path) -> None:
    output = destination / f"OneNodeAIStudio-{_project_version()}-windows-x64.zip"
    _replace_output(output)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, Path("OneNodeAIStudio") / path.relative_to(source))


def archive_macos(source: Path, destination: Path) -> None:
    output = destination / f"OneNodeAIStudio-{_project_version()}-macos-arm64.tar.gz"
    _replace_output(output)
    with tarfile.open(output, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        archive.add(source, arcname="OneNodeAI Studio.app", recursive=True)


def write_checksums(directory: Path) -> None:
    artifacts = sorted(
        path for path in directory.iterdir() if path.is_file() and path.name != "SHA256SUMS"
    )
    if not artifacts:
        raise SystemExit("no release artifacts found")
    lines = []
    for artifact in artifacts:
        digest = hashlib.sha256()
        with artifact.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        lines.append(f"{digest.hexdigest()}  {artifact.name}\n")
    (directory / "SHA256SUMS").write_text("".join(lines), encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    verify = subcommands.add_parser("verify-tag")
    verify.add_argument("tag")
    archive = subcommands.add_parser("archive")
    archive.add_argument("platform", choices=("windows", "macos"))
    archive.add_argument("source", type=Path)
    archive.add_argument("destination", type=Path)
    checksums = subcommands.add_parser("checksums")
    checksums.add_argument("directory", type=Path)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    if arguments.command == "verify-tag":
        verify_tag(arguments.tag)
        return
    if arguments.command == "checksums":
        write_checksums(arguments.directory)
        return
    arguments.destination.mkdir(parents=True, exist_ok=True)
    if arguments.platform == "windows":
        archive_windows(arguments.source, arguments.destination)
    else:
        archive_macos(arguments.source, arguments.destination)


if __name__ == "__main__":
    main()
