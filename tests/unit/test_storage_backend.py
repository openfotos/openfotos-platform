from datetime import UTC, datetime

from openfotos_storage.backend import S3ObjectStore


class PresignClient:
    def __init__(self) -> None:
        self.calls = []

    def generate_presigned_url(self, operation, *, Params, ExpiresIn, HttpMethod):
        self.calls.append((operation, Params, ExpiresIn, HttpMethod))
        return "https://storage.invalid/exact-object"


def test_presigned_get_is_exact_short_lived_and_private(monkeypatch) -> None:
    client = PresignClient()
    monkeypatch.setattr("openfotos_storage.backend.boto3.client", lambda *_args, **_kwargs: client)
    store = S3ObjectStore(
        endpoint_url="https://storage.invalid",
        bucket_name="private-gallery",
        access_key_id="synthetic-key",
        secret_access_key="synthetic-secret",
    )
    before = datetime.now(UTC)

    signed = store.presign_get(
        key="events/event-id/previews/asset-id.jpg",
        expires_in_seconds=300,
    )

    assert signed.url == "https://storage.invalid/exact-object"
    assert 299 <= (signed.expires_at - before).total_seconds() <= 301
    assert client.calls == [
        (
            "get_object",
            {
                "Bucket": "private-gallery",
                "Key": "events/event-id/previews/asset-id.jpg",
                "ResponseContentDisposition": "inline",
                "ResponseCacheControl": "private, no-store",
            },
            300,
            "GET",
        )
    ]


def test_presigned_put_binds_the_validated_original_content_type(monkeypatch) -> None:
    client = PresignClient()
    monkeypatch.setattr("openfotos_storage.backend.boto3.client", lambda *_args, **_kwargs: client)
    store = S3ObjectStore(
        endpoint_url="https://storage.invalid",
        bucket_name="private-gallery",
        access_key_id="synthetic-key",
        secret_access_key="synthetic-secret",
    )

    signed = store.presign_put(
        key="events/event-id/originals/asset-id.webp",
        content_length=123,
        content_md5="ndTkYSaMgDT1yFZOFVxnpg==",
        sha256="a" * 64,
        content_type="image/webp",
        expires_in_seconds=300,
    )

    assert signed.headers["Content-Type"] == "image/webp"
    assert client.calls[0][1]["ContentType"] == "image/webp"
