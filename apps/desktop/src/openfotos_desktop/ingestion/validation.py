"""JPEG validation and checksum mechanics."""

import base64
import hashlib
import warnings
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .models import InventoryStatus, RejectionReason, ScanLimits, ValidationResult

JPEG_EXTENSIONS = frozenset({".jpg", ".jpeg"})
JPEG_CONTENT_TYPE = "image/jpeg"
_READ_CHUNK_BYTES = 1024 * 1024


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
        if path.suffix.lower() not in JPEG_EXTENSIONS:
            return self._rejected(RejectionReason.UNSUPPORTED_EXTENSION)
        if size_bytes > self.limits.max_file_bytes:
            return self._rejected(RejectionReason.FILE_TOO_LARGE)
        if not self._has_jpeg_signature(path):
            return self._rejected(RejectionReason.CONTENT_TYPE_MISMATCH)

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    if image.format != "JPEG":
                        return self._rejected(RejectionReason.CONTENT_TYPE_MISMATCH)
                    width, height = image.size
                    if width * height > self.limits.max_pixels:
                        return self._rejected(RejectionReason.IMAGE_TOO_LARGE)
                    image.load()
        except Image.DecompressionBombError:
            return self._rejected(RejectionReason.IMAGE_TOO_LARGE)
        except (OSError, UnidentifiedImageError, ValueError):
            return self._rejected(RejectionReason.INVALID_JPEG)

        sha256, content_md5 = file_checksums(path)
        return ValidationResult(
            status=InventoryStatus.ACCEPTED,
            reason=None,
            content_type=JPEG_CONTENT_TYPE,
            sha256=sha256,
            content_md5=content_md5,
            width=width,
            height=height,
        )

    @staticmethod
    def _has_jpeg_signature(path: Path) -> bool:
        with path.open("rb") as source:
            return source.read(3) == b"\xff\xd8\xff"

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
