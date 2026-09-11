import base64
import hashlib
import os
from uuid import uuid4

import httpx
import pytest

from openfotos_storage import AssetVariant, asset_key, ingestion_manifest_key
from openfotos_storage.backend import ObjectAlreadyExists, S3ObjectStore

pytestmark = pytest.mark.skipif(
    os.environ.get("OPENFOTOS_LIVE_S3") != "1",
    reason="set OPENFOTOS_LIVE_S3=1 to exercise a private S3-compatible test bucket",
)


def configured_store() -> S3ObjectStore:
    account_id = os.environ.get("R2_ACCOUNT_ID", "").strip()
    endpoint = os.environ.get("OPENFOTOS_TEST_S3_ENDPOINT", "").strip()
    endpoint = endpoint or (f"https://{account_id}.r2.cloudflarestorage.com" if account_id else "")
    bucket = os.environ.get("OPENFOTOS_TEST_S3_BUCKET", "").strip()
    bucket = bucket or os.environ.get("R2_BUCKET_NAME", "").strip()
    access_key = os.environ.get("OPENFOTOS_TEST_S3_ACCESS_KEY", "").strip()
    access_key = access_key or os.environ.get("R2_PARENT_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("OPENFOTOS_TEST_S3_SECRET_KEY", "").strip()
    secret_key = secret_key or os.environ.get("R2_PARENT_SECRET_ACCESS_KEY", "").strip()
    if not all((endpoint, bucket, access_key, secret_key)):
        pytest.fail("The live S3-compatible test configuration is incomplete.")
    return S3ObjectStore(
        endpoint_url=endpoint,
        bucket_name=bucket,
        access_key_id=access_key,
        secret_access_key=secret_key,
        region=os.environ.get("OPENFOTOS_TEST_S3_REGION", "auto"),
        addressing_style=os.environ.get("OPENFOTOS_TEST_S3_ADDRESSING_STYLE", "path"),
    )


def test_presigned_original_and_immutable_manifest_against_live_object_store() -> None:
    store = configured_store()
    event_id = uuid4()
    asset_id = uuid4()
    original_key = asset_key(event_id, asset_id, AssetVariant.ORIGINAL)
    manifest_key = ingestion_manifest_key(event_id, 1)
    original = b"\xff\xd8\xff\xe0OpenFotos synthetic live storage check\xff\xd9"
    original_sha256 = hashlib.sha256(original).hexdigest()
    original_md5 = base64.b64encode(hashlib.md5(original, usedforsecurity=False).digest()).decode()
    manifest = b'{"schema_version":1,"synthetic":true}'
    manifest_sha256 = hashlib.sha256(manifest).hexdigest()
    manifest_md5 = base64.b64encode(hashlib.md5(manifest, usedforsecurity=False).digest()).decode()

    try:
        lease = store.presign_put(
            key=original_key,
            content_length=len(original),
            content_md5=original_md5,
            sha256=original_sha256,
            expires_in_seconds=300,
        )
        try:
            response = httpx.put(lease.url, headers=lease.headers, content=original, timeout=30)
        except httpx.HTTPError:
            raise AssertionError("The presigned live object upload could not be reached.") from None
        assert response.status_code in {200, 201, 204}

        head = store.head(original_key)
        assert head is not None
        assert head.content_length == len(original)
        assert head.content_type == "image/jpeg"
        assert head.etag == hashlib.md5(original, usedforsecurity=False).hexdigest()
        assert head.metadata["openfotos-sha256"] == original_sha256

        repeated = httpx.put(lease.url, headers=lease.headers, content=original, timeout=30)
        assert repeated.status_code in {409, 412}
        assert store.head(original_key) == head

        store.put_immutable(
            key=manifest_key,
            body=manifest,
            content_length=len(manifest),
            content_md5=manifest_md5,
            sha256=manifest_sha256,
            content_type="application/json",
        )
        with pytest.raises(ObjectAlreadyExists):
            store.put_immutable(
                key=manifest_key,
                body=manifest,
                content_length=len(manifest),
                content_md5=manifest_md5,
                sha256=manifest_sha256,
                content_type="application/json",
            )
    finally:
        store.delete(original_key)
        store.delete(manifest_key)
