"""Portfolio media, event creation, and public-portal lifecycle operations."""

from __future__ import annotations

import base64
import hashlib
import warnings
from dataclasses import dataclass
from datetime import timedelta
from io import BytesIO
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

from openfotos_contracts import EventState
from openfotos_storage import event_cover_key, photographer_logo_key
from openfotos_storage.backend import ObjectAlreadyExists, ObjectStoreError, S3ObjectStore

from .audit import record_audit
from .models import (
    AuditAction,
    AuditResult,
    ConsentAttestation,
    Event,
    FaceSearchResultSet,
    Photographer,
    PortalCapability,
    generate_share_pin,
)
from .services import transition_event

CONSENT_NOTICE_VERSION = "portfolio-face-index-consent-v1"
_ACCEPTED_IMAGE_FORMATS = frozenset({"JPEG", "PNG", "WEBP", "HEIF", "HEIC"})


class PortfolioError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SanitizedImage:
    content: bytes
    content_type: str
    sha256: str
    content_md5: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class PublishedPortal:
    event: Event
    capability: PortalCapability
    pin: str | None


def sanitize_portfolio_image(uploaded, *, logo: bool = False) -> SanitizedImage:
    """Decode, orient, bound, and re-encode a public derivative without source metadata."""
    try:
        return _sanitize_portfolio_image(uploaded, logo=logo)
    finally:
        uploaded.close()


def _sanitize_portfolio_image(uploaded, *, logo: bool) -> SanitizedImage:
    if uploaded.size <= 0 or uploaded.size > settings.PORTFOLIO_IMAGE_MAX_BYTES:
        raise PortfolioError("portfolio_image_too_large", "Choose an image no larger than 20 MB.")
    content = uploaded.read(settings.PORTFOLIO_IMAGE_MAX_BYTES + 1)
    if not content or len(content) > settings.PORTFOLIO_IMAGE_MAX_BYTES:
        raise PortfolioError("portfolio_image_too_large", "Choose an image no larger than 20 MB.")
    register_heif_opener()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as opened:
                if opened.format not in _ACCEPTED_IMAGE_FORMATS or getattr(
                    opened, "is_animated", False
                ):
                    raise PortfolioError(
                        "unsupported_portfolio_image",
                        "Use a still JPEG, PNG, WebP, HEIC, or HEIF image.",
                    )
                width, height = opened.size
                if (
                    width <= 0
                    or height <= 0
                    or width * height > settings.PORTFOLIO_IMAGE_MAX_PIXELS
                ):
                    raise PortfolioError(
                        "portfolio_image_too_large", "Choose an image of at most 40 megapixels."
                    )
                opened.load()
                oriented = ImageOps.exif_transpose(opened)
                converted = _to_srgb(oriented, preserve_alpha=logo)
                converted.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                output = BytesIO()
                if logo:
                    converted.save(output, format="PNG", optimize=True)
                    content_type = "image/png"
                else:
                    converted.convert("RGB").save(
                        output,
                        format="JPEG",
                        quality=88,
                        optimize=True,
                        progressive=True,
                    )
                    content_type = "image/jpeg"
                width, height = converted.size
    except PortfolioError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        ImageCms.PyCMSError,
        OSError,
        UnidentifiedImageError,
        ValueError,
    ) as exc:
        raise PortfolioError(
            "invalid_portfolio_image", "Choose a valid still image for the portfolio."
        ) from exc
    sanitized = output.getvalue()
    md5 = hashlib.md5(sanitized, usedforsecurity=False).digest()
    return SanitizedImage(
        content=sanitized,
        content_type=content_type,
        sha256=hashlib.sha256(sanitized).hexdigest(),
        content_md5=base64.b64encode(md5).decode(),
        width=width,
        height=height,
    )


def _to_srgb(image, *, preserve_alpha: bool):
    output_mode = "RGBA" if preserve_alpha else "RGB"
    icc_profile = image.info.get("icc_profile")
    if not icc_profile:
        return image.convert(output_mode)
    source_profile = ImageCms.ImageCmsProfile(BytesIO(icc_profile))
    target_profile = ImageCms.createProfile("sRGB")
    converted = ImageCms.profileToProfile(
        image,
        source_profile,
        target_profile,
        outputMode="RGB",
    )
    if preserve_alpha and "A" in image.getbands():
        converted.putalpha(image.getchannel("A"))
    return converted.convert(output_mode)


def _put_sanitized(object_store: S3ObjectStore, *, key: str, image: SanitizedImage) -> None:
    try:
        object_store.put_immutable(
            key=key,
            body=image.content,
            content_length=len(image.content),
            content_md5=image.content_md5,
            sha256=image.sha256,
            content_type=image.content_type,
        )
    except ObjectAlreadyExists:
        try:
            head = object_store.head(key)
        except ObjectStoreError as exc:
            raise PortfolioError(
                "object_store_unavailable", "Portfolio media storage is temporarily unavailable."
            ) from exc
        if (
            head is None
            or head.content_length != len(image.content)
            or head.content_type != image.content_type
            or head.metadata.get("openfotos-sha256") != image.sha256
        ):
            raise PortfolioError(
                "portfolio_media_conflict", "The portfolio media key conflicts with stored data."
            ) from None
    except ObjectStoreError as exc:
        raise PortfolioError(
            "object_store_unavailable", "Portfolio media storage is temporarily unavailable."
        ) from exc


def create_event_with_cover(
    *,
    photographer: Photographer,
    name: str,
    cover_upload,
    actor,
    object_store: S3ObjectStore,
    request=None,
) -> Event:
    event_id = uuid4()
    image = sanitize_portfolio_image(cover_upload)
    key = event_cover_key(event_id, image.sha256)
    _put_sanitized(object_store, key=key, image=image)
    try:
        with transaction.atomic():
            event = Event.objects.create(
                id=event_id,
                photographer=photographer,
                name=name.strip(),
                cover_object_key=key,
                cover_sha256=image.sha256,
                cover_width=image.width,
                cover_height=image.height,
            )
            ConsentAttestation.objects.create(
                event=event,
                actor=actor,
                notice_version=CONSENT_NOTICE_VERSION,
            )
            record_audit(
                photographer=photographer,
                event=event,
                actor=actor,
                action=AuditAction.EVENT_CREATED,
                result=AuditResult.SUCCEEDED,
                request=request,
                metadata={"consent_notice_version": CONSENT_NOTICE_VERSION},
            )
            return event
    except Exception:
        object_store.delete(key)
        raise


def update_portfolio_profile(
    *,
    photographer: Photographer,
    display_name: str,
    contact_phone: str,
    instagram_url: str,
    logo_upload,
    actor,
    object_store: S3ObjectStore | None,
    request=None,
) -> Photographer:
    new_logo = sanitize_portfolio_image(logo_upload, logo=True) if logo_upload else None
    new_key = photographer.logo_object_key
    if new_logo is not None:
        if object_store is None:
            raise PortfolioError(
                "object_store_unavailable", "Portfolio media storage is temporarily unavailable."
            )
        new_key = photographer_logo_key(photographer.id, new_logo.sha256)
        _put_sanitized(object_store, key=new_key, image=new_logo)
    with transaction.atomic():
        locked = Photographer.objects.select_for_update().get(pk=photographer.pk)
        locked.display_name = display_name.strip()
        locked.contact_phone = contact_phone.strip()
        locked.instagram_url = instagram_url.strip()
        if new_logo is not None:
            locked.logo_object_key = new_key
            locked.logo_sha256 = new_logo.sha256
            locked.logo_width = new_logo.width
            locked.logo_height = new_logo.height
        locked.full_clean()
        locked.save()
        record_audit(
            photographer=locked,
            actor=actor,
            action=AuditAction.PORTFOLIO_PROFILE_CHANGED,
            result=AuditResult.SUCCEEDED,
            request=request,
        )
        return locked


@transaction.atomic
def publish_event_with_portal(*, event: Event, actor, request=None) -> PublishedPortal:
    locked = Event.objects.select_for_update().select_related("photographer").get(pk=event.pk)
    if not locked.cover_object_key or not ConsentAttestation.objects.filter(event=locked).exists():
        raise ValidationError(
            "An event cover and the current customer-consent attestation are required."
        )
    now = timezone.now()
    if locked.first_published_at is None:
        locked.first_published_at = now
        locked.expires_at = now + timedelta(days=settings.EVENT_RETENTION_DAYS)
        locked.purge_after = locked.expires_at + timedelta(days=settings.EVENT_PURGE_GRACE_DAYS)
        locked.save(update_fields=("first_published_at", "expires_at", "purge_after", "updated_at"))
    published = transition_event(
        event_id=locked.id,
        target=EventState.PUBLISHED,
        actor=actor,
        request=request,
    )
    capability = PortalCapability.objects.select_for_update().filter(event=published).first()
    pin = None
    if capability is None:
        pin = generate_share_pin()
        capability = PortalCapability(event=published, expires_at=published.expires_at)
        capability.set_pin(pin)
        capability.save()
        record_audit(
            photographer=published.photographer,
            event=published,
            actor=actor,
            action=AuditAction.PORTAL_PIN_ISSUED,
            result=AuditResult.SUCCEEDED,
            request=request,
            metadata={"portal_capability_id": str(capability.id)},
        )
    return PublishedPortal(event=published, capability=capability, pin=pin)


@transaction.atomic
def rotate_portal_pin(*, event: Event, actor, request=None) -> PublishedPortal:
    try:
        capability = (
            PortalCapability.objects.select_for_update()
            .select_related("event__photographer")
            .get(event=event)
        )
    except PortalCapability.DoesNotExist as exc:
        raise PortfolioError(
            "portal_not_found", "Publish the event before rotating its PIN."
        ) from exc
    if not capability.event.is_publicly_available():
        raise PortfolioError("portal_unavailable", "The public event portal is unavailable.")
    pin = generate_share_pin()
    capability.set_pin(pin)
    capability.access_version = uuid4()
    capability.save(update_fields=("pin_hash", "access_version", "updated_at"))
    FaceSearchResultSet.objects.filter(portal_capability=capability).delete()
    record_audit(
        photographer=capability.event.photographer,
        event=capability.event,
        actor=actor,
        action=AuditAction.PORTAL_PIN_ROTATED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"portal_capability_id": str(capability.id)},
    )
    return PublishedPortal(event=capability.event, capability=capability, pin=pin)
