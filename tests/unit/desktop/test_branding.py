from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication

from openfotos_contracts import WatermarkLogoKind
from openfotos_desktop.branding import (
    WatermarkCompositionError,
    compose_watermark_mark,
)


@pytest.fixture(scope="module", autouse=True)
def application():
    existing = QApplication.instance()
    return existing if existing is not None else QApplication([])


def test_builtin_wordmark_and_unicode_text_produce_a_transparent_png() -> None:
    content = compose_watermark_mark(
        logo_kind=WatermarkLogoKind.OFTS,
        text="OFTS · विवाह",
    )

    with Image.open(BytesIO(content)) as image:
        assert image.format == "PNG"
        assert image.mode == "RGBA"
        assert image.width <= 2048
        assert image.getbbox()


def test_custom_logo_must_be_a_png_with_real_transparency(tmp_path: Path) -> None:
    opaque = tmp_path / "opaque.png"
    Image.new("RGBA", (80, 30), (20, 30, 40, 255)).save(opaque, format="PNG")

    with pytest.raises(WatermarkCompositionError, match="contain transparency"):
        compose_watermark_mark(
            logo_kind=WatermarkLogoKind.CUSTOM,
            text="",
            custom_logo_path=opaque,
        )


def test_enabled_mark_requires_logo_or_text() -> None:
    with pytest.raises(WatermarkCompositionError, match="Choose a logo"):
        compose_watermark_mark(logo_kind=WatermarkLogoKind.NONE, text="")


def test_fully_transparent_colored_pixels_are_rejected(tmp_path: Path) -> None:
    transparent = tmp_path / "transparent.png"
    Image.new("RGBA", (80, 30), (20, 30, 40, 0)).save(transparent, format="PNG")

    with pytest.raises(WatermarkCompositionError, match="fully transparent"):
        compose_watermark_mark(
            logo_kind=WatermarkLogoKind.CUSTOM,
            text="",
            custom_logo_path=transparent,
        )
