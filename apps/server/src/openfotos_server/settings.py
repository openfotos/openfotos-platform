"""OpenFotos settings with fail-closed deployment validation."""

import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parents[4]

OPENFOTOS_ENVIRONMENT = os.environ.get("OPENFOTOS_ENVIRONMENT", "local").strip().lower()
if OPENFOTOS_ENVIRONMENT not in {"local", "rehearsal", "production"}:
    raise ImproperlyConfigured("OPENFOTOS_ENVIRONMENT must be local, rehearsal, or production.")
IS_DEPLOYED = OPENFOTOS_ENVIRONMENT in {"rehearsal", "production"}

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "unsafe-development-key")
SHARE_PIN_PEPPER = os.environ.get("SHARE_PIN_PEPPER", "").strip()
if not SHARE_PIN_PEPPER:
    if SECRET_KEY != "unsafe-development-key":
        raise ImproperlyConfigured("SHARE_PIN_PEPPER is required outside local development.")
    SHARE_PIN_PEPPER = "unsafe-development-share-pin-pepper"
if len(SHARE_PIN_PEPPER) < 32:
    raise ImproperlyConfigured("SHARE_PIN_PEPPER must contain at least 32 characters.")
DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get(
        "DJANGO_ALLOWED_HOSTS",
        "localhost,.localhost,127.0.0.1,testserver",
    ).split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "openfotos_server.events",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "openfotos_server.events.middleware.OriginProtectionMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "openfotos_server.events.middleware.RequestIdentityMiddleware",
    "openfotos_server.events.middleware.PhotographerHostMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "openfotos_server.events.middleware.ResponseSecurityHeadersMiddleware",
]

ROOT_URLCONF = "openfotos_server.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]
WSGI_APPLICATION = "openfotos_server.wsgi.application"
ASGI_APPLICATION = "openfotos_server.asgi.application"

# SQLite keeps local checks immediately runnable. Production uses PostgreSQL/pgvector through
# DATABASE_URL for the face-search query boundary.
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'openfotos.sqlite3'}",
        conn_max_age=60,
        conn_health_checks=True,
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

PUBLIC_BASE_DOMAIN = os.environ.get("PUBLIC_BASE_DOMAIN", "localhost").strip().lower().rstrip(".")
base_domain_labels = PUBLIC_BASE_DOMAIN.split(".")
if len(PUBLIC_BASE_DOMAIN) > 253 or any(
    not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in base_domain_labels
):
    raise ImproperlyConfigured("PUBLIC_BASE_DOMAIN must be a hostname without a scheme or port.")

ADMIN_HOST = os.environ.get("ADMIN_HOST", PUBLIC_BASE_DOMAIN).strip().lower().rstrip(".")
admin_host_labels = ADMIN_HOST.split(".")
if len(ADMIN_HOST) > 253 or any(
    not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in admin_host_labels
):
    raise ImproperlyConfigured("ADMIN_HOST must be a hostname without a scheme or port.")

CLOUDFLARE_ORIGIN_SECRET = os.environ.get("CLOUDFLARE_ORIGIN_SECRET", "").strip()
PRIVACY_HASH_KEY = os.environ.get("PRIVACY_HASH_KEY", "").strip()
if not PRIVACY_HASH_KEY:
    PRIVACY_HASH_KEY = "unsafe-development-privacy-hash-key" if not IS_DEPLOYED else ""
REQUIRE_CLOUDFLARE_ORIGIN_SECRET = IS_DEPLOYED
TRUST_CLOUDFLARE_CLIENT_IP = IS_DEPLOYED

CSRF_TRUSTED_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
STUDIO_PRIVACY_EMAIL = os.environ.get("STUDIO_PRIVACY_EMAIL", "").strip()
SOURCE_CODE_URL = os.environ.get(
    "SOURCE_CODE_URL", "https://github.com/openfotos/openfotos-platform"
).strip()
OPENFOTOS_RUNTIME_DB_ROLE = os.environ.get("OPENFOTOS_RUNTIME_DB_ROLE", "openfotos_runtime").strip()
OPENFOTOS_DATABASE_MODE = os.environ.get("OPENFOTOS_DATABASE_MODE", "runtime").strip().lower()


def positive_integer_setting(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be a positive integer.") from exc
    if value <= 0:
        raise ImproperlyConfigured(f"{name} must be a positive integer.")
    return value


def nonnegative_integer_setting(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be a non-negative integer.") from exc
    if value < 0:
        raise ImproperlyConfigured(f"{name} must be a non-negative integer.")
    return value


AUTH_FAILURE_LIMIT = positive_integer_setting("AUTH_FAILURE_LIMIT", 5)
AUTH_FAILURE_WINDOW_SECONDS = positive_integer_setting("AUTH_FAILURE_WINDOW_SECONDS", 900)
PORTAL_PIN_FAILURE_LIMIT = positive_integer_setting("PORTAL_PIN_FAILURE_LIMIT", 10)
PORTAL_SESSION_TTL_SECONDS = positive_integer_setting("PORTAL_SESSION_TTL_SECONDS", 24 * 3_600)
EVENT_RETENTION_DAYS = 365
EVENT_PURGE_GRACE_DAYS = 30
PORTFOLIO_IMAGE_MAX_BYTES = 20 * 1024 * 1024
PORTFOLIO_IMAGE_MAX_PIXELS = 40_000_000
FACE_SEARCH_RESULT_TTL_SECONDS = 3_600
FACE_SEARCH_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
FACE_SEARCH_MAX_PIXELS = 40_000_000
FACE_SEARCH_CLIENT_LIMIT = 10
FACE_SEARCH_CAPABILITY_LIMIT = 100
FACE_SEARCH_LIMIT_WINDOW_SECONDS = 900
FACE_DETECTOR_MODEL_PATH = os.environ.get("FACE_DETECTOR_MODEL_PATH", "").strip()
FACE_RECOGNIZER_MODEL_PATH = os.environ.get("FACE_RECOGNIZER_MODEL_PATH", "").strip()
DESKTOP_ACCESS_TTL_SECONDS = positive_integer_setting("DESKTOP_ACCESS_TTL_SECONDS", 900)
DESKTOP_REFRESH_TTL_SECONDS = positive_integer_setting("DESKTOP_REFRESH_TTL_SECONDS", 14 * 86_400)
DESKTOP_REFRESH_RETRY_GRACE_SECONDS = positive_integer_setting(
    "DESKTOP_REFRESH_RETRY_GRACE_SECONDS", 60
)
UPLOAD_LEASE_TTL_SECONDS = positive_integer_setting("UPLOAD_LEASE_TTL_SECONDS", 300)
UPLOAD_LEASE_PAGE_SIZE = positive_integer_setting("UPLOAD_LEASE_PAGE_SIZE", 8)
DESKTOP_API_MAX_BODY_BYTES = positive_integer_setting("DESKTOP_API_MAX_BODY_BYTES", 8 * 1024 * 1024)
IDEMPOTENCY_TTL_SECONDS = positive_integer_setting("IDEMPOTENCY_TTL_SECONDS", 30 * 86_400)
SIGNED_URL_TTL_SECONDS = positive_integer_setting("SIGNED_URL_TTL_SECONDS", 300)
if SIGNED_URL_TTL_SECONDS > 300:
    raise ImproperlyConfigured("SIGNED_URL_TTL_SECONDS cannot exceed the five-minute pilot limit.")

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "").strip()
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "").strip()
R2_PARENT_ACCESS_KEY_ID = os.environ.get("R2_PARENT_ACCESS_KEY_ID", "").strip()
R2_PARENT_SECRET_ACCESS_KEY = os.environ.get("R2_PARENT_SECRET_ACCESS_KEY", "").strip()
OBJECT_STORAGE_ENDPOINT_URL = os.environ.get(
    "OBJECT_STORAGE_ENDPOINT_URL",
    f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else "",
).strip()
OBJECT_STORAGE_REGION = os.environ.get("OBJECT_STORAGE_REGION", "auto").strip()
OBJECT_STORAGE_ADDRESSING_STYLE = os.environ.get("OBJECT_STORAGE_ADDRESSING_STYLE", "path").strip()
READINESS_OBJECT_KEY = os.environ.get(
    "READINESS_OBJECT_KEY", "operations/readiness-sentinel.txt"
).strip()

SESSION_COOKIE_NAME = "openfotos_account_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if IS_DEPLOYED else None
SECURE_SSL_REDIRECT = IS_DEPLOYED
SECURE_REDIRECT_EXEMPT = [r"^health/live/$"]
SECURE_HSTS_SECONDS = nonnegative_integer_setting(
    "DJANGO_SECURE_HSTS_SECONDS",
    300 if OPENFOTOS_ENVIRONMENT == "production" else 0,
)
SECURE_HSTS_INCLUDE_SUBDOMAINS = IS_DEPLOYED
SECURE_HSTS_PRELOAD = False


def _is_https_origin(origin: str) -> bool:
    try:
        parsed = urlparse(origin)
        _port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return False
    hostname = parsed.hostname.removeprefix("*.")
    return len(hostname) <= 253 and all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in hostname.split(".")
    )


def _secret_is_strong(value: str, *, minimum_length: int) -> bool:
    return len(value) >= minimum_length and len(set(value)) >= 16


def _validate_deployed_settings() -> None:
    if not IS_DEPLOYED:
        return
    errors: list[str] = []
    if DEBUG:
        errors.append("DJANGO_DEBUG must be false")
    if not _secret_is_strong(SECRET_KEY, minimum_length=50):
        errors.append("DJANGO_SECRET_KEY must contain at least 50 varied characters")
    if not _secret_is_strong(SHARE_PIN_PEPPER, minimum_length=32):
        errors.append("SHARE_PIN_PEPPER must contain at least 32 varied characters")
    if OPENFOTOS_DATABASE_MODE not in {"runtime", "migration"}:
        errors.append("OPENFOTOS_DATABASE_MODE must be runtime or migration")

    database_url = os.environ.get("DATABASE_URL", "")
    parsed_database_url = urlparse(database_url)
    database_options = parse_qs(parsed_database_url.query)
    if parsed_database_url.scheme not in {"postgres", "postgresql"}:
        errors.append("DATABASE_URL must use PostgreSQL")
    if database_options.get("sslmode") != ["verify-full"]:
        errors.append("DATABASE_URL must set sslmode=verify-full")
    if database_options.get("sslrootcert") != ["/tmp/openfotos-supabase-ca.pem"]:
        errors.append("DATABASE_URL must set sslrootcert=/tmp/openfotos-supabase-ca.pem")
    if database_options.get("options") != ["-csearch_path=openfotos,extensions"]:
        errors.append("DATABASE_URL must set options=-csearch_path=openfotos,extensions")
    if not os.environ.get("DATABASE_CA_CERT_BASE64", "").strip():
        errors.append("DATABASE_CA_CERT_BASE64 is required")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", OPENFOTOS_RUNTIME_DB_ROLE):
        errors.append("OPENFOTOS_RUNTIME_DB_ROLE must be a lowercase PostgreSQL identifier")
    database_role = (parsed_database_url.username or "").split(".", 1)[0]
    if OPENFOTOS_DATABASE_MODE == "runtime" and database_role != OPENFOTOS_RUNTIME_DB_ROLE:
        errors.append("DATABASE_URL must authenticate as OPENFOTOS_RUNTIME_DB_ROLE")
    if OPENFOTOS_DATABASE_MODE == "migration" and database_role != "postgres":
        errors.append("MIGRATION_DATABASE_URL must authenticate as the postgres migration role")

    if OPENFOTOS_DATABASE_MODE == "runtime":
        if not _secret_is_strong(CLOUDFLARE_ORIGIN_SECRET, minimum_length=32):
            errors.append("CLOUDFLARE_ORIGIN_SECRET must contain at least 32 varied characters")
        if not _secret_is_strong(PRIVACY_HASH_KEY, minimum_length=32):
            errors.append("PRIVACY_HASH_KEY must contain at least 32 varied characters")
        if ADMIN_HOST == PUBLIC_BASE_DOMAIN:
            errors.append("ADMIN_HOST must be separate from PUBLIC_BASE_DOMAIN")
        if not CSRF_TRUSTED_ORIGINS or any(
            not _is_https_origin(origin) for origin in CSRF_TRUSTED_ORIGINS
        ):
            errors.append("DJANGO_CSRF_TRUSTED_ORIGINS must contain only valid HTTPS origins")
        if not READINESS_OBJECT_KEY:
            errors.append("READINESS_OBJECT_KEY is required")
        if "@" not in STUDIO_PRIVACY_EMAIL:
            errors.append("STUDIO_PRIVACY_EMAIL must contain the studio rights-contact email")
        if not SOURCE_CODE_URL.startswith("https://"):
            errors.append("SOURCE_CODE_URL must use HTTPS")
        if OPENFOTOS_ENVIRONMENT == "production" and SECURE_HSTS_SECONDS < 300:
            errors.append("DJANGO_SECURE_HSTS_SECONDS must be at least 300 in production")

        required_storage = {
            "OBJECT_STORAGE_ENDPOINT_URL or R2_ACCOUNT_ID": OBJECT_STORAGE_ENDPOINT_URL,
            "R2_BUCKET_NAME": R2_BUCKET_NAME,
            "R2_PARENT_ACCESS_KEY_ID": R2_PARENT_ACCESS_KEY_ID,
            "R2_PARENT_SECRET_ACCESS_KEY": R2_PARENT_SECRET_ACCESS_KEY,
        }
        errors.extend(
            f"{name} is required" for name, value in required_storage.items() if not value
        )
        if OBJECT_STORAGE_ENDPOINT_URL and not OBJECT_STORAGE_ENDPOINT_URL.startswith("https://"):
            errors.append("OBJECT_STORAGE_ENDPOINT_URL must use HTTPS")
        if not FACE_DETECTOR_MODEL_PATH or not FACE_RECOGNIZER_MODEL_PATH:
            errors.append("both face-model paths are required")
    if errors:
        raise ImproperlyConfigured("Unsafe deployed configuration: " + "; ".join(errors) + ".")


_validate_deployed_settings()

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "privacy_safe_json": {
            "()": "openfotos_server.logging.PrivacySafeJsonFormatter",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "privacy_safe_json",
        }
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.server": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
