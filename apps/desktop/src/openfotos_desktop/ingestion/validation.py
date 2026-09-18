"""Supported original-image validation and checksum mechanics."""

import base64
import hashlib
import warnings
from pathlib import Path

from PIL import Image, UnidentifiedImageError

try:
    from pillow_heif import register_heif_opener
except ImportError:  # pragma: no cover - dependency is installed in packaged desktop builds
    register_heif_opener = None

from .models import InventoryStatus, RejectionReason, ScanLimits, ValidationResult

IMAGE_FORMATS = {
    ".jpg": ("JPEG", "image/jpeg"),
    ".jpeg": ("JPEG", "image/jpeg"),
    ".png": ("PNG", "image/png"),
    ".webp": ("WEBP", "image/webp"),
    ".heic": ("HEIF", "image/heic"),
    ".heif": ("HEIF", "image/heif"),
}
_READ_CHUNK_BYTES = 1024 * 1024

if register_heif_opener is not None:
    register_heif_opener()


def sha256_file(path: Path) -> str:
    return file_checksums(path)[0]


def file_checksums(path: Path) -> tuple[str, str]:
    digest = hashlib.sha256()
    md5_digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as source:
        while chunk := source.read(_READ_CHUNK_BYTES):
            digest.update(chunk)
            md5_digest.update(chunk)
    return digest.hexdigest(), base64.b64encode(md5_digest.digest()).decode()


class InventoryValidator:
    def __init__(self, limits: ScanLimits | None = None) -> None:
        self.limits = limits or ScanLimits()

    def validate(self, path: Path, *, size_bytes: int) -> ValidationResult:
        expected = IMAGE_FORMATS.get(path.suffix.lower())
        if expected is None:
            return self._rejected(RejectionReason.UNSUPPORTED_EXTENSION)
        if size_bytes > self.limits.max_file_bytes:
            return self._rejected(RejectionReason.FILE_TOO_LARGE)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    expected_format, content_type = expected
                    if image.format != expected_format:
                        return self._rejected(RejectionReason.CONTENT_TYPE_MISMATCH)
                    if getattr(image, "is_animated", False):
                        return self._rejected(RejectionReason.UNSUPPORTED_FILE_TYPE)
                    width, height = image.size
                    if width * height > self.limits.max_pixels:
                        return self._rejected(RejectionReason.IMAGE_TOO_LARGE)
                    image.load()
        except Image.DecompressionBombError:
            return self._rejected(RejectionReason.IMAGE_TOO_LARGE)
        except (OSError, UnidentifiedImageError, ValueError):
            return self._rejected(RejectionReason.INVALID_IMAGE)

        sha256, content_md5 = file_checksums(path)
        return ValidationResult(
            status=InventoryStatus.ACCEPTED,
            reason=None,
            content_type=content_type,
            sha256=sha256,
            content_md5=content_md5,
            width=width,
            height=height,
        )

    @staticmethod
    def _rejected(reason: RejectionReason) -> ValidationResult:
        return ValidationResult(
            status=InventoryStatus.REJECTED,
            reason=reason,
            content_type=None,
            sha256=None,
            content_md5=None,
            width=None,
            height=None,
        )
