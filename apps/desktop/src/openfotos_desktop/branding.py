"""Compose the event's immutable raster watermark mark on a photographer installation."""

import unicodedata
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

from openfotos_contracts import MAX_WATERMARK_TEXT_LENGTH, WatermarkLogoKind

from .theme import asset_path

MAX_CUSTOM_LOGO_BYTES = 4 * 1024 * 1024
MAX_CUSTOM_LOGO_PIXELS = 4_000_000
MAX_CUSTOM_LOGO_EDGE = 2048
MAX_COMPOSED_MARK_EDGE = 1900


class WatermarkCompositionError(ValueError):
    pass


def compose_watermark_mark(
    *,
    logo_kind: WatermarkLogoKind,
    text: str,
    custom_logo_path: Path | None = None,
) -> bytes:
    normalized_text = _validate_text(text)
    logo = _load_logo(logo_kind, custom_logo_path)
    if logo is None and not normalized_text:
        raise WatermarkCompositionError("Choose a logo, enter watermark text, or use both.")

    text_font = QFont("Sans Serif")
    text_font.setPixelSize(96)
    text_font.setWeight(QFont.Weight.DemiBold)
    text_metrics = QFontMetricsF(text_font)
    text_width = round(text_metrics.horizontalAdvance(normalized_text)) if normalized_text else 0
    text_height = round(text_metrics.height()) if normalized_text else 0
    logo_width = logo.width() if logo is not None else 0
    logo_height = logo.height() if logo is not None else 0
    gap = 46 if logo is not None and normalized_text else 0
    padding = 12
    width = logo_width + gap + text_width + padding * 2
    height = max(logo_height, text_height) + padding * 2
    if width > MAX_COMPOSED_MARK_EDGE or height > MAX_COMPOSED_MARK_EDGE:
        scale = min(
            (MAX_COMPOSED_MARK_EDGE - padding * 2) / max(width - padding * 2, 1),
            (MAX_COMPOSED_MARK_EDGE - padding * 2) / max(height - padding * 2, 1),
        )
        logo = _scaled(logo, scale) if logo is not None else None
        logo_width = logo.width() if logo is not None else 0
        logo_height = logo.height() if logo is not None else 0
        text_font.setPixelSize(max(12, round(text_font.pixelSize() * scale)))
        text_metrics = QFontMetricsF(text_font)
        text_width = (
            round(text_metrics.horizontalAdvance(normalized_text)) if normalized_text else 0
        )
        text_height = round(text_metrics.height()) if normalized_text else 0
        gap = round(gap * scale)
        width = logo_width + gap + text_width + padding * 2
        height = max(logo_height, text_height) + padding * 2

    canvas = QImage(max(1, width), max(1, height), QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    x = padding
    if logo is not None:
        painter.drawImage(x, (height - logo.height()) // 2, logo)
        x += logo.width() + gap
    if normalized_text:
        painter.setFont(text_font)
        painter.setPen(QColor("#f8fafc"))
        baseline = (height - text_metrics.height()) / 2 + text_metrics.ascent()
        painter.drawText(
            QRectF(x, baseline - text_metrics.ascent(), text_width, text_metrics.height()),
            normalized_text,
        )
    painter.end()
    return _png_bytes(canvas)


def _validate_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.strip())
    if len(normalized) > MAX_WATERMARK_TEXT_LENGTH:
        raise WatermarkCompositionError(
            f"Watermark text is limited to {MAX_WATERMARK_TEXT_LENGTH} characters."
        )
    if any(character in "\r\n" or ord(character) < 32 for character in normalized):
        raise WatermarkCompositionError("Watermark text must be a single printable line.")
    return normalized


def _load_logo(kind: WatermarkLogoKind, custom_path: Path | None) -> QImage | None:
    if kind is WatermarkLogoKind.NONE:
        return None
    if kind is WatermarkLogoKind.ONENODEAI:
        renderer = QSvgRenderer(str(asset_path("onenodeai.svg")))
        if not renderer.isValid():
            raise WatermarkCompositionError("The built-in OneNodeAI wordmark is unavailable.")
        image = QImage(900, 240, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
        return image
    if kind is not WatermarkLogoKind.CUSTOM or custom_path is None:
        raise WatermarkCompositionError("Choose a transparent PNG logo.")
    content = _validated_custom_png(Path(custom_path))
    image = QImage.fromData(content, "PNG")
    if image.isNull():
        raise WatermarkCompositionError("The custom logo could not be decoded.")
    return image


def _validated_custom_png(path: Path) -> bytes:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise WatermarkCompositionError("The custom logo is unavailable.") from exc
    if not content or len(content) > MAX_CUSTOM_LOGO_BYTES:
        raise WatermarkCompositionError("The custom logo must be a PNG no larger than 4 MiB.")
    try:
        with Image.open(BytesIO(content)) as opened:
            if opened.format != "PNG" or "A" not in opened.getbands():
                raise WatermarkCompositionError("The custom logo must be a transparent PNG.")
            width, height = opened.size
            if (
                width <= 0
                or height <= 0
                or width > MAX_CUSTOM_LOGO_EDGE
                or height > MAX_CUSTOM_LOGO_EDGE
                or width * height > MAX_CUSTOM_LOGO_PIXELS
            ):
                raise WatermarkCompositionError("The custom logo dimensions are unsupported.")
            opened.load()
            alpha = opened.getchannel("A")
            if alpha.getextrema() == (255, 255):
                raise WatermarkCompositionError("The custom logo PNG must contain transparency.")
            if not opened.getchannel("A").getbbox():
                raise WatermarkCompositionError("The custom logo cannot be fully transparent.")
    except WatermarkCompositionError:
        raise
    except (OSError, UnidentifiedImageError) as exc:
        raise WatermarkCompositionError("The custom logo could not be decoded.") from exc
    return content


def _scaled(image: QImage, scale: float) -> QImage:
    return image.scaled(
        max(1, round(image.width() * scale)),
        max(1, round(image.height() * scale)),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _png_bytes(image: QImage) -> bytes:
    content = QByteArray()
    buffer = QBuffer(content)
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not image.save(buffer, "PNG"):
        raise WatermarkCompositionError("The watermark mark could not be encoded.")
    buffer.close()
    return bytes(content)
