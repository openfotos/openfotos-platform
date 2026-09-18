import base64
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.http import HttpResponse
from django.test import Client, RequestFactory, override_settings

from openfotos_server.events.middleware import RequestIdentityMiddleware
from openfotos_server.events.models import Photographer

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def configure_test_static_files(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
    settings.MIDDLEWARE = [
        middleware
        for middleware in settings.MIDDLEWARE
        if middleware != "whitenoise.middleware.WhiteNoiseMiddleware"
    ]


def test_liveness_does_not_require_dependencies() -> None:
    response = Client().get("/health/live/", headers={"host": "localhost"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert (
        response.headers["Cache-Control"]
        == "max-age=0, no-cache, no-store, must-revalidate, private"
    )


def test_readiness_checks_database_and_private_object_sentinel() -> None:
    store = SimpleNamespace(head=lambda key: SimpleNamespace(key=key))

    with patch("openfotos_server.health.configured_object_store", return_value=store):
        response = Client().get("/health/ready/", headers={"host": "localhost"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_failure_is_generic() -> None:
    store = SimpleNamespace(head=lambda _key: None)

    with patch("openfotos_server.health.configured_object_store", return_value=store):
        response = Client().get("/health/ready/", headers={"host": "localhost"})

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert b"object" not in response.content


@override_settings(
    PUBLIC_BASE_DOMAIN="example.com",
    ADMIN_HOST="admin.example.com",
    ALLOWED_HOSTS=[".example.com"],
)
def test_admin_is_available_only_on_the_dedicated_admin_host() -> None:
    Photographer.objects.create(slug="alpha", display_name="Alpha Photos")
    client = Client()

    assert client.get("/admin/", headers={"host": "admin.example.com"}).status_code == 302
    assert client.get("/admin/", headers={"host": "example.com"}).status_code == 404
    assert client.get("/admin/", headers={"host": "alpha.example.com"}).status_code == 404
    assert client.get("/", headers={"host": "admin.example.com"}).status_code == 404


@override_settings(
    REQUIRE_CLOUDFLARE_ORIGIN_SECRET=True,
    CLOUDFLARE_ORIGIN_SECRET="cloudflare-origin-secret-at-least-32-characters",
)
def test_deployed_requests_require_the_cloudflare_origin_secret() -> None:
    Photographer.objects.create(slug="alpha", display_name="Alpha Photos")
    client = Client()

    rejected = client.get("/login/", headers={"host": "alpha.localhost"})
    accepted = client.get(
        "/login/",
        headers={
            "host": "alpha.localhost",
            "x-openfotos-origin": "cloudflare-origin-secret-at-least-32-characters",
        },
    )
    liveness = client.get("/health/live/", headers={"host": "localhost"})

    assert rejected.status_code == 404
    assert accepted.status_code == 200
    assert liveness.status_code == 200


@override_settings(TRUST_CLOUDFLARE_CLIENT_IP=True)
def test_verified_edge_address_replaces_the_proxy_address() -> None:
    captured = {}

    def respond(request):
        captured["address"] = request.openfotos_client_address
        return HttpResponse()

    request = RequestFactory().get(
        "/",
        REMOTE_ADDR="10.0.0.5",
        HTTP_CF_CONNECTING_IP="203.0.113.9",
    )

    RequestIdentityMiddleware(respond)(request)

    assert captured["address"] == "203.0.113.9"


@override_settings(TRUST_CLOUDFLARE_CLIENT_IP=True)
def test_invalid_edge_address_falls_back_to_the_direct_peer() -> None:
    captured = {}

    def respond(request):
        captured["address"] = request.openfotos_client_address
        return HttpResponse()

    request = RequestFactory().get(
        "/",
        REMOTE_ADDR="10.0.0.5",
        HTTP_CF_CONNECTING_IP="not-an-address",
    )

    RequestIdentityMiddleware(respond)(request)

    assert captured["address"] == "10.0.0.5"


def test_application_responses_set_restrictive_browser_headers() -> None:
    Photographer.objects.create(slug="alpha", display_name="Alpha Photos")

    response = Client().get("/login/", headers={"host": "alpha.localhost"})

    assert response.status_code == 200
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "object-src 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Permissions-Policy"] == "camera=(), geolocation=(), microphone=()"
    assert response.headers["Cross-Origin-Resource-Policy"] == "same-site"


@override_settings(STUDIO_PRIVACY_EMAIL="privacy@studio.example")
def test_tenant_legal_pages_publish_privacy_terms_and_source_disclosures() -> None:
    Photographer.objects.create(
        slug="alpha",
        display_name="Alpha Photos",
        contact_phone="+91 9000000000",
    )
    client = Client()

    privacy = client.get("/legal/privacy/", headers={"host": "alpha.localhost"})
    terms = client.get("/legal/terms/", headers={"host": "alpha.localhost"})

    assert privacy.status_code == 200
    assert b"privacy@studio.example" in privacy.content
    assert b"parent or guardian authority" in privacy.content
    assert b"within 24 hours" in privacy.content
    assert b"Singapore" in privacy.content
    assert terms.status_code == 200
    assert b"Face search is approximate" in terms.content
    assert b"GNU Affero General Public License v3.0 only" in terms.content
    assert b"https://github.com/openfotos/openfotos-platform" in terms.content


def test_legal_pages_do_not_exist_on_unknown_tenant_host() -> None:
    response = Client().get("/legal/privacy/", headers={"host": "unknown.localhost"})

    assert response.status_code == 404


def _deployed_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "OPENFOTOS_ENVIRONMENT": "rehearsal",
            "DJANGO_DEBUG": "false",
            "DJANGO_SECRET_KEY": "django-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ-secret-value",
            "SHARE_PIN_PEPPER": "share-pin-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            "PRIVACY_HASH_KEY": "privacy-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            "CLOUDFLARE_ORIGIN_SECRET": "origin-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            "DJANGO_ALLOWED_HOSTS": ".example.com",
            "DJANGO_CSRF_TRUSTED_ORIGINS": "https://*.example.com",
            "PUBLIC_BASE_DOMAIN": "example.com",
            "ADMIN_HOST": "admin.example.com",
            "STUDIO_PRIVACY_EMAIL": "privacy@example.com",
            "DATABASE_CA_CERT_BASE64": base64.b64encode(
                b"-----BEGIN CERTIFICATE-----\nsynthetic\n-----END CERTIFICATE-----\n"
            ).decode("ascii"),
            "DATABASE_URL": (
                "postgresql://openfotos_runtime.projectref:secret@example.invalid:5432/openfotos"
                "?sslmode=verify-full&sslrootcert=/tmp/openfotos-supabase-ca.pem"
                "&options=-csearch_path%3Dopenfotos%2Cextensions"
            ),
            "OBJECT_STORAGE_ENDPOINT_URL": "https://account.r2.cloudflarestorage.com",
            "R2_BUCKET_NAME": "openfotos-rehearsal",
            "R2_PARENT_ACCESS_KEY_ID": "bucket-key",
            "R2_PARENT_SECRET_ACCESS_KEY": "bucket-secret",
            "FACE_DETECTOR_MODEL_PATH": "/private/models/yunet.onnx",
            "FACE_RECOGNIZER_MODEL_PATH": "/private/models/sface.onnx",
        }
    )
    return environment


def _import_settings(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", "import openfotos_server.settings"],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )


def test_complete_deployed_configuration_imports_successfully() -> None:
    result = _import_settings(_deployed_environment())

    assert result.returncode == 0, result.stderr


def test_deployed_configuration_fails_closed_without_origin_secret() -> None:
    environment = _deployed_environment()
    environment["CLOUDFLARE_ORIGIN_SECRET"] = ""

    result = _import_settings(environment)

    assert result.returncode != 0
    assert "CLOUDFLARE_ORIGIN_SECRET" in result.stderr


def test_deployed_configuration_rejects_low_entropy_secrets() -> None:
    environment = _deployed_environment()
    environment["DJANGO_SECRET_KEY"] = "x" * 60

    result = _import_settings(environment)

    assert result.returncode != 0
    assert "varied characters" in result.stderr


def test_runtime_configuration_rejects_the_privileged_database_role() -> None:
    environment = _deployed_environment()
    environment["DATABASE_URL"] = environment["DATABASE_URL"].replace(
        "openfotos_runtime.projectref", "postgres.projectref"
    )

    result = _import_settings(environment)

    assert result.returncode != 0
    assert "OPENFOTOS_RUNTIME_DB_ROLE" in result.stderr


def test_migration_configuration_accepts_only_the_postgres_role() -> None:
    environment = _deployed_environment()
    environment["OPENFOTOS_DATABASE_MODE"] = "migration"
    environment["DATABASE_URL"] = environment["DATABASE_URL"].replace(
        "openfotos_runtime.projectref", "postgres.projectref"
    )
    for name in (
        "CLOUDFLARE_ORIGIN_SECRET",
        "PRIVACY_HASH_KEY",
        "R2_BUCKET_NAME",
        "R2_PARENT_ACCESS_KEY_ID",
        "R2_PARENT_SECRET_ACCESS_KEY",
        "FACE_DETECTOR_MODEL_PATH",
        "FACE_RECOGNIZER_MODEL_PATH",
    ):
        environment[name] = ""

    accepted = _import_settings(environment)
    environment["DATABASE_URL"] = environment["DATABASE_URL"].replace(
        "postgres.projectref", "unprivileged.projectref"
    )
    rejected = _import_settings(environment)

    assert accepted.returncode == 0, accepted.stderr
    assert rejected.returncode != 0
    assert "postgres migration role" in rejected.stderr


def test_deployed_configuration_rejects_invalid_origin_and_hsts_values() -> None:
    invalid_origin = _deployed_environment()
    invalid_origin["DJANGO_CSRF_TRUSTED_ORIGINS"] = "https://"
    origin_result = _import_settings(invalid_origin)

    negative_hsts = _deployed_environment()
    negative_hsts["DJANGO_SECURE_HSTS_SECONDS"] = "-1"
    hsts_result = _import_settings(negative_hsts)

    assert origin_result.returncode != 0
    assert "valid HTTPS origins" in origin_result.stderr
    assert hsts_result.returncode != 0
    assert "non-negative integer" in hsts_result.stderr


def test_production_requires_the_initial_five_minute_hsts_floor() -> None:
    environment = _deployed_environment()
    environment["OPENFOTOS_ENVIRONMENT"] = "production"
    environment["DJANGO_SECURE_HSTS_SECONDS"] = "0"

    result = _import_settings(environment)

    assert result.returncode != 0
    assert "at least 300" in result.stderr
