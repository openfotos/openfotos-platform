#!/usr/bin/env python3
"""Build the OneNodeAI Studio installer icons from the provided logo."""

from __future__ import annotations

import argparse
import struct
from io import BytesIO
from pathlib import Path

from PIL import Image

REPOSITORY = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = REPOSITORY / "logo.png"
DEFAULT_DESTINATION = REPOSITORY / "packaging" / "icons"
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


def _square(image: Image.Image, size: int) -> Image.Image:
    source = image.convert("RGBA")
    background = _corner_color(source)
    canvas = Image.new("RGBA", (size, size), background)
    scaled = source.copy()
    scaled.thumbnail((size, size), Image.Resampling.LANCZOS)
    canvas.paste(scaled, ((size - scaled.width) // 2, (size - scaled.height) // 2))
    return canvas


def _corner_color(image: Image.Image) -> tuple[int, int, int, int]:
    samples = [
        image.getpixel((0, 0)),
        image.getpixel((image.width - 1, 0)),
        image.getpixel((0, image.height - 1)),
        image.getpixel((image.width - 1, image.height - 1)),
    ]
    red = sorted(value[0] for value in samples)[len(samples) // 2]
    green = sorted(value[1] for value in samples)[len(samples) // 2]
    blue = sorted(value[2] for value in samples)[len(samples) // 2]
    return (red, green, blue, 255)


def build_icons(source: Path, destination: Path) -> tuple[Path, Path]:
    destination.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as opened:
        image = opened.convert("RGBA")
        ico_path = destination / "onenodeai-studio.ico"
        # Pillow derives ICO frames with aspect-preserving thumbnails, so start from a square.
        _square(image, 1024).save(
            ico_path,
            format="ICO",
            sizes=[(size, size) for size in ICO_SIZES],
        )
        icns_path = destination / "onenodeai-studio.icns"
        icns_path.write_bytes(_icns_bytes(image))
    return ico_path, icns_path


def _icns_bytes(image: Image.Image) -> bytes:
    chunks = bytearray()
    for size, kind in sorted(ICNS_TYPES.items()):
        buffer = BytesIO()
        _square(image, size).save(buffer, format="PNG")
        payload = buffer.getvalue()
        chunks += kind + struct.pack(">I", len(payload) + 8) + payload
    header = b"icns" + struct.pack(">I", len(chunks) + 8)
    return header + bytes(chunks)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    arguments = parser.parse_args()
    ico_path, icns_path = build_icons(arguments.source, arguments.destination)
    print(f"Wrote {ico_path}")
    print(f"Wrote {icns_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
