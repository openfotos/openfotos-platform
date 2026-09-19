#!/usr/bin/env python3
"""Build the OneNodeAI Studio window and installer icons from the provided logo.

The source paints the mark on a near-black square. This script extracts the mark and
places it on a white disc so the round logo stays visible on both dark and light
desktops instead of rendering as a black tile.
"""

from __future__ import annotations

import argparse
import struct
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

REPOSITORY = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = REPOSITORY / "logo.png"
DEFAULT_ICON_DESTINATION = REPOSITORY / "packaging" / "icons"
DEFAULT_RUNTIME_DESTINATION = (
    REPOSITORY / "apps" / "desktop" / "src" / "openfotos_desktop" / "assets" / "logo.png"
)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
ICNS_TYPES = {
    16: b"icp4",
    32: b"icp5",
    64: b"icp6",
    128: b"ic07",
    256: b"ic08",
    512: b"ic09",
    1024: b"ic10",
}
_BACKGROUND_FLOOR = 20
_MARK_CEILING = 140
_DISC_COLOR = (255, 255, 255, 255)
_MARK_COLOR = (16, 24, 40, 255)
_DISC_FILL = 0.94
_MARK_FILL = 0.68
_SUPERSAMPLE = 2
_RUNTIME_EDGE = 256
_ICON_EDGE = 1024


def _alpha_for_luminance(value: int) -> int:
    if value <= _BACKGROUND_FLOOR:
        return 0
    return min(255, round((value - _BACKGROUND_FLOOR) * 255 / (_MARK_CEILING - _BACKGROUND_FLOOR)))


def _mark_mask(source: Image.Image) -> Image.Image:
    alpha = source.convert("L").point(_alpha_for_luminance)
    if "A" in source.getbands():
        alpha = ImageChops.multiply(alpha, source.getchannel("A"))
    bbox = alpha.getbbox()
    if bbox is None:
        raise ValueError("The icon source does not contain a visible mark.")
    mark = Image.new("RGBA", source.size, (255, 255, 255, 0))
    mark.putalpha(alpha)
    return mark.crop(bbox)


def _badge(mark: Image.Image, size: int) -> Image.Image:
    canvas_size = size * _SUPERSAMPLE
    canvas = Image.new("RGBA", (canvas_size, canvas_size), (255, 255, 255, 0))
    inset = round(canvas_size * (1 - _DISC_FILL) / 2)
    ImageDraw.Draw(canvas).ellipse(
        (inset, inset, canvas_size - inset, canvas_size - inset),
        fill=_DISC_COLOR,
    )
    target = round(canvas_size * _MARK_FILL)
    scale = target / max(mark.width, mark.height)
    scaled = mark.resize(
        (max(1, round(mark.width * scale)), max(1, round(mark.height * scale))),
        Image.Resampling.LANCZOS,
    )
    ink = Image.new("RGBA", scaled.size, _MARK_COLOR)
    ink.putalpha(scaled.getchannel("A"))
    canvas.alpha_composite(
        ink, ((canvas_size - scaled.width) // 2, (canvas_size - scaled.height) // 2)
    )
    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def build_icons(
    source: Path, destination: Path, runtime_destination: Path
) -> tuple[Path, Path, Path]:
    destination.mkdir(parents=True, exist_ok=True)
    runtime_destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as opened:
        badge = _badge(_mark_mask(opened), _ICON_EDGE)
        ico_path = destination / "onenodeai-studio.ico"
        # Pillow derives ICO frames with aspect-preserving thumbnails, so start from a square.
        badge.save(ico_path, format="ICO", sizes=[(size, size) for size in ICO_SIZES])
        icns_path = destination / "onenodeai-studio.icns"
        icns_path.write_bytes(_icns_bytes(badge))
        runtime_destination.write_bytes(_runtime_bytes(badge))
    return ico_path, icns_path, runtime_destination


def _icns_bytes(badge: Image.Image) -> bytes:
    chunks = bytearray()
    for size, kind in sorted(ICNS_TYPES.items()):
        payload = _png_bytes(badge, size)
        chunks += kind + struct.pack(">I", len(payload) + 8) + payload
    header = b"icns" + struct.pack(">I", len(chunks) + 8)
    return header + bytes(chunks)


def _runtime_bytes(badge: Image.Image) -> bytes:
    return _png_bytes(badge, _RUNTIME_EDGE)


def _png_bytes(badge: Image.Image, size: int) -> bytes:
    buffer = BytesIO()
    badge.resize((size, size), Image.Resampling.LANCZOS).save(buffer, format="PNG")
    return buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--destination", type=Path, default=DEFAULT_ICON_DESTINATION)
    parser.add_argument(
        "--runtime-destination",
        type=Path,
        default=DEFAULT_RUNTIME_DESTINATION,
        help="path of the packaged desktop window-icon PNG",
    )
    arguments = parser.parse_args()
    ico_path, icns_path, runtime_path = build_icons(
        arguments.source, arguments.destination, arguments.runtime_destination
    )
    print(f"Wrote {ico_path}")
    print(f"Wrote {icns_path}")
    print(f"Wrote {runtime_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
