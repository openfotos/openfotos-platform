"""Development-safe settings for the initial OpenFotos server skeleton."""

import os
import re
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parents[4]

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "unsafe-development-key")
EVENT_PIN_PEPPER = os.environ.get("EVENT_PIN_PEPPER", "").strip()
if not EVENT_PIN_PEPPER:
    if SECRET_KEY != "unsafe-development-key":
        raise ImproperlyConfigured("EVENT_PIN_PEPPER is required outside local development.")
    EVENT_PIN_PEPPER = "unsafe-development-event-pin-pepper"
if len(EVENT_PIN_PEPPER) < 32:
    raise ImproperlyConfigured("EVENT_PIN_PEPPER must contain at least 32 characters.")
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
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "openfotos_server.events.middleware.RequestIdentityMiddleware",
    "openfotos_server.events.middleware.PhotographerHostMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
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


def positive_integer_setting(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be a positive integer.") from exc
    if value <= 0:
        raise ImproperlyConfigured(f"{name} must be a positive integer.")
    return value


AUTH_FAILURE_LIMIT = positive_integer_setting("AUTH_FAILURE_LIMIT", 5)
AUTH_FAILURE_WINDOW_SECONDS = positive_integer_setting("AUTH_FAILURE_WINDOW_SECONDS", 900)
EVENT_SESSION_TTL_SECONDS = positive_integer_setting("EVENT_SESSION_TTL_SECONDS", 86_400)
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

SESSION_COOKIE_NAME = "openfotos_account_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin"
X_FRAME_OPTIONS = "DENY"
