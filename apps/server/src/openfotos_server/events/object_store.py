"""Object-store configuration at the Django deployment boundary."""

from functools import lru_cache

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from openfotos_storage.backend import S3ObjectStore


@lru_cache(maxsize=1)
def configured_object_store() -> S3ObjectStore:
    values = {
        "OBJECT_STORAGE_ENDPOINT_URL or R2_ACCOUNT_ID": settings.OBJECT_STORAGE_ENDPOINT_URL,
        "R2_BUCKET_NAME": settings.R2_BUCKET_NAME,
        "R2_PARENT_ACCESS_KEY_ID": settings.R2_PARENT_ACCESS_KEY_ID,
        "R2_PARENT_SECRET_ACCESS_KEY": settings.R2_PARENT_SECRET_ACCESS_KEY,
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ImproperlyConfigured(
            f"Object storage is unavailable; configure {', '.join(missing)}."
        )
    return S3ObjectStore(
        endpoint_url=settings.OBJECT_STORAGE_ENDPOINT_URL,
        bucket_name=settings.R2_BUCKET_NAME,
        access_key_id=settings.R2_PARENT_ACCESS_KEY_ID,
        secret_access_key=settings.R2_PARENT_SECRET_ACCESS_KEY,
        region=settings.OBJECT_STORAGE_REGION,
        addressing_style=settings.OBJECT_STORAGE_ADDRESSING_STYLE,
    )
