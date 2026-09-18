from io import BytesIO

import numpy
import pytest
from PIL import Image

from openfotos_vision import FaceImageError, decode_srgb_bgr


def test_face_decode_applies_exif_orientation_and_returns_bgr() -> None:
    source = Image.new("RGB", (2, 3), color=(255, 0, 0))
    exif = Image.Exif()
    exif[274] = 6
    encoded = BytesIO()
    source.save(encoded, format="JPEG", exif=exif)

    decoded = decode_srgb_bgr(encoded.getvalue())

    assert decoded.shape == (2, 3, 3)
    assert numpy.mean(decoded[:, :, 0]) < 5
    assert numpy.mean(decoded[:, :, 2]) > 245


def test_face_decode_rejects_non_image_bytes() -> None:
    with pytest.raises(FaceImageError, match="decoded safely"):
        decode_srgb_bgr(b"not an image")


def test_face_decode_rejects_invalid_embedded_color_profile() -> None:
    image = Image.new("RGB", (2, 2), color=(1, 2, 3))
    encoded = BytesIO()
    image.save(encoded, format="JPEG", icc_profile=b"not an icc profile")

    with pytest.raises(FaceImageError, match="decoded safely"):
        decode_srgb_bgr(encoded.getvalue())
