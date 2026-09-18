"""Ephemeral reference-photo processing and non-authorizing result state."""

from __future__ import annotations

import warnings
from datetime import timedelta
from io import BytesIO

from django.conf import settings
from django.utils import timezone

from openfotos_vision import ACCEPTED_FACE_MODEL_CONTRACT, FaceEngineError

from .face_services import search_face_index
from .models import FaceSearchResultSet, GuestCapability, OwnerCapability, SubEvent

_ACCEPTED_IMAGE_FORMATS = frozenset({"JPEG", "PNG", "WEBP", "HEIF", "HEIC"})


class FaceSearchError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def create_face_search(
    *,
    capability: OwnerCapability | GuestCapability,
    sub_event: SubEvent | None,
    uploaded_photo,
    engine,
) -> FaceSearchResultSet:
    image_bytes = _read_reference_photo(uploaded_photo)
    try:
        _validate_reference_image(image_bytes)
        try:
            detected_faces = engine.detect_and_embed(image_bytes)
        except FaceEngineError as exc:
            raise FaceSearchError(
                "face_search_processing_failed",
                "This photo could not be processed safely. Try another clear photo.",
            ) from exc
        usable_faces = tuple(
            face
            for face in detected_faces
            if face.detector_confidence
            >= ACCEPTED_FACE_MODEL_CONTRACT.quality.detector_confidence_threshold
            and min(face.bounding_box_width, face.bounding_box_height)
            >= ACCEPTED_FACE_MODEL_CONTRACT.quality.minimum_face_size_pixels
        )
        if not usable_faces:
            raise FaceSearchError(
                "no_usable_face",
                "No clear usable face was found. Try another single-person photo.",
            )
        if len(usable_faces) > 1:
            raise FaceSearchError(
                "multiple_usable_faces",
                "More than one usable face was found. Choose a photo containing one person.",
            )
        event = (
            capability.event if isinstance(capability, OwnerCapability) else capability.owner.event
        )
        results = search_face_index(
            event_id=event.id,
            sub_event_id=sub_event.id if sub_event else None,
            embedding=usable_faces[0].embedding,
        )
        values = {
            "event": event,
            "sub_event": sub_event,
            "ordered_asset_ids": [str(result.asset_id) for result in results],
            "expires_at": timezone.now()
            + timedelta(seconds=settings.FACE_SEARCH_RESULT_TTL_SECONDS),
        }
        if isinstance(capability, OwnerCapability):
            values["owner_capability"] = capability
        else:
            values["guest_capability"] = capability
        return FaceSearchResultSet.objects.create(**values)
    finally:
        image_bytes = b""


def delete_face_search(
    *,
    result_id,
    capability: OwnerCapability | GuestCapability,
) -> None:
    query = FaceSearchResultSet.objects.filter(pk=result_id)
    if isinstance(capability, OwnerCapability):
        query = query.filter(owner_capability=capability)
    else:
        query = query.filter(guest_capability=capability)
    query.delete()


def _read_reference_photo(uploaded_photo) -> bytes:
    try:
        size = uploaded_photo.size
        if size <= 0 or size > settings.FACE_SEARCH_MAX_UPLOAD_BYTES:
            raise FaceSearchError(
                "reference_photo_too_large",
                "Choose a non-empty photo no larger than 20 MB.",
            )
        content = uploaded_photo.read(settings.FACE_SEARCH_MAX_UPLOAD_BYTES + 1)
        if not content or len(content) > settings.FACE_SEARCH_MAX_UPLOAD_BYTES:
            raise FaceSearchError(
                "reference_photo_too_large",
                "Choose a non-empty photo no larger than 20 MB.",
            )
        return content
    finally:
        uploaded_photo.close()


def _validate_reference_image(image_bytes: bytes) -> None:
    from PIL import Image, UnidentifiedImageError
    from pillow_heif import register_heif_opener

    register_heif_opener()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(image_bytes)) as image:
                if image.format not in _ACCEPTED_IMAGE_FORMATS:
                    raise FaceSearchError(
                        "unsupported_reference_photo",
                        "Use a JPEG, PNG, WebP, HEIC, or HEIF photo.",
                    )
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > settings.FACE_SEARCH_MAX_PIXELS:
                    raise FaceSearchError(
                        "reference_photo_too_large",
                        "Choose a photo of at most 40 megapixels.",
                    )
                image.verify()
    except FaceSearchError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        raise FaceSearchError(
            "invalid_reference_photo",
            "Choose a valid JPEG, PNG, WebP, HEIC, or HEIF photo.",
        ) from exc
