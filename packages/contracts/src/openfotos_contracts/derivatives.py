"""Stable values and strict inputs for gallery derivatives."""

import base64
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from .ingestion import ContractError, _positive_integer, _strict_fields, _uuid

DERIVATIVE_PROFILE_ID = "gallery-jpeg-v1"
WATERMARK_RENDERER_ID = "watermark-raster-v1"
MAX_WATERMARK_TEXT_LENGTH = 60
MAX_PREVIEW_BYTES = 20 * 1024 * 1024
MAX_THUMBNAIL_BYTES = 5 * 1024 * 1024
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class WatermarkTemplate(StrEnum):
    COMPACT_BOTTOM_RIGHT = "compact-bottom-right"
    BOTTOM_CENTER = "bottom-center"
    CENTER_BRAND = "center-brand"
    REPEATED_DIAGONAL = "repeated-diagonal"


class WatermarkLogoKind(StrEnum):
    NONE = "none"
    OFTS = "ofts"
    CUSTOM = "custom"


class OriginalDownloadPolicy(StrEnum):
    DISABLED = "disabled"
    AUTHORIZED_VISITORS = "authorized-visitors"
    EXPLICIT_SHARES = "explicit-shares"


class DerivativeVariant(StrEnum):
    PREVIEW = "previews"
    THUMBNAIL = "thumbnails"


def normalized_watermark_text(value: object) -> str:
    if not isinstance(value, str):
        raise ContractError("invalid_watermark_text", "Watermark text must be a string.")
    text = unicodedata.normalize("NFC", value.strip())
    if len(text) > MAX_WATERMARK_TEXT_LENGTH or any(
        ord(character) < 32 or character in {"\u2028", "\u2029"} for character in text
    ):
        raise ContractError(
            "invalid_watermark_text",
            "Watermark text must be one visible line of at most "
            f"{MAX_WATERMARK_TEXT_LENGTH} characters.",
        )
    return text


@dataclass(frozen=True)
class PreviewPolicyInput:
    enabled: bool
    template: WatermarkTemplate
    text: str
    logo_kind: WatermarkLogoKind
    mark_png_base64: str

    @classmethod
    def from_dict(cls, raw: object) -> "PreviewPolicyInput":
        value = _strict_fields(
            raw,
            {"enabled", "template", "text", "logo_kind", "mark_png_base64"},
            context="Preview policy",
        )
        if not isinstance(value["enabled"], bool):
            raise ContractError("invalid_request", "enabled must be a boolean.")
        try:
            template = WatermarkTemplate(str(value["template"]))
            logo_kind = WatermarkLogoKind(str(value["logo_kind"]))
        except ValueError as exc:
            raise ContractError(
                "invalid_request", "The watermark selection is unsupported."
            ) from exc
        text = normalized_watermark_text(value["text"])
        if not isinstance(value["mark_png_base64"], str):
            raise ContractError(
                "invalid_watermark_image", "The watermark mark must be base64 encoded."
            )
        mark_png_base64 = value["mark_png_base64"]
        if value["enabled"]:
            if logo_kind is WatermarkLogoKind.NONE and not text:
                raise ContractError(
                    "empty_watermark",
                    "An enabled watermark requires a logo, text, or both.",
                )
            if not mark_png_base64:
                raise ContractError("empty_watermark", "The rendered watermark mark is required.")
            try:
                base64.b64decode(mark_png_base64, validate=True)
            except (ValueError, TypeError) as exc:
                raise ContractError(
                    "invalid_watermark_image", "The watermark mark must be base64 encoded."
                ) from exc
        elif mark_png_base64 or text or logo_kind is not WatermarkLogoKind.NONE:
            raise ContractError(
                "invalid_watermark_image",
                "A disabled watermark cannot include logo, text, or rendered mark bytes.",
            )
        return cls(
            enabled=value["enabled"],
            template=template,
            text=text,
            logo_kind=logo_kind,
            mark_png_base64=mark_png_base64,
        )

    @property
    def mark_png(self) -> bytes:
        return base64.b64decode(self.mark_png_base64) if self.mark_png_base64 else b""


@dataclass(frozen=True)
class DerivativeObjectInput:
    variant: DerivativeVariant
    size_bytes: int
    sha256: str
    content_md5: str
    width: int
    height: int

    @classmethod
    def from_dict(cls, raw: object) -> "DerivativeObjectInput":
        value = _strict_fields(
            raw,
            {"variant", "size_bytes", "sha256", "content_md5", "width", "height"},
            context="Each derivative object",
        )
        try:
            variant = DerivativeVariant(str(value["variant"]))
        except ValueError as exc:
            raise ContractError(
                "invalid_derivative_variant", "Unknown derivative variant."
            ) from exc
        size_bytes = _positive_integer(value["size_bytes"], field="size_bytes")
        maximum = MAX_PREVIEW_BYTES if variant is DerivativeVariant.PREVIEW else MAX_THUMBNAIL_BYTES
        if size_bytes > maximum:
            raise ContractError("derivative_too_large", "The derivative exceeds its byte limit.")
        sha256 = str(value["sha256"])
        if not _SHA256_PATTERN.fullmatch(sha256):
            raise ContractError("invalid_checksum", "sha256 must be a lowercase hex digest.")
        content_md5 = str(value["content_md5"])
        try:
            decoded_md5 = base64.b64decode(content_md5, validate=True)
        except (ValueError, TypeError) as exc:
            raise ContractError("invalid_checksum", "content_md5 must be base64 encoded.") from exc
        if len(decoded_md5) != 16:
            raise ContractError("invalid_checksum", "content_md5 must encode exactly 16 bytes.")
        return cls(
            variant=variant,
            size_bytes=size_bytes,
            sha256=sha256,
            content_md5=content_md5,
            width=_positive_integer(value["width"], field="width"),
            height=_positive_integer(value["height"], field="height"),
        )


@dataclass(frozen=True)
class AssetDerivativesInput:
    asset_id: UUID
    source_sha256: str
    policy_id: UUID
    profile_id: str
    captured_at: datetime | None
    objects: tuple[DerivativeObjectInput, DerivativeObjectInput]

    @classmethod
    def from_dict(cls, raw: object) -> "AssetDerivativesInput":
        value = _strict_fields(
            raw,
            {"asset_id", "source_sha256", "policy_id", "profile_id", "captured_at", "objects"},
            context="Asset derivatives",
        )
        source_sha256 = str(value["source_sha256"])
        if not _SHA256_PATTERN.fullmatch(source_sha256):
            raise ContractError("invalid_checksum", "source_sha256 must be a lowercase hex digest.")
        if value["profile_id"] != DERIVATIVE_PROFILE_ID:
            raise ContractError(
                "derivative_profile_mismatch", "The derivative profile is unsupported."
            )
        objects_raw = value["objects"]
        if not isinstance(objects_raw, list) or len(objects_raw) != 2:
            raise ContractError(
                "invalid_derivative_count", "Each asset requires one preview and one thumbnail."
            )
        parsed_objects = tuple(DerivativeObjectInput.from_dict(item) for item in objects_raw)
        if {item.variant for item in parsed_objects} != set(DerivativeVariant):
            raise ContractError(
                "invalid_derivative_count", "Each asset requires one preview and one thumbnail."
            )
        captured_at = None
        if value["captured_at"] is not None:
            try:
                captured_at = datetime.fromisoformat(str(value["captured_at"]))
            except ValueError as exc:
                raise ContractError(
                    "invalid_capture_time", "captured_at must be an ISO local date and time."
                ) from exc
            if captured_at.tzinfo is not None:
                raise ContractError(
                    "invalid_capture_time", "captured_at must not contain a timezone offset."
                )
        objects = (parsed_objects[0], parsed_objects[1])
        return cls(
            asset_id=_uuid(value["asset_id"], field="asset_id"),
            source_sha256=source_sha256,
            policy_id=_uuid(value["policy_id"], field="policy_id"),
            profile_id=str(value["profile_id"]),
            captured_at=captured_at,
            objects=objects,
        )
