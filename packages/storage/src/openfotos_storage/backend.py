"""Private S3-compatible object operations owned by the server."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import BinaryIO

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError


class ObjectStoreError(RuntimeError):
    pass


class ObjectAlreadyExists(ObjectStoreError):
    pass


@dataclass(frozen=True)
class ObjectHead:
    key: str
    content_length: int
    content_type: str
    etag: str
    metadata: dict[str, str]


@dataclass(frozen=True)
class PresignedPut:
    url: str
    headers: dict[str, str]
    expires_at: datetime


class S3ObjectStore:
    """Issue narrow upload leases and inspect objects without exposing parent credentials."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket_name: str,
        access_key_id: str,
        secret_access_key: str,
        region: str = "auto",
        addressing_style: str = "path",
    ) -> None:
        if not all((endpoint_url, bucket_name, access_key_id, secret_access_key)):
            raise ValueError("Complete object-store configuration is required.")
        self.bucket_name = bucket_name
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": addressing_style},
            ),
        )

    def presign_put(
        self,
        *,
        key: str,
        content_length: int,
        content_md5: str,
        sha256: str,
        expires_in_seconds: int,
    ) -> PresignedPut:
        if content_length <= 0 or expires_in_seconds <= 0:
            raise ValueError("Upload size and lease lifetime must be positive.")
        metadata = {"openfotos-sha256": sha256}
        parameters = {
            "Bucket": self.bucket_name,
            "Key": key,
            "ContentLength": content_length,
            "ContentMD5": content_md5,
            "ContentType": "image/jpeg",
            "IfNoneMatch": "*",
            "Metadata": metadata,
        }
        try:
            url = self._client.generate_presigned_url(
                "put_object",
                Params=parameters,
                ExpiresIn=expires_in_seconds,
                HttpMethod="PUT",
            )
        except BotoCoreError as exc:
            raise ObjectStoreError("An upload lease could not be created.") from exc
        return PresignedPut(
            url=url,
            headers={
                "Content-Length": str(content_length),
                "Content-MD5": content_md5,
                "Content-Type": "image/jpeg",
                "If-None-Match": "*",
                "x-amz-meta-openfotos-sha256": sha256,
            },
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
        )

    def head(self, key: str) -> ObjectHead | None:
        try:
            response = self._client.head_object(Bucket=self.bucket_name, Key=key)
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = exc.response.get("Error", {}).get("Code")
            if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise ObjectStoreError("Object metadata could not be read.") from exc
        except BotoCoreError as exc:
            raise ObjectStoreError("Object metadata could not be read.") from exc
        return ObjectHead(
            key=key,
            content_length=response["ContentLength"],
            content_type=response.get("ContentType", ""),
            etag=response.get("ETag", "").strip('"'),
            metadata=response.get("Metadata", {}),
        )

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket_name, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStoreError("The unverified object could not be removed.") from exc

    def put_immutable(
        self,
        *,
        key: str,
        body: bytes | BinaryIO,
        content_length: int,
        content_md5: str,
        sha256: str,
        content_type: str,
    ) -> None:
        try:
            self._client.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=body,
                ContentLength=content_length,
                ContentMD5=content_md5,
                ContentType=content_type,
                IfNoneMatch="*",
                Metadata={"openfotos-sha256": sha256},
            )
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = exc.response.get("Error", {}).get("Code")
            if status == 412 or code in {"PreconditionFailed", "ConditionalRequestConflict"}:
                raise ObjectAlreadyExists("The immutable object already exists.") from exc
            raise ObjectStoreError("The immutable object could not be written.") from exc
        except BotoCoreError as exc:
            raise ObjectStoreError("The immutable object could not be written.") from exc
