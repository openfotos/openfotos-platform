"""Authorized gallery queries and photographer-controlled delivery policy."""

from dataclasses import dataclass
from uuid import UUID

from django.conf import settings
from django.core.paginator import Page, Paginator
from django.db import transaction
from django.db.models import Exists, OuterRef, QuerySet
from django.utils import timezone

from openfotos_contracts import AssetVariant, EventState, OriginalDownloadPolicy, UploadObjectState
from openfotos_storage.backend import ObjectStoreError, S3ObjectStore

from .audit import record_audit
from .derivative_services import refresh_derivative_readiness
from .event_lifecycle import state_for_derivative_readiness
from .ingestion_services import IngestionError
from .models import Asset, AssetObject, AuditAction, AuditResult, Event

GALLERY_PAGE_SIZE = 48


@dataclass(frozen=True)
class GalleryImage:
    asset: Asset
    url: str
    width: int
    height: int


def available_gallery_assets(event: Event) -> QuerySet[Asset]:
    original = AssetObject.objects.filter(
        asset_id=OuterRef("pk"),
        variant=AssetVariant.ORIGINAL.value,
        state=UploadObjectState.VERIFIED.value,
    )
    preview = AssetObject.objects.filter(
        asset_id=OuterRef("pk"),
        variant=AssetVariant.PREVIEW.value,
        state=UploadObjectState.VERIFIED.value,
    )
    thumbnail = AssetObject.objects.filter(
        asset_id=OuterRef("pk"),
        variant=AssetVariant.THUMBNAIL.value,
        state=UploadObjectState.VERIFIED.value,
    )
    return (
        Asset.objects.filter(
            batch__device__event=event,
            gallery_excluded_at__isnull=True,
        )
        .annotate(
            has_original=Exists(original),
            has_preview=Exists(preview),
            has_thumbnail=Exists(thumbnail),
        )
        .filter(has_original=True, has_preview=True, has_thumbnail=True)
        .order_by("gallery_position", "id")
    )


def gallery_page(
    *, event: Event, page_number: object, object_store: S3ObjectStore
) -> tuple[Page, list[GalleryImage]]:
    page = Paginator(available_gallery_assets(event), GALLERY_PAGE_SIZE).get_page(page_number)
    images = [
        _signed_image(asset=asset, variant=AssetVariant.THUMBNAIL, object_store=object_store)
        for asset in page.object_list
    ]
    return page, images


def gallery_photo(
    *, event: Event, asset_id: UUID, object_store: S3ObjectStore
) -> tuple[GalleryImage, Asset | None, Asset | None]:
    assets = available_gallery_assets(event)
    try:
        asset = assets.get(pk=asset_id)
    except Asset.DoesNotExist as exc:
        raise IngestionError("asset_not_found", "The gallery photo is unavailable.") from exc
    ordered_assets = list(assets)
    position = next(
        index for index, candidate in enumerate(ordered_assets) if candidate.id == asset.id
    )
    previous_asset = ordered_assets[position - 1] if position else None
    next_asset = ordered_assets[position + 1] if position + 1 < len(ordered_assets) else None
    return (
        _signed_image(asset=asset, variant=AssetVariant.PREVIEW, object_store=object_store),
        previous_asset,
        next_asset,
    )


@transaction.atomic
def change_download_policy(
    *, event: Event, actor, policy: OriginalDownloadPolicy, request=None
) -> Event:
    locked = Event.objects.select_for_update().select_related("photographer").get(pk=event.pk)
    if locked.original_download_policy == policy.value:
        return locked
    previous = locked.original_download_policy
    locked.original_download_policy = policy.value
    locked.save(update_fields=("original_download_policy", "updated_at"))
    record_audit(
        photographer=locked.photographer,
        event=locked,
        actor=actor,
        action=AuditAction.DOWNLOAD_POLICY_CHANGED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"from": previous, "to": policy.value},
    )
    return locked


def exclude_from_gallery(
    *, event: Event, asset_id: UUID, actor, reason: str, request=None
) -> Asset:
    normalized_reason = reason.strip()
    if not 1 <= len(normalized_reason) <= 240 or any(
        ord(character) < 32 for character in normalized_reason
    ):
        raise IngestionError("invalid_exclusion_reason", "An exclusion reason is required.")
    with transaction.atomic():
        locked_event = Event.objects.select_for_update().get(pk=event.pk)
        if locked_event.state not in {EventState.PROCESSING.value, EventState.REVIEW.value}:
            raise IngestionError(
                "event_not_reviewable", "Gallery exclusions require Processing or Review state."
            )
        try:
            asset = (
                Asset.objects.select_for_update()
                .select_related("batch__device")
                .get(
                    pk=asset_id,
                    batch__device__event=locked_event,
                )
            )
        except Asset.DoesNotExist as exc:
            raise IngestionError("asset_not_found", "The gallery photo is unavailable.") from exc
        if asset.gallery_excluded_at is not None:
            return asset
        derivatives = AssetObject.objects.filter(
            asset=asset,
            variant__in=(AssetVariant.PREVIEW.value, AssetVariant.THUMBNAIL.value),
        )
        has_reported_failure = (
            bool(asset.derivative_failure_code)
            or derivatives.filter(state=UploadObjectState.FAILED.value).exists()
        )
        if asset.derivative_attempt_count < 5 or not has_reported_failure:
            raise IngestionError(
                "derivative_retries_remaining",
                "Retry derivative processing five times before excluding this photo.",
            )
        asset.gallery_excluded_at = timezone.now()
        asset.gallery_exclusion_reason = normalized_reason
        asset.gallery_excluded_by = actor
        asset.gallery_position = None
        asset.save(
            update_fields=(
                "gallery_excluded_at",
                "gallery_exclusion_reason",
                "gallery_excluded_by",
                "gallery_position",
            )
        )
        locked_event.derivatives_ready_generation = None
        locked_event.save(update_fields=("derivatives_ready_generation", "updated_at"))
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=actor,
        action=AuditAction.ASSET_GALLERY_EXCLUDED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"asset_id": str(asset.id), "reason": normalized_reason},
    )
    refresh_derivative_readiness(event.id)
    return asset


def restore_to_gallery(*, event: Event, asset_id: UUID, actor, request=None) -> Asset:
    with transaction.atomic():
        locked_event = Event.objects.select_for_update().get(pk=event.pk)
        if locked_event.state not in {EventState.PROCESSING.value, EventState.REVIEW.value}:
            raise IngestionError(
                "event_not_reviewable", "Gallery restoration requires Processing or Review state."
            )
        try:
            asset = Asset.objects.select_for_update().get(
                pk=asset_id,
                batch__device__event=locked_event,
            )
        except Asset.DoesNotExist as exc:
            raise IngestionError("asset_not_found", "The gallery photo is unavailable.") from exc
        if asset.gallery_excluded_at is None:
            return asset
        asset.gallery_excluded_at = None
        asset.gallery_exclusion_reason = ""
        asset.gallery_excluded_by = None
        asset.gallery_position = None
        asset.save(
            update_fields=(
                "gallery_excluded_at",
                "gallery_exclusion_reason",
                "gallery_excluded_by",
                "gallery_position",
            )
        )
        locked_event.derivatives_ready_generation = None
        locked_event.state = state_for_derivative_readiness(
            EventState(locked_event.state), ready=False
        ).value
        locked_event.save(update_fields=("derivatives_ready_generation", "state", "updated_at"))
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=actor,
        action=AuditAction.ASSET_GALLERY_RESTORED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"asset_id": str(asset.id)},
    )
    refresh_derivative_readiness(event.id)
    return asset


def _signed_image(
    *, asset: Asset, variant: AssetVariant, object_store: S3ObjectStore
) -> GalleryImage:
    object_record = AssetObject.objects.get(
        asset=asset,
        variant=variant.value,
        state=UploadObjectState.VERIFIED.value,
    )
    try:
        signed = object_store.presign_get(
            key=object_record.object_key,
            expires_in_seconds=settings.SIGNED_URL_TTL_SECONDS,
        )
    except ObjectStoreError as exc:
        raise IngestionError(
            "object_store_unavailable", "Gallery media is temporarily unavailable.", retryable=True
        ) from exc
    return GalleryImage(
        asset=asset,
        url=signed.url,
        width=object_record.width,
        height=object_record.height,
    )
