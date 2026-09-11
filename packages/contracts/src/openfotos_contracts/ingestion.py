"""Strict desktop-to-server ingestion request values."""

import base64
import json
import re
import unicodedata
from dataclasses import dataclass
from uuid import UUID

MAX_BATCH_ASSETS = 10_000
MAX_ORIGINAL_BYTES = 100 * 1024 * 1024
MAX_IMAGE_PIXELS = 120_000_000
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class ContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _strict_fields(value: object, required: set[str], *, context: str) -> dict:
    if not isinstance(value, dict):
        raise ContractError("invalid_request", f"{context} must be a JSON object.")
    fields = set(value)
    if fields != required:
        raise ContractError(
            "invalid_request",
            f"{context} fields do not match the versioned contract.",
        )
    return value


def _uuid(value: object, *, field: str) -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ContractError("invalid_request", f"{field} must be a UUID.") from exc


def _positive_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContractError("invalid_request", f"{field} must be a positive integer.")
    return value


@dataclass(frozen=True)
class OriginalAssetInput:
    id: UUID
    filename: str
    size_bytes: int
    sha256: str
    content_md5: str
    width: int
    height: int

    @classmethod
    def from_dict(cls, raw: object) -> "OriginalAssetInput":
        value = _strict_fields(
            raw,
            {"id", "filename", "size_bytes", "sha256", "content_md5", "width", "height"},
            context="Each asset",
        )
        filename = unicodedata.normalize("NFC", str(value["filename"]).strip())
        if (
            not filename
            or len(filename) > 255
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or any(ord(character) < 32 for character in filename)
        ):
            raise ContractError("invalid_filename", "Asset filenames must be safe basenames.")
        size_bytes = _positive_integer(value["size_bytes"], field="size_bytes")
        if size_bytes > MAX_ORIGINAL_BYTES:
            raise ContractError("asset_too_large", "An original exceeds the 100 MiB limit.")
        width = _positive_integer(value["width"], field="width")
        height = _positive_integer(value["height"], field="height")
        if width * height > MAX_IMAGE_PIXELS:
            raise ContractError("image_too_large", "An original exceeds the 120 MP limit.")
        sha256 = str(value["sha256"])
        if not _SHA256_PATTERN.fullmatch(sha256):
            raise ContractError("invalid_checksum", "sha256 must be a lowercase hex digest.")
        content_md5 = str(value["content_md5"])
        try:
            decoded_md5 = base64.b64decode(content_md5, validate=True)
        except (ValueError, TypeError) as exc:
            raise ContractError("invalid_checksum", "content_md5 must be base64 encoded.") from exc
        if len(decoded_md5) != 16 or base64.b64encode(decoded_md5).decode() != content_md5:
            raise ContractError("invalid_checksum", "content_md5 must encode exactly 16 bytes.")
        return cls(
            id=_uuid(value["id"], field="asset id"),
            filename=filename,
            size_bytes=size_bytes,
            sha256=sha256,
            content_md5=content_md5,
            width=width,
            height=height,
        )

    def as_dict(self) -> dict:
        return {
            "id": str(self.id),
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "content_md5": self.content_md5,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True)
class ContributionInput:
    batch_id: UUID
    label: str
    processing_profile_id: str
    device_label: str
    assets: tuple[OriginalAssetInput, ...]

    @classmethod
    def from_dict(cls, raw: object) -> "ContributionInput":
        value = _strict_fields(
            raw,
            {"batch_id", "label", "processing_profile_id", "device_label", "assets"},
            context="Contribution",
        )
        assets_value = value["assets"]
        if not isinstance(assets_value, list) or not 1 <= len(assets_value) <= MAX_BATCH_ASSETS:
            raise ContractError(
                "invalid_asset_count",
                f"A contribution must contain 1 to {MAX_BATCH_ASSETS} assets.",
            )
        assets = tuple(OriginalAssetInput.from_dict(item) for item in assets_value)
        if len({asset.id for asset in assets}) != len(assets):
            raise ContractError("duplicate_asset_id", "Asset IDs must be unique within a batch.")
        label = str(value["label"]).strip()
        device_label = str(value["device_label"]).strip()
        processing_profile_id = str(value["processing_profile_id"]).strip()
        if len(label) > 100:
            raise ContractError("invalid_request", "The contribution label is too long.")
        if any(ord(character) < 32 for character in label):
            raise ContractError("invalid_request", "The contribution label must be visible text.")
        if not 1 <= len(device_label) <= 100 or any(
            ord(character) < 32 for character in device_label
        ):
            raise ContractError("invalid_request", "A device label is required.")
        if (
            not processing_profile_id
            or len(processing_profile_id) > 100
            or any(ord(character) < 32 for character in processing_profile_id)
        ):
            raise ContractError("invalid_request", "A processing profile is required.")
        return cls(
            batch_id=_uuid(value["batch_id"], field="batch_id"),
            label=label,
            processing_profile_id=processing_profile_id,
            device_label=device_label,
            assets=assets,
        )

    @property
    def original_bytes(self) -> int:
        return sum(asset.size_bytes for asset in self.assets)

    def canonical_document(self) -> dict:
        return {
            "format": "openfotos-contribution-v1",
            "batch_id": str(self.batch_id),
            "label": self.label,
            "processing_profile_id": self.processing_profile_id,
            "assets": [
                asset.as_dict() for asset in sorted(self.assets, key=lambda item: str(item.id))
            ],
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.canonical_document(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
