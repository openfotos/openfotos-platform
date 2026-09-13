"""Preview policy, private derivative uploads, and readiness reconciliation."""

import base64
import hashlib
import re
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import NAMESPACE_URL, UUID, uuid5

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from openfotos_contracts import (
    DERIVATIVE_PROFILE,
    DERIVATIVE_PROFILE_ID,
    DERIVATIVE_VARIANTS,
    WATERMARK_RENDERER_ID,
    AssetDerivativesInput,
    AssetVariant,
    EventState,
    IngestionManifestState,
    PreviewPolicyInput,
    PreviewPolicySnapshot,
    UploadObjectState,
    WatermarkLogoKind,
    WatermarkTemplate,
)
from openfotos_storage import asset_key, preview_policy_mark_key
from openfotos_storage.backend import ObjectAlreadyExists, ObjectStoreError, S3ObjectStore

from .audit import record_audit
from .event_lifecycle import state_for_derivative_readiness
from .ingestion_services import IngestionError, event_for_session
from .models import (
    Asset,
    AssetObject,
    AuditAction,
    AuditResult,
    DesktopSession,
    Event,
    PreviewPolicy,
)

MAX_MARK_INPUT_BYTES = 4 * 1024 * 1024
MAX_MARK_PIXELS = 4_000_000
MAX_MARK_EDGE = 2048
_NATURAL_PART = re.compile(r"(\d+)")


def preview_policy_snapshot(policy: PreviewPolicy | None) -> PreviewPolicySnapshot | None:
    if policy is None:
        return None
    return PreviewPolicySnapshot(
        id=policy.id,
        enabled=policy.enabled,
        template=WatermarkTemplate(policy.template),
        text=policy.text,
        logo_kind=WatermarkLogoKind(policy.logo_kind),
        renderer_id=policy.renderer_id,
        derivative_profile_id=policy.derivative_profile_id,
        mark_sha256=policy.mark_sha256,
    )


def confirm_preview_policy(
    *,
    session: DesktopSession,
    event_id: UUID,
    value: PreviewPolicyInput,
    object_store: S3ObjectStore | None,
    request=None,
) -> PreviewPolicy:
    event = _lead_event(session, event_id)
    policy_id = uuid5(NAMESPACE_URL, f"openfotos:preview-policy:{event.id}")
    normalized_mark = b""
    mark_width = 0
    mark_height = 0
    mark_key = None
    mark_sha256 = ""
    if value.enabled:
        if object_store is None:  # pragma: no cover - guarded by the API boundary
            raise IngestionError(
                "object_store_unavailable", "Object storage is not configured.", retryable=True
            )
        normalized_mark, mark_width, mark_height = normalize_mark_png(value.mark_png)
        mark_sha256 = hashlib.sha256(normalized_mark).hexdigest()
        mark_key = preview_policy_mark_key(event.id, policy_id)

    with transaction.atomic():
        locked_event = Event.objects.select_for_update().get(pk=event.pk)
        existing = PreviewPolicy.objects.filter(event=locked_event).first()
        if existing is not None:
            _require_matching_policy(existing, value, mark_sha256)
            return existing
        if locked_event.state in {
            EventState.REVIEW.value,
            EventState.PUBLISHED.value,
            EventState.ARCHIVED.value,
            EventState.CANCELLED.value,
        }:
            raise IngestionError(
                "preview_policy_locked",
                "Preview settings must be confirmed before gallery review.",
            )
        if AssetObject.objects.filter(
            asset__batch__device__event=locked_event,
            variant__in=(AssetVariant.PREVIEW.value, AssetVariant.THUMBNAIL.value),
        ).exists():
            raise IngestionError(
                "preview_policy_locked", "Preview settings cannot change after processing begins."
            )
    if value.enabled:
        _put_immutable(
            object_store,
            key=mark_key,
            content=normalized_mark,
            sha256=mark_sha256,
            content_type="image/png",
        )

    with transaction.atomic():
        locked_event = Event.objects.select_for_update().get(pk=event.pk)
        existing = PreviewPolicy.objects.filter(event=locked_event).first()
        if existing is not None:
            _require_matching_policy(existing, value, mark_sha256)
            return existing
        policy = PreviewPolicy.objects.create(
            id=policy_id,
            event=locked_event,
            enabled=value.enabled,
            template=value.template.value,
            text=value.text,
            logo_kind=value.logo_kind.value,
            renderer_id=WATERMARK_RENDERER_ID,
            derivative_profile_id=DERIVATIVE_PROFILE_ID,
            mark_object_key=mark_key,
            mark_sha256=mark_sha256,
            mark_bytes=len(normalized_mark),
            mark_width=mark_width,
            mark_height=mark_height,
            confirmed_by=session.user,
        )
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=session.user,
        action=AuditAction.PREVIEW_POLICY_CONFIRMED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={
            "policy_id": str(policy.id),
            "enabled": policy.enabled,
            "template": policy.template,
            "logo_kind": policy.logo_kind,
        },
    )
    return policy


def _require_matching_policy(
    existing: PreviewPolicy,
    value: PreviewPolicyInput,
    mark_sha256: str,
) -> None:
    expected = (
        value.enabled,
        value.template.value,
        value.text,
        value.logo_kind.value,
        mark_sha256,
        WATERMARK_RENDERER_ID,
        DERIVATIVE_PROFILE_ID,
    )
    actual = (
        existing.enabled,
        existing.template,
        existing.text,
        existing.logo_kind,
        existing.mark_sha256,
        existing.renderer_id,
        existing.derivative_profile_id,
    )
    if actual != expected:
        raise IngestionError(
            "preview_policy_already_confirmed",
            "Different preview settings are already locked for this event.",
        )


def normalize_mark_png(content: bytes) -> tuple[bytes, int, int]:
    if not content or len(content) > MAX_MARK_INPUT_BYTES:
        raise IngestionError(
            "invalid_watermark_image", "The rendered watermark PNG exceeds the 4 MiB limit."
        )
    try:
        with Image.open(BytesIO(content)) as opened:
            if opened.format != "PNG":
                raise IngestionError(
                    "invalid_watermark_image", "The rendered watermark must be a PNG."
                )
            width, height = opened.size
            if (
                width <= 0
                or height <= 0
                or width > MAX_MARK_EDGE
                or height > MAX_MARK_EDGE
                or width * height > MAX_MARK_PIXELS
            ):
                raise IngestionError(
                    "invalid_watermark_image", "The rendered watermark dimensions are unsupported."
                )
            opened.load()
            normalized = opened.convert("RGBA")
    except IngestionError:
        raise
    except (OSError, UnidentifiedImageError) as exc:
        raise IngestionError(
            "invalid_watermark_image", "The rendered watermark cannot be decoded."
        ) from exc
    if not normalized.getchannel("A").getbbox():
        raise IngestionError(
            "invalid_watermark_image", "The rendered watermark cannot be fully transparent."
        )
    output = BytesIO()
    normalized.save(output, format="PNG", optimize=True)
    result = output.getvalue()
    if len(result) > MAX_MARK_INPUT_BYTES:
        raise IngestionError(
            "invalid_watermark_image", "The normalized watermark PNG exceeds the 4 MiB limit."
        )
    return result, width, height


def issue_policy_mark_url(
    *,
    session: DesktopSession,
    event_id: UUID,
    object_store: S3ObjectStore,
) -> dict:
    event = event_for_session(session, event_id)
    policy = PreviewPolicy.objects.filter(event=event).first()
    if policy is None:
        raise IngestionError(
            "preview_policy_not_confirmed", "The lead has not confirmed preview settings."
        )
    if not policy.enabled:
        return {"policy_id": str(policy.id), "url": None, "expires_at": None}
    try:
        signed = object_store.presign_get(
            key=policy.mark_object_key,
            expires_in_seconds=settings.SIGNED_URL_TTL_SECONDS,
        )
    except ObjectStoreError as exc:
        raise IngestionError(
            "object_store_unavailable",
            "The confirmed watermark is temporarily unavailable.",
            retryable=True,
        ) from exc
    return {
        "policy_id": str(policy.id),
        "sha256": policy.mark_sha256,
        "url": signed.url,
        "expires_at": signed.expires_at.isoformat(),
    }


@transaction.atomic
def register_asset_derivatives(
    *, session: DesktopSession, event_id: UUID, value: AssetDerivativesInput
) -> list[AssetObject]:
    event = event_for_session(session, event_id)
    policy = PreviewPolicy.objects.filter(event=event).first()
    if policy is None:
        raise IngestionError(
            "preview_policy_not_confirmed", "The lead has not confirmed preview settings."
        )
    if value.policy_id != policy.id:
        raise IngestionError(
            "preview_policy_mismatch", "The desktop preview policy does not match the event."
        )
    if value.profile_id != policy.derivative_profile_id:
        raise IngestionError(
            "derivative_profile_mismatch",
            "The desktop derivative profile does not match the event.",
        )
    asset = _owned_asset(session=session, event=event, asset_id=value.asset_id, for_update=True)
    if asset.sha256 != value.source_sha256:
        raise IngestionError(
            "source_checksum_mismatch",
            "The derivative source does not match the uploaded original.",
        )
    original = AssetObject.objects.select_for_update().get(
        asset=asset,
        variant=AssetVariant.ORIGINAL.value,
    )
    if original.state != UploadObjectState.VERIFIED.value:
        raise IngestionError(
            "original_not_verified", "Verify the immutable original before its derivatives."
        )
    if event.state in {
        EventState.PUBLISHED.value,
        EventState.ARCHIVED.value,
        EventState.CANCELLED.value,
    }:
        raise IngestionError("event_not_processing", "This event no longer accepts derivatives.")

    _validate_capture_time(value.captured_at)
    if asset.captured_at is not None and value.captured_at is not None:
        supplied = timezone.make_aware(value.captured_at, timezone.get_current_timezone())
        if asset.captured_at != supplied:
            raise IngestionError(
                "derivative_manifest_conflict", "The asset capture time is already immutable."
            )
    elif asset.captured_at is None and value.captured_at is not None:
        asset.captured_at = timezone.make_aware(value.captured_at, timezone.get_current_timezone())
        asset.save(update_fields=("captured_at",))

    if asset.derivative_failure_code:
        asset.derivative_failure_code = ""
        asset.derivative_failure_at = None
        asset.save(update_fields=("derivative_failure_code", "derivative_failure_at"))

    objects: list[AssetObject] = []
    for item in value.objects:
        _validate_dimensions(asset, item.variant, item.width, item.height)
        variant = AssetVariant(item.variant.value)
        existing = (
            AssetObject.objects.select_for_update()
            .filter(asset=asset, variant=variant.value)
            .first()
        )
        expected = {
            "object_key": asset_key(event.id, asset.id, variant),
            "expected_bytes": item.size_bytes,
            "sha256": item.sha256,
            "content_md5": item.content_md5,
            "width": item.width,
            "height": item.height,
        }
        if existing is not None:
            if any(getattr(existing, field) != value for field, value in expected.items()):
                raise IngestionError(
                    "derivative_manifest_conflict",
                    "The derivative metadata differs from its immutable reservation.",
                )
            objects.append(existing)
            continue
        objects.append(AssetObject.objects.create(asset=asset, variant=variant.value, **expected))
    return objects


@transaction.atomic
def report_derivative_failure(
    *,
    session: DesktopSession,
    event_id: UUID,
    asset_id: UUID,
    code: str,
    request=None,
) -> Asset:
    normalized_code = code.strip()
    if not re.fullmatch(r"[a-z0-9_]{1,64}", normalized_code):
        raise IngestionError(
            "invalid_failure_code", "A stable derivative failure code is required."
        )
    event = event_for_session(session, event_id)
    asset = _owned_asset(session=session, event=event, asset_id=asset_id, for_update=True)
    asset.derivative_failure_code = normalized_code
    asset.derivative_failure_at = timezone.now()
    asset.derivative_attempt_count = F("derivative_attempt_count") + 1
    asset.save(
        update_fields=(
            "derivative_failure_code",
            "derivative_failure_at",
            "derivative_attempt_count",
        )
    )
    asset.refresh_from_db()
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=session.user,
        uploader_device=asset.batch.device,
        action=AuditAction.DERIVATIVE_FAILED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"asset_id": str(asset.id), "code": normalized_code},
    )
    return asset


def issue_derivative_leases(
    *,
    session: DesktopSession,
    event_id: UUID,
    asset_id: UUID,
    variants: tuple[AssetVariant, ...],
    object_store: S3ObjectStore,
) -> list[dict]:
    event = event_for_session(session, event_id)
    asset = _owned_asset(session=session, event=event, asset_id=asset_id)
    if not variants or len(set(variants)) != len(variants):
        raise IngestionError("invalid_request", "Choose one or two unique derivative variants.")
    uploads = AssetObject.objects.filter(
        asset=asset,
        variant__in=[variant.value for variant in variants],
        state__in=(UploadObjectState.RESERVED.value, UploadObjectState.FAILED.value),
    ).order_by("variant")
    leases = []
    for upload in uploads:
        try:
            signed = object_store.presign_put(
                key=upload.object_key,
                content_length=upload.expected_bytes,
                content_md5=upload.content_md5,
                sha256=upload.sha256,
                expires_in_seconds=settings.UPLOAD_LEASE_TTL_SECONDS,
            )
        except ObjectStoreError as exc:
            raise IngestionError(
                "object_store_unavailable",
                "A derivative upload lease could not be created.",
                retryable=True,
            ) from exc
        leases.append(
            {
                "asset_id": str(asset.id),
                "variant": upload.variant,
                "url": signed.url,
                "headers": signed.headers,
                "expires_at": signed.expires_at.isoformat(),
            }
        )
    return leases


def issue_owned_original_url(
    *,
    session: DesktopSession,
    event_id: UUID,
    asset_id: UUID,
    object_store: S3ObjectStore,
) -> dict:
    event = event_for_session(session, event_id)
    asset = _owned_asset(session=session, event=event, asset_id=asset_id)
    original = AssetObject.objects.get(asset=asset, variant=AssetVariant.ORIGINAL.value)
    if original.state != UploadObjectState.VERIFIED.value:
        raise IngestionError("original_not_verified", "The immutable original is unavailable.")
    try:
        signed = object_store.presign_get(
            key=original.object_key,
            expires_in_seconds=settings.SIGNED_URL_TTL_SECONDS,
        )
    except ObjectStoreError as exc:
        raise IngestionError(
            "object_store_unavailable", "The original is temporarily unavailable.", retryable=True
        ) from exc
    return {
        "asset_id": str(asset.id),
        "sha256": original.sha256,
        "url": signed.url,
        "expires_at": signed.expires_at.isoformat(),
    }


def verify_derivative(
    *,
    session: DesktopSession,
    event_id: UUID,
    asset_id: UUID,
    variant: AssetVariant,
    object_store: S3ObjectStore,
    request=None,
) -> AssetObject:
    event = event_for_session(session, event_id)
    asset = _owned_asset(session=session, event=event, asset_id=asset_id)
    upload = AssetObject.objects.filter(asset=asset, variant=variant.value).first()
    if upload is None:
        raise IngestionError("derivative_not_found", "The derivative reservation is unavailable.")
    if upload.state == UploadObjectState.VERIFIED.value:
        return upload
    try:
        head = object_store.head(upload.object_key)
    except ObjectStoreError as exc:
        raise IngestionError(
            "object_store_unavailable", "The derivative could not be verified.", retryable=True
        ) from exc
    if head is None:
        raise IngestionError(
            "derivative_missing", "The derivative upload is missing.", retryable=True
        )
    mismatch = _object_mismatch(upload, head)
    with transaction.atomic():
        locked = AssetObject.objects.select_for_update().get(pk=upload.pk)
        if mismatch:
            try:
                object_store.delete(locked.object_key)
            except ObjectStoreError as exc:
                raise IngestionError(
                    "object_store_unavailable",
                    "The invalid derivative could not be removed.",
                    retryable=True,
                ) from exc
            locked.state = UploadObjectState.FAILED.value
            locked.failure_code = mismatch
            locked.save(update_fields=("state", "failure_code", "updated_at"))
        else:
            locked.state = UploadObjectState.VERIFIED.value
            locked.etag = head.etag
            locked.verified_at = timezone.now()
            locked.failure_code = ""
            locked.save(
                update_fields=("state", "etag", "verified_at", "failure_code", "updated_at")
            )
    if mismatch:
        raise IngestionError(mismatch, "The derivative did not match its immutable reservation.")
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=session.user,
        uploader_device=asset.batch.device,
        action=AuditAction.DERIVATIVE_UPLOAD_VERIFIED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"asset_id": str(asset.id), "variant": variant.value},
    )
    refresh_derivative_readiness(event.id)
    return locked


@transaction.atomic
def refresh_derivative_readiness(event_id: UUID) -> bool:
    event = (
        Event.objects.select_for_update()
        .select_related("current_ingestion_manifest")
        .get(pk=event_id)
    )
    manifest = event.current_ingestion_manifest
    if (
        manifest is None
        or manifest.state != IngestionManifestState.COMMITTED.value
        or manifest.generation != event.intake_generation
    ):
        return False
    assets = list(
        Asset.objects.filter(
            batch__device__event=event,
            variant_objects__variant=AssetVariant.ORIGINAL.value,
            variant_objects__state=UploadObjectState.VERIFIED.value,
            gallery_excluded_at__isnull=True,
        ).distinct()
    )
    if not assets:
        event.derivatives_ready_generation = None
        event.save(update_fields=("derivatives_ready_generation", "updated_at"))
        return False
    asset_ids = [asset.id for asset in assets]
    verified_pairs = AssetObject.objects.filter(
        asset_id__in=asset_ids,
        variant__in=(AssetVariant.PREVIEW.value, AssetVariant.THUMBNAIL.value),
        state=UploadObjectState.VERIFIED.value,
    ).count()
    if verified_pairs != len(assets) * 2:
        event.derivatives_ready_generation = None
        next_state = state_for_derivative_readiness(EventState(event.state), ready=False)
        if next_state.value != event.state:
            previous_state = event.state
            event.state = next_state.value
            event.save(update_fields=("derivatives_ready_generation", "state", "updated_at"))
            _record_automatic_transition(event, previous_state)
        else:
            event.save(update_fields=("derivatives_ready_generation", "updated_at"))
        return False

    ordered = sorted(assets, key=_gallery_sort_key)
    for position, asset in enumerate(ordered, start=1):
        asset.gallery_position = position
    Asset.objects.bulk_update(ordered, ("gallery_position",))
    event.derivatives_ready_generation = event.intake_generation
    next_state = state_for_derivative_readiness(EventState(event.state), ready=True)
    if next_state.value != event.state:
        previous_state = event.state
        event.state = next_state.value
    else:
        previous_state = event.state
    event.save(update_fields=("derivatives_ready_generation", "state", "updated_at"))
    if previous_state != event.state:
        _record_automatic_transition(event, previous_state)
    return True


def _record_automatic_transition(event: Event, previous_state: str) -> None:
    record_audit(
        photographer=event.photographer,
        event=event,
        action=AuditAction.EVENT_STATE_CHANGED,
        result=AuditResult.SUCCEEDED,
        metadata={
            "from": previous_state,
            "to": event.state,
            "automatic": "derivative_readiness",
        },
    )


def _lead_event(session: DesktopSession, event_id: UUID) -> Event:
    event = event_for_session(session, event_id)
    if session.user_id is None:
        raise IngestionError("lead_required", "Only the event lead may perform this action.")
    return event


def _owned_asset(
    *,
    session: DesktopSession,
    event: Event,
    asset_id: UUID,
    for_update: bool = False,
) -> Asset:
    query = Asset.objects.select_related("batch__device")
    if for_update:
        query = query.select_for_update()
    try:
        asset = query.get(pk=asset_id, batch__device__event=event)
    except Asset.DoesNotExist as exc:
        raise IngestionError("asset_not_found", "The asset is unavailable.") from exc
    if session.user_id is None and asset.batch.device_id != session.device_id:
        raise IngestionError("asset_not_found", "The asset is unavailable.")
    if asset.batch.device.status != "active":
        raise IngestionError("device_revoked", "This contribution device has been revoked.")
    return asset


def _validate_dimensions(asset: Asset, variant: AssetVariant, width: int, height: int) -> None:
    if variant not in DERIVATIVE_VARIANTS:
        raise IngestionError("invalid_derivative_variant", "Unknown derivative variant.")
    profile = DERIVATIVE_PROFILE.for_variant(variant)
    expected_long_edge = min(profile.maximum_long_edge, max(asset.width, asset.height))
    if max(width, height) != expected_long_edge:
        raise IngestionError(
            "derivative_dimensions_mismatch", "The derivative dimensions do not match the profile."
        )
    original_ratio = asset.width / asset.height
    derivative_ratio = width / height
    ratio_error = min(
        abs(original_ratio - derivative_ratio),
        abs((1 / original_ratio) - derivative_ratio),
    )
    if ratio_error > 0.03:
        raise IngestionError(
            "derivative_dimensions_mismatch",
            "The derivative aspect ratio does not match the original.",
        )


def _validate_capture_time(value: datetime | None) -> None:
    if value is None:
        return
    earliest = datetime(1970, 1, 1)
    latest = datetime.now() + timedelta(days=366)
    if not earliest <= value <= latest:
        raise IngestionError(
            "invalid_capture_time", "The capture time is outside the allowed range."
        )


def _object_mismatch(upload: AssetObject, head) -> str:
    if head.content_length != upload.expected_bytes:
        return "derivative_size_mismatch"
    if head.content_type.lower().partition(";")[0] != "image/jpeg":
        return "derivative_content_type_mismatch"
    if head.metadata.get("openfotos-sha256") != upload.sha256:
        return "derivative_checksum_mismatch"
    expected_etag = base64.b64decode(upload.content_md5).hex()
    if head.etag.lower() != expected_etag:
        return "derivative_checksum_mismatch"
    return ""


def _put_immutable(
    object_store: S3ObjectStore,
    *,
    key: str,
    content: bytes,
    sha256: str,
    content_type: str,
) -> None:
    content_md5 = base64.b64encode(hashlib.md5(content, usedforsecurity=False).digest()).decode()
    try:
        object_store.put_immutable(
            key=key,
            body=content,
            content_length=len(content),
            content_md5=content_md5,
            sha256=sha256,
            content_type=content_type,
        )
    except ObjectAlreadyExists:
        try:
            head = object_store.head(key)
        except ObjectStoreError as exc:
            raise IngestionError(
                "object_store_unavailable",
                "The existing watermark could not be verified.",
                retryable=True,
            ) from exc
        if (
            head is None
            or head.content_length != len(content)
            or head.metadata.get("openfotos-sha256") != sha256
        ):
            raise IngestionError(
                "watermark_object_conflict", "The immutable watermark key contains other bytes."
            ) from None
    except ObjectStoreError as exc:
        raise IngestionError(
            "object_store_unavailable", "The watermark could not be stored.", retryable=True
        ) from exc


def _gallery_sort_key(asset: Asset) -> tuple:
    filename_parts = tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in _NATURAL_PART.split(asset.original_filename)
        if part
    )
    captured = asset.captured_at or datetime.max.replace(tzinfo=UTC)
    return asset.captured_at is None, captured, filename_parts, str(asset.id)
