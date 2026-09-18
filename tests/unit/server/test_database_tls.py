import base64
from pathlib import Path

import pytest

from openfotos_server.database_tls import (
    DatabaseTlsConfigurationError,
    prepare_database_environment,
)

_CERTIFICATE = b"-----BEGIN CERTIFICATE-----\nsynthetic-ca\n-----END CERTIFICATE-----\n"


def _environment(certificate_path: Path) -> dict[str, str]:
    return {
        "DATABASE_CA_CERT_BASE64": base64.b64encode(_CERTIFICATE).decode("ascii"),
        "DATABASE_URL": (
            "postgresql://runtime:secret@db.example:5432/openfotos"
            f"?sslmode=verify-full&sslrootcert={certificate_path}"
            "&options=-csearch_path%3Dopenfotos%2Cextensions"
        ),
        "MIGRATION_DATABASE_URL": (
            "postgresql://migrator:secret@db.example:5432/openfotos"
            f"?sslmode=verify-full&sslrootcert={certificate_path}"
            "&options=-csearch_path%3Dopenfotos%2Cextensions"
        ),
    }


def test_database_ca_is_written_privately_and_runtime_url_is_selected(tmp_path: Path) -> None:
    certificate_path = tmp_path / "database-ca.pem"

    prepared = prepare_database_environment(
        _environment(certificate_path),
        database_url_variable="DATABASE_URL",
        certificate_path=certificate_path,
    )

    assert certificate_path.read_bytes() == _CERTIFICATE
    assert certificate_path.stat().st_mode & 0o777 == 0o600
    assert prepared["DATABASE_URL"].startswith("postgresql://runtime:")
    assert prepared["OPENFOTOS_DATABASE_MODE"] == "runtime"


def test_migration_url_replaces_runtime_url_for_child_process(tmp_path: Path) -> None:
    certificate_path = tmp_path / "database-ca.pem"

    prepared = prepare_database_environment(
        _environment(certificate_path),
        database_url_variable="MIGRATION_DATABASE_URL",
        certificate_path=certificate_path,
    )

    assert prepared["DATABASE_URL"].startswith("postgresql://migrator:")
    assert prepared["OPENFOTOS_DATABASE_MODE"] == "migration"


def test_arbitrary_database_secret_name_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(
        DatabaseTlsConfigurationError,
        match="DATABASE_URL or MIGRATION_DATABASE_URL",
    ):
        prepare_database_environment(
            _environment(tmp_path / "database-ca.pem"),
            database_url_variable="UNREVIEWED_DATABASE_URL",
            certificate_path=tmp_path / "database-ca.pem",
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("DATABASE_CA_CERT_BASE64", "not base64", "not valid base64"),
        ("DATABASE_CA_CERT_BASE64", base64.b64encode(b"not pem").decode(), "PEM encoded"),
        ("DATABASE_URL", "sqlite:///local.db", "must use PostgreSQL"),
        (
            "DATABASE_URL",
            "postgresql://app:secret@db.example/openfotos?sslmode=require",
            "sslmode=verify-full",
        ),
        (
            "DATABASE_URL",
            "postgresql://app:secret@db.example/openfotos"
            f"?sslmode=verify-full&sslrootcert={Path('/tmp/wrong-ca.pem')}",
            "sslrootcert",
        ),
    ],
)
def test_invalid_database_tls_configuration_fails_before_exec(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    certificate_path = tmp_path / "database-ca.pem"
    environment = _environment(certificate_path)
    environment[field] = value

    with pytest.raises(DatabaseTlsConfigurationError, match=message):
        prepare_database_environment(
            environment,
            database_url_variable="DATABASE_URL",
            certificate_path=certificate_path,
        )

    assert not certificate_path.exists()
