import hashlib
from io import BytesIO

import pytest
from PIL import Image, ImageChops

from openfotos_contracts import WatermarkTemplate
from openfotos_desktop.derivatives import (
    DerivativeRenderer,
    RenderPolicy,
    apply_watermark,
)


def _source(path, *, size=(120, 240)) -> str:
    image = Image.new("RGB", size, (40, 90, 140))
    exif = Image.Exif()
    exif[274] = 6
    exif[36867] = "2026:09:12 10:20:30"
    exif[34853] = {1: "N", 2: (12.0, 0.0, 0.0), 3: "E", 4: (77.0, 0.0, 0.0)}
    image.save(path, format="JPEG", quality=92, exif=exif)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mark_png() -> bytes:
    mark = Image.new("RGBA", (360, 100), (0, 0, 0, 0))
    for x in range(20, 340):
        for y in range(20, 80):
            mark.putpixel((x, y), (245, 180, 40, 255))
    output = BytesIO()
    mark.save(output, format="PNG")
    return output.getvalue()


def test_derivatives_apply_orientation_strip_metadata_and_preserve_original(tmp_path) -> None:
    source = tmp_path / "source.jpg"
    original_sha256 = _source(source)

    rendered = DerivativeRenderer().render(
        source,
        expected_source_sha256=original_sha256,
        policy=RenderPolicy(False, WatermarkTemplate.COMPACT_BOTTOM_RIGHT),
        cache_directory=tmp_path / "cache",
        asset_stem="asset",
    )

    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_sha256
    assert rendered.captured_at.isoformat() == "2026-09-12T10:20:30"
    for derivative in (rendered.preview, rendered.thumbnail):
        assert (derivative.width, derivative.height) == (240, 120)
        assert derivative.path.stat().st_mode & 0o077 == 0
        with Image.open(derivative.path) as image:
            assert image.getexif() == {}
            assert "icc_profile" not in image.info
            assert image.mode == "RGB"


@pytest.mark.parametrize("template", list(WatermarkTemplate))
def test_every_watermark_template_changes_preview_pixels_but_not_thumbnail(
    tmp_path, template
) -> None:
    source = tmp_path / "source.jpg"
    source_sha256 = _source(source, size=(900, 600))
    renderer = DerivativeRenderer()
    clean = renderer.render(
        source,
        expected_source_sha256=source_sha256,
        policy=RenderPolicy(False, template),
        cache_directory=tmp_path / "clean",
        asset_stem="asset",
    )
    marked = renderer.render(
        source,
        expected_source_sha256=source_sha256,
        policy=RenderPolicy(True, template, _mark_png()),
        cache_directory=tmp_path / template.value,
        asset_stem="asset",
    )

    assert marked.thumbnail.sha256 == clean.thumbnail.sha256
    with (
        Image.open(clean.preview.path) as clean_image,
        Image.open(marked.preview.path) as marked_image,
    ):
        assert ImageChops.difference(clean_image, marked_image).getbbox() is not None


def test_watermark_rejects_a_fully_transparent_mark() -> None:
    output = BytesIO()
    Image.new("RGBA", (100, 40), (0, 0, 0, 0)).save(output, format="PNG")

    with pytest.raises(RuntimeError, match="transparent"):
        apply_watermark(
            Image.new("RGB", (800, 600), "navy"),
            output.getvalue(),
            WatermarkTemplate.CENTER_BRAND,
        )
