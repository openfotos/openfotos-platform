"""PIN-only event portal flows for gallery delivery."""

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from openfotos_contracts import AssetVariant
from openfotos_storage.backend import ObjectStoreError
from openfotos_vision import FaceEngineError

from .audit import record_audit
from .cookies import access_cookie_name, has_valid_access_cookie, set_access_cookie
from .face_search_engine import configured_face_search_engine
from .face_search_services import FaceSearchError, create_face_search, delete_face_search
from .forms import FaceSearchForm, SharePinForm
from .gallery_services import (
    GALLERY_PAGE_SIZE,
    available_gallery_assets,
    gallery_images,
    gallery_photo,
    original_download,
    search_gallery_page,
)
from .ingestion_services import IngestionError
from .models import (
    AuditAction,
    AuditResult,
    FaceSearchResultSet,
    PortalCapability,
    RateLimitPurpose,
    SubEvent,
)
from .object_store import configured_object_store
from .rate_limits import clear_failures, consume_attempt, rate_limit_status, register_failure
from .sharing_services import ShareAccessError, portal_for_tenant

GENERIC_PIN_ERROR = "We could not unlock this gallery. Check the PIN and try again."
GENERIC_RATE_LIMIT_ERROR = "Too many attempts. Please wait before trying again."


def _private_response(response: HttpResponse) -> HttpResponse:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


def _private_render(request, template_name, context=None, *, status=None) -> HttpResponse:
    return _private_response(render(request, template_name, context, status=status))


def _portal(request: HttpRequest, event_slug: str) -> PortalCapability:
    photographer = getattr(request, "photographer", None)
    if photographer is None:
        raise Http404
    try:
        return portal_for_tenant(photographer=photographer, event_slug=event_slug)
    except ShareAccessError as exc:
        raise Http404 from exc


def _scope(capability: PortalCapability, requested_sub_event_id) -> SubEvent | None:
    if requested_sub_event_id is None:
        return None
    try:
        return capability.event.sub_events.get(pk=requested_sub_event_id, is_archived=False)
    except SubEvent.DoesNotExist as exc:
        raise Http404 from exc


def _root_redirect(capability: PortalCapability) -> HttpResponse:
    return redirect("events:portal-gallery", capability.event.slug)


def _route_context(capability: PortalCapability) -> dict[str, str]:
    return {
        "gallery_route": "events:portal-gallery",
        "sub_event_route": "events:portal-sub-event",
        "photo_route": "events:portal-photo",
        "sub_event_photo_route": "events:portal-sub-event-photo",
        "download_route": "events:portal-download",
        "sub_event_download_route": "events:portal-sub-event-download",
        "search_route": "events:portal-search",
        "sub_event_search_route": "events:portal-sub-event-search",
        "clear_search_route": "events:portal-clear-search",
        "route_identifier": capability.event.slug,
    }


def _gallery_listing(event, request, *, sub_event=None):
    query = available_gallery_assets(event, sub_event=sub_event)
    page = Paginator(query, GALLERY_PAGE_SIZE).get_page(request.GET.get("page"))
    assets = list(page.object_list)
    if not assets:
        return page, []
    try:
        images = gallery_images(
            assets=assets,
            variant=AssetVariant.THUMBNAIL,
            object_store=configured_object_store(),
        )
        return page, images
    except (ImproperlyConfigured, IngestionError):
        messages.error(request, "Gallery media is temporarily unavailable. Please try again.")
        return Paginator(query.none(), GALLERY_PAGE_SIZE).get_page(1), []


def _render_gallery(
    request: HttpRequest,
    capability: PortalCapability,
    *,
    selected_sub_event: SubEvent | None,
    search_form: FaceSearchForm | None = None,
    status: int | None = None,
) -> HttpResponse:
    event = capability.event
    page, images = _gallery_listing(event, request, sub_event=selected_sub_event)
    return _private_render(
        request,
        "openfotos_events/event.html",
        {
            "event": event,
            "capability": capability,
            "page": page,
            "images": images,
            "sub_events": event.sub_events.filter(is_archived=False),
            "selected_sub_event": selected_sub_event,
            "search_form": search_form or FaceSearchForm(),
            "cover_url": _cover_url(event),
            **_route_context(capability),
        },
        status=status,
    )


def _cover_url(event) -> str:
    if not event.cover_object_key:
        return ""
    try:
        return (
            configured_object_store()
            .presign_get(
                key=event.cover_object_key,
                expires_in_seconds=settings.SIGNED_URL_TTL_SECONDS,
            )
            .url
        )
    except (ImproperlyConfigured, ObjectStoreError):
        return ""


def _brand_logo_url(photographer) -> str:
    if not photographer.logo_object_key:
        return ""
    try:
        return (
            configured_object_store()
            .presign_get(
                key=photographer.logo_object_key,
                expires_in_seconds=settings.SIGNED_URL_TTL_SECONDS,
            )
            .url
        )
    except (ImproperlyConfigured, ObjectStoreError):
        return ""


def _render_cover(
    request: HttpRequest, capability: PortalCapability, *, status=None
) -> HttpResponse:
    event = capability.event
    return _private_render(
        request,
        "openfotos_events/cover.html",
        {
            "event": event,
            "cover_url": _cover_url(event),
            "logo_url": _brand_logo_url(event.photographer),
            "unlock_url": reverse("events:portal-unlock", args=(event.slug,)),
        },
        status=status,
    )


@require_GET
def portal_gallery(request: HttpRequest, event_slug, sub_event_id=None) -> HttpResponse:
    capability = _portal(request, event_slug)
    if has_valid_access_cookie(request, capability):
        return _render_gallery(
            request,
            capability,
            selected_sub_event=_scope(capability, sub_event_id),
        )
    if sub_event_id is not None:
        return _private_response(redirect("events:portal-gallery", capability.event.slug))
    return _render_cover(request, capability)


@sensitive_post_parameters("pin")
@require_http_methods(["GET", "POST"])
def portal_unlock(request: HttpRequest, event_slug) -> HttpResponse:
    capability = _portal(request, event_slug)
    if request.method == "GET":
        if has_valid_access_cookie(request, capability):
            return _private_response(redirect("events:portal-gallery", event_slug))
        return _private_render(
            request,
            "openfotos_events/unlock.html",
            {
                "event": capability.event,
                "form": SharePinForm(),
                "unlock_url": reverse("events:portal-unlock", args=(event_slug,)),
            },
        )
    form = SharePinForm(request.POST)
    subject = str(capability.id)
    current_limit = rate_limit_status(
        purpose=RateLimitPurpose.PORTAL_PIN,
        subject=subject,
        request=request,
    )
    if current_limit.limited:
        return _pin_response(request, capability, form, current_limit.retry_after_seconds)
    if form.is_valid() and capability.check_pin(form.cleaned_data["pin"]):
        clear_failures(
            purpose=RateLimitPurpose.PORTAL_PIN,
            subject=subject,
            request=request,
        )
        record_audit(
            photographer=capability.event.photographer,
            event=capability.event,
            action=AuditAction.PORTAL_PIN_UNLOCK,
            result=AuditResult.SUCCEEDED,
            request=request,
            metadata={"portal_capability_id": str(capability.id)},
        )
        response = _root_redirect(capability)
        set_access_cookie(response, capability)
        return _private_response(response)
    failed_limit = register_failure(
        purpose=RateLimitPurpose.PORTAL_PIN,
        subject=subject,
        request=request,
    )
    record_audit(
        photographer=capability.event.photographer,
        event=capability.event,
        action=AuditAction.PORTAL_PIN_UNLOCK,
        result=AuditResult.RATE_LIMITED if failed_limit.limited else AuditResult.DENIED,
        request=request,
        metadata={"portal_capability_id": str(capability.id)},
    )
    if failed_limit.limited:
        return _pin_response(request, capability, form, failed_limit.retry_after_seconds)
    form.add_error(None, GENERIC_PIN_ERROR)
    return _private_render(
        request,
        "openfotos_events/unlock.html",
        {"event": capability.event, "form": form, "unlock_url": request.path},
    )


def _pin_response(request, capability, form, retry_after_seconds):
    record_audit(
        photographer=capability.event.photographer,
        event=capability.event,
        action=AuditAction.PORTAL_PIN_UNLOCK,
        result=AuditResult.RATE_LIMITED,
        request=request,
        metadata={"portal_capability_id": str(capability.id)},
    )
    form.add_error(None, GENERIC_RATE_LIMIT_ERROR)
    response = _private_render(
        request,
        "openfotos_events/unlock.html",
        {"event": capability.event, "form": form, "unlock_url": request.path},
        status=429,
    )
    response.headers["Retry-After"] = str(retry_after_seconds)
    return response


@require_GET
def portal_photo(request: HttpRequest, event_slug, asset_id, sub_event_id=None) -> HttpResponse:
    capability = _portal(request, event_slug)
    if not has_valid_access_cookie(request, capability):
        raise Http404
    selected_sub_event = _scope(capability, sub_event_id)
    try:
        image, previous_asset, next_asset = gallery_photo(
            event=capability.event,
            asset_id=asset_id,
            object_store=configured_object_store(),
            sub_event=selected_sub_event,
        )
    except IngestionError as exc:
        if exc.code == "asset_not_found":
            raise Http404 from exc
        return _private_response(
            HttpResponse("Gallery media is temporarily unavailable.", status=503)
        )
    except ImproperlyConfigured:
        return _private_response(
            HttpResponse("Gallery media is temporarily unavailable.", status=503)
        )
    return _private_render(
        request,
        "openfotos_events/photo.html",
        {
            "event": capability.event,
            "capability": capability,
            "image": image,
            "previous_asset": previous_asset,
            "next_asset": next_asset,
            "dashboard_mode": False,
            "selected_sub_event": selected_sub_event,
            **_route_context(capability),
        },
    )


@require_GET
def portal_download(request: HttpRequest, event_slug, asset_id, sub_event_id=None) -> HttpResponse:
    capability = _portal(request, event_slug)
    if not has_valid_access_cookie(request, capability):
        raise Http404
    try:
        download = original_download(
            event=capability.event,
            asset_id=asset_id,
            object_store=configured_object_store(),
            sub_event=_scope(capability, sub_event_id),
        )
    except IngestionError as exc:
        if exc.code == "asset_not_found":
            raise Http404 from exc
        return _private_response(
            HttpResponse("The original is temporarily unavailable.", status=503)
        )
    except ImproperlyConfigured:
        return _private_response(
            HttpResponse("The original is temporarily unavailable.", status=503)
        )
    record_audit(
        photographer=capability.event.photographer,
        event=capability.event,
        action=AuditAction.ORIGINAL_DOWNLOAD_ISSUED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={
            "asset_id": str(download.asset.id),
            "portal_capability_id": str(capability.id),
            "sha256": download.sha256,
        },
    )
    return _private_response(redirect(download.url))


@sensitive_post_parameters("reference_photo")
@require_POST
def portal_search(request: HttpRequest, event_slug, sub_event_id=None) -> HttpResponse:
    capability = _portal(request, event_slug)
    if not has_valid_access_cookie(request, capability):
        raise Http404
    selected_sub_event = _scope(capability, sub_event_id)
    form = FaceSearchForm(request.POST, request.FILES)
    if not form.is_valid():
        _close_uploaded(request)
        return _render_gallery(
            request,
            capability,
            selected_sub_event=selected_sub_event,
            search_form=form,
            status=400,
        )
    client_limit = consume_attempt(
        purpose=RateLimitPurpose.FACE_SEARCH_CLIENT,
        subject=str(capability.id),
        request=request,
        client_identifier=request.COOKIES.get(access_cookie_name(capability)),
    )
    capability_limit = None
    if not client_limit.limited:
        capability_limit = consume_attempt(
            purpose=RateLimitPurpose.FACE_SEARCH_CAPABILITY,
            subject=str(capability.id),
            request=request,
            client_scoped=False,
        )
    if client_limit.limited or (capability_limit is not None and capability_limit.limited):
        _close_uploaded(request)
        form.add_error(None, GENERIC_RATE_LIMIT_ERROR)
        response = _render_gallery(
            request,
            capability,
            selected_sub_event=selected_sub_event,
            search_form=form,
            status=429,
        )
        response.headers["Retry-After"] = str(
            max(
                client_limit.retry_after_seconds,
                capability_limit.retry_after_seconds if capability_limit else 0,
            )
        )
        return response
    try:
        result_set = create_face_search(
            capability=capability,
            sub_event=selected_sub_event,
            uploaded_photo=form.cleaned_data["reference_photo"],
            engine=configured_face_search_engine(),
        )
    except FaceSearchError as exc:
        form.add_error("reference_photo", str(exc))
        return _render_gallery(
            request,
            capability,
            selected_sub_event=selected_sub_event,
            search_form=form,
            status=400,
        )
    except (FaceEngineError, ImproperlyConfigured):
        return _private_response(
            HttpResponse("Face search is temporarily unavailable.", status=503)
        )
    finally:
        _close_uploaded(request)
    record_audit(
        photographer=capability.event.photographer,
        event=capability.event,
        action=AuditAction.FACE_SEARCH_COMPLETED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={
            "portal_capability_id": str(capability.id),
            "result_count": len(result_set.ordered_asset_ids),
            "sub_event_id": str(selected_sub_event.id) if selected_sub_event else None,
        },
    )
    return redirect(
        "events:portal-search-results",
        capability.event.slug,
        result_set.id,
    )


@require_GET
def portal_search_results(request: HttpRequest, event_slug, result_id) -> HttpResponse:
    capability = _portal(request, event_slug)
    if not has_valid_access_cookie(request, capability):
        raise Http404
    result_set = (
        FaceSearchResultSet.objects.select_related("event", "sub_event")
        .filter(
            pk=result_id,
            event=capability.event,
            portal_capability=capability,
            expires_at__gt=timezone.now(),
        )
        .first()
    )
    if result_set is None or (
        result_set.sub_event_id is not None and result_set.sub_event.is_archived
    ):
        raise Http404
    try:
        page, images = search_gallery_page(
            result_set=result_set,
            page_number=request.GET.get("page"),
            object_store=configured_object_store(),
        )
    except (ImproperlyConfigured, IngestionError):
        return _private_response(
            HttpResponse("Search results are temporarily unavailable.", status=503)
        )
    return _private_render(
        request,
        "openfotos_events/event.html",
        {
            "event": capability.event,
            "capability": capability,
            "page": page,
            "images": images,
            "sub_events": capability.event.sub_events.filter(is_archived=False),
            "selected_sub_event": result_set.sub_event,
            "search_form": FaceSearchForm(),
            "search_mode": True,
            "search_result": result_set,
            **_route_context(capability),
        },
    )


@require_POST
def portal_clear_search(request: HttpRequest, event_slug, result_id) -> HttpResponse:
    capability = _portal(request, event_slug)
    if not has_valid_access_cookie(request, capability):
        raise Http404
    delete_face_search(result_id=result_id, capability=capability)
    return _root_redirect(capability)


def _close_uploaded(request: HttpRequest) -> None:
    uploaded = request.FILES.get("reference_photo")
    if uploaded is not None:
        uploaded.close()
