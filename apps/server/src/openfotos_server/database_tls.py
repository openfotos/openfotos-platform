"""Materialize the private database CA and execute a TLS-verified process."""

from __future__ import annotations

import argparse
import base64
import binascii
import os
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DATABASE_CA_PATH = Path("/tmp/openfotos-supabase-ca.pem")
_MAX_CA_BYTES = 64 * 1024
_DATABASE_MODES = {
    "DATABASE_URL": "runtime",
    "MIGRATION_DATABASE_URL": "migration",
}


class DatabaseTlsConfigurationError(RuntimeError):
    pass


def prepare_database_environment(
    environment: Mapping[str, str],
    *,
    database_url_variable: str,
    certificate_path: Path = DATABASE_CA_PATH,
) -> dict[str, str]:
    try:
        database_mode = _DATABASE_MODES[database_url_variable]
    except KeyError as exc:
        raise DatabaseTlsConfigurationError(
            "The database URL variable must be DATABASE_URL or MIGRATION_DATABASE_URL."
        ) from exc

    encoded_certificate = environment.get("DATABASE_CA_CERT_BASE64", "").strip()
    if not encoded_certificate:
        raise DatabaseTlsConfigurationError("DATABASE_CA_CERT_BASE64 is required.")
    try:
        certificate = base64.b64decode(encoded_certificate, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise DatabaseTlsConfigurationError("DATABASE_CA_CERT_BASE64 is not valid base64.") from exc
    if not certificate or len(certificate) > _MAX_CA_BYTES:
        raise DatabaseTlsConfigurationError("The database CA certificate has an invalid size.")
    try:
        certificate_text = certificate.decode("ascii")
    except UnicodeDecodeError as exc:
        raise DatabaseTlsConfigurationError(
            "The database CA certificate must be PEM text."
        ) from exc
    if not (
        certificate_text.startswith("-----BEGIN CERTIFICATE-----\n")
        and certificate_text.rstrip().endswith("-----END CERTIFICATE-----")
    ):
        raise DatabaseTlsConfigurationError("The database CA certificate must be PEM encoded.")

    database_url = environment.get(database_url_variable, "").strip()
    parsed_url = urlparse(database_url)
    options = parse_qs(parsed_url.query)
    if parsed_url.scheme not in {"postgres", "postgresql"}:
        raise DatabaseTlsConfigurationError(f"{database_url_variable} must use PostgreSQL.")
    if options.get("sslmode") != ["verify-full"]:
        raise DatabaseTlsConfigurationError(
            f"{database_url_variable} must set sslmode=verify-full."
        )
    if options.get("sslrootcert") != [str(certificate_path)]:
        raise DatabaseTlsConfigurationError(
            f"{database_url_variable} must set sslrootcert={certificate_path}."
        )
    if options.get("options") != ["-csearch_path=openfotos,extensions"]:
        raise DatabaseTlsConfigurationError(
            f"{database_url_variable} must set options=-csearch_path=openfotos,extensions."
        )

    _write_private_certificate(certificate_path, certificate)
    prepared = dict(environment)
    prepared["DATABASE_URL"] = database_url
    prepared["OPENFOTOS_DATABASE_MODE"] = database_mode
    return prepared


def _write_private_certificate(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
    except Exception:
        with suppress(OSError):
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url-variable", default="DATABASE_URL")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(arguments: Sequence[str] | None = None) -> None:
    parsed = _parser().parse_args(arguments)
    command = parsed.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("A command is required after --.")
    try:
        environment = prepare_database_environment(
            os.environ,
            database_url_variable=parsed.database_url_variable,
        )
    except DatabaseTlsConfigurationError as exc:
        raise SystemExit(str(exc)) from exc
    os.execvpe(command[0], command, environment)


if __name__ == "__main__":
    main()
