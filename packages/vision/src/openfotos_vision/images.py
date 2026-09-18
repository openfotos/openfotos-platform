"""Pinned orientation and color decoding for face processing."""

from __future__ import annotations

from io import BytesIO


class FaceImageError(ValueError):
    """Image bytes cannot enter the accepted face preprocessing pipeline."""


def decode_srgb_bgr(image_bytes: bytes) -> object:
    import cv2
    import numpy
    from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

    try:
        with Image.open(BytesIO(image_bytes)) as opened:
            opened.load()
            oriented = ImageOps.exif_transpose(opened)
            converted = _to_srgb(oriented, ImageCms)
            rgb = numpy.asarray(converted, dtype=numpy.uint8)
    except (ImageCms.PyCMSError, OSError, UnidentifiedImageError, ValueError) as exc:
        raise FaceImageError("The benchmark image cannot be decoded safely.") from exc
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise FaceImageError("The benchmark image is not a color image.")
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def _to_srgb(image: object, image_cms: object) -> object:
    icc_profile = image.info.get("icc_profile")
    if not icc_profile:
        return image.convert("RGB")
    source_profile = image_cms.ImageCmsProfile(BytesIO(icc_profile))
    target_profile = image_cms.createProfile("sRGB")
    return image_cms.profileToProfile(
        image,
        source_profile,
        target_profile,
        outputMode="RGB",
    )
