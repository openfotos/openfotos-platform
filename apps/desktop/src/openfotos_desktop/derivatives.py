"""Deterministic local gallery derivative and watermark rendering."""

import base64
import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageCms, ImageDraw, ImageOps, UnidentifiedImageError

try:
    from pillow_heif import register_heif_opener
except ImportError:  # pragma: no cover - dependency is installed in packaged desktop builds
    register_heif_opener = None

from openfotos_contracts import DERIVATIVE_PROFILE, AssetVariant, WatermarkTemplate

if register_heif_opener is not None:
    register_heif_opener()


class DerivativeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RenderPolicy:
    enabled: bool
    template: WatermarkTemplate
    mark_png: bytes = b""


@dataclass(frozen=True)
class RenderedObject:
    variant: AssetVariant
    path: Path
    size_bytes: int
    sha256: str
    content_md5: str
    width: int
    height: int

    def as_contract(self) -> dict:
        return {
            "variant": self.variant.value,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "content_md5": self.content_md5,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True)
class RenderedAsset:
    source_sha256: str
    captured_at: datetime | None
    preview: RenderedObject
    thumbnail: RenderedObject


class DerivativeRenderer:
    profile_id = DERIVATIVE_PROFILE.id

    def render(
        self,
        source_path: Path,
        *,
        expected_source_sha256: str,
        policy: RenderPolicy,
        cache_directory: Path,
        asset_stem: str,
    ) -> RenderedAsset:
        source_path = Path(source_path)
        source_sha256 = _file_sha256(source_path)
        if source_sha256 != expected_source_sha256:
            raise DerivativeError(
                "source_changed",
                "The source bytes changed after the original was verified.",
            )
        try:
            with Image.open(source_path) as opened:
                captured_at = _capture_time(opened)
                opened.load()
                oriented = ImageOps.exif_transpose(opened)
                source = _to_srgb(oriented)
        except (OSError, UnidentifiedImageError) as exc:
            raise DerivativeError(
                "invalid_image", "The verified source can no longer be decoded."
            ) from exc

        cache_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            cache_directory.chmod(0o700)
        preview_image = _resize_copy(source, DERIVATIVE_PROFILE.preview.maximum_long_edge)
        if policy.enabled:
            if not policy.mark_png:
                raise DerivativeError(
                    "watermark_mark_missing", "The confirmed watermark is unavailable."
                )
            preview_image = apply_watermark(preview_image, policy.mark_png, policy.template)
        thumbnail_image = _resize_copy(source, DERIVATIVE_PROFILE.thumbnail.maximum_long_edge)
        preview = _write_jpeg(
            preview_image,
            cache_directory / f"{asset_stem}-preview.jpg",
            variant=AssetVariant.PREVIEW,
            quality=DERIVATIVE_PROFILE.preview.jpeg_quality,
        )
        thumbnail = _write_jpeg(
            thumbnail_image,
            cache_directory / f"{asset_stem}-thumbnail.jpg",
            variant=AssetVariant.THUMBNAIL,
            quality=DERIVATIVE_PROFILE.thumbnail.jpeg_quality,
        )
        return RenderedAsset(
            source_sha256=source_sha256,
            captured_at=captured_at,
            preview=preview,
            thumbnail=thumbnail,
        )


def render_for_review(image: Image.Image, policy: RenderPolicy) -> Image.Image:
    source = _to_srgb(image)
    preview = _resize_copy(source, DERIVATIVE_PROFILE.preview.maximum_long_edge)
    if policy.enabled:
        if not policy.mark_png:
            raise DerivativeError("watermark_mark_missing", "Choose a logo, text, or both.")
        return apply_watermark(preview, policy.mark_png, policy.template)
    return preview


def apply_watermark(
    image: Image.Image,
    mark_png: bytes,
    template: WatermarkTemplate,
) -> Image.Image:
    try:
        with Image.open(BytesIO(mark_png)) as opened:
            if opened.format != "PNG":
                raise DerivativeError(
                    "invalid_watermark_image", "The confirmed watermark mark is not a PNG."
                )
            opened.load()
            mark = opened.convert("RGBA")
    except (OSError, UnidentifiedImageError) as exc:
        raise DerivativeError(
            "invalid_watermark_image", "The confirmed watermark mark cannot be decoded."
        ) from exc
    if not mark.getchannel("A").getbbox():
        raise DerivativeError("empty_watermark", "The confirmed watermark mark is transparent.")

    canvas = image.convert("RGBA")
    if template is WatermarkTemplate.COMPACT_BOTTOM_RIGHT:
        _place_edge_mark(canvas, mark, width_ratio=0.24, height_ratio=0.09, centered=False)
    elif template is WatermarkTemplate.BOTTOM_CENTER:
        _place_edge_mark(canvas, mark, width_ratio=0.38, height_ratio=0.11, centered=True)
    elif template is WatermarkTemplate.CENTER_BRAND:
        fitted = _fit_mark(mark, int(canvas.width * 0.52), int(canvas.height * 0.22))
        fitted = _with_opacity(fitted, 0.34)
        x = (canvas.width - fitted.width) // 2
        y = (canvas.height - fitted.height) // 2
        shadow = Image.new("RGBA", canvas.size)
        shadow_mark = _with_opacity(_solid_alpha(fitted, (0, 0, 0)), 0.26)
        shadow.alpha_composite(shadow_mark, (x + max(2, canvas.width // 500), y + 2))
        canvas.alpha_composite(shadow)
        canvas.alpha_composite(fitted, (x, y))
    elif template is WatermarkTemplate.REPEATED_DIAGONAL:
        fitted = _fit_mark(mark, int(canvas.width * 0.19), int(canvas.height * 0.09))
        fitted = _with_opacity(fitted, 0.20)
        tile = fitted.rotate(330, resample=Image.Resampling.BICUBIC, expand=True)
        spacing_x = max(tile.width + canvas.width // 12, 1)
        spacing_y = max(tile.height + canvas.height // 10, 1)
        for row, y in enumerate(range(-tile.height, canvas.height + tile.height, spacing_y)):
            offset = -(spacing_x // 2) if row % 2 else 0
            for x in range(offset - tile.width, canvas.width + tile.width, spacing_x):
                canvas.alpha_composite(tile, (x, y))
    else:  # pragma: no cover - enum makes this defensive boundary unreachable
        raise DerivativeError(
            "unsupported_watermark_template", "The watermark template is unsupported."
        )
    return canvas.convert("RGB")


def _place_edge_mark(
    canvas: Image.Image,
    mark: Image.Image,
    *,
    width_ratio: float,
    height_ratio: float,
    centered: bool,
) -> None:
    fitted = _fit_mark(mark, int(canvas.width * width_ratio), int(canvas.height * height_ratio))
    fitted = _with_opacity(fitted, 0.88)
    margin = max(12, round(min(canvas.size) * 0.03))
    padding = max(8, round(min(canvas.size) * 0.012))
    x = (canvas.width - fitted.width) // 2 if centered else canvas.width - fitted.width - margin
    y = canvas.height - fitted.height - margin
    plate = Image.new("RGBA", canvas.size)
    draw = ImageDraw.Draw(plate)
    draw.rounded_rectangle(
        (x - padding, y - padding, x + fitted.width + padding, y + fitted.height + padding),
        radius=padding,
        fill=(0, 0, 0, 96),
    )
    canvas.alpha_composite(plate)
    canvas.alpha_composite(fitted, (x, y))


def _fit_mark(mark: Image.Image, maximum_width: int, maximum_height: int) -> Image.Image:
    if maximum_width <= 0 or maximum_height <= 0:
        raise DerivativeError("image_too_small", "The preview is too small for a watermark.")
    scale = min(maximum_width / mark.width, maximum_height / mark.height)
    size = (max(1, round(mark.width * scale)), max(1, round(mark.height * scale)))
    return mark.resize(size, Image.Resampling.LANCZOS) if size != mark.size else mark.copy()


def _with_opacity(image: Image.Image, opacity: float) -> Image.Image:
    result = image.copy()
    alpha = result.getchannel("A").point(lambda value: round(value * opacity))
    result.putalpha(alpha)
    return result


def _solid_alpha(image: Image.Image, color: tuple[int, int, int]) -> Image.Image:
    result = Image.new("RGBA", image.size, (*color, 0))
    result.putalpha(image.getchannel("A"))
    return result


def _resize_copy(image: Image.Image, long_edge: int) -> Image.Image:
    width, height = image.size
    scale = min(1.0, long_edge / max(width, height))
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS) if size != image.size else image.copy()


def _to_srgb(image: Image.Image) -> Image.Image:
    icc_profile = image.info.get("icc_profile")
    if not icc_profile:
        return image.convert("RGB")
    try:
        source_profile = ImageCms.ImageCmsProfile(BytesIO(icc_profile))
        target_profile = ImageCms.createProfile("sRGB")
        return ImageCms.profileToProfile(image, source_profile, target_profile, outputMode="RGB")
    except (OSError, ValueError) as exc:
        raise DerivativeError(
            "invalid_color_profile", "The photograph contains an invalid color profile."
        ) from exc


def _capture_time(image: Image.Image) -> datetime | None:
    try:
        value = image.getexif().get(36867)
    except (OSError, ValueError):
        return None
    if not isinstance(value, str):
        return None
    try:
        captured = datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    if not 1970 <= captured.year <= datetime.now().year + 1:
        return None
    return captured


def _write_jpeg(
    image: Image.Image,
    path: Path,
    *,
    variant: AssetVariant,
    quality: int,
) -> RenderedObject:
    partial = path.with_suffix(".part")
    image.convert("RGB").save(
        partial,
        format="JPEG",
        quality=quality,
        optimize=DERIVATIVE_PROFILE.optimize,
        progressive=DERIVATIVE_PROFILE.progressive,
        subsampling=DERIVATIVE_PROFILE.subsampling,
    )
    partial.replace(path)
    if os.name != "nt":
        path.chmod(0o600)
    content = path.read_bytes()
    return RenderedObject(
        variant=variant,
        path=path,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        content_md5=base64.b64encode(hashlib.md5(content, usedforsecurity=False).digest()).decode(),
        width=image.width,
        height=image.height,
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise DerivativeError("source_unavailable", "The verified source is unavailable.") from exc
    return digest.hexdigest()
