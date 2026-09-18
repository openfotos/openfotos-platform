"""Owner and guest capability flows for private gallery delivery."""

from __future__ import annotations

from uuid import UUID

from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST

from openfotos_vision import FaceEngineError

from .audit import record_audit
from .cookies import (
    access_cookie_name,
    delete_presented_cookie,
    delete_share_cookies,
    has_valid_access_cookie,
    has_valid_presented_cookie,
    set_access_cookie,
    set_presented_cookie,
)
from .face_search_engine import configured_face_search_engine
from .face_search_services import (
    FaceSearchError,
    create_face_search,
    delete_face_search,
)
from .forms import FaceSearchForm, GuestCapabilityForm, SharePinForm
from .gallery_services import (
    GALLERY_PAGE_SIZE,
    available_gallery_assets,
    gallery_page,
    gallery_photo,
    original_download,
    search_gallery_page,
)
from .ingestion_services import IngestionError
from .models import (
    AuditAction,
    AuditResult,
    FaceSearchResultSet,
    GuestCapability,
    OwnerCapability,
    RateLimitPurpose,
    SubEvent,
)
from .object_store import configured_object_store
from .rate_limits import clear_failures, consume_attempt, rate_limit_status, register_failure
from .sharing_services import (
    ShareAccessError,
    create_guest_capability,
    guest_for_tenant,
    owner_for_tenant,
    revoke_guest_capability,
    secret_matches,
)

GENERIC_PIN_ERROR = "We could not unlock this gallery. Check the PIN and try again."
GENERIC_RATE_LIMIT_ERROR = "Too many attempts. Please wait before trying again."


def _private_response(response: HttpResponse) -> HttpResponse:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


def _private_render(request, template_name, context=None, *, status=None) -> HttpResponse:
    return _private_response(render(request, template_name, context, status=status))


def _tenant(request: HttpRequest):
    photographer = getattr(request, "photographer", None)
    if photographer is None:
        raise Http404
    return photographer


def _owner(request: HttpRequest, capability_id: UUID) -> OwnerCapability:
    try:
        return owner_for_tenant(photographer=_tenant(request), capability_id=capability_id)
    except ShareAccessError as exc:
        raise Http404 from exc


def _guest(request: HttpRequest, capability_id: UUID) -> GuestCapability:
    try:
        return guest_for_tenant(photographer=_tenant(request), capability_id=capability_id)
    except ShareAccessError as exc:
        raise Http404 from exc


def _event(capability: OwnerCapability | GuestCapability):
    return capability.event if isinstance(capability, OwnerCapability) else capability.owner.event


def _scope(
    capability: OwnerCapability | GuestCapability,
    requested_sub_event_id: UUID | None,
) -> SubEvent | None:
    event = _event(capability)
    if isinstance(capability, GuestCapability) and capability.sub_event_id is not None:
        if requested_sub_event_id not in (None, capability.sub_event_id):
            raise Http404
        return capability.sub_event
    if requested_sub_event_id is None:
        return None
    try:
        return event.sub_events.get(pk=requested_sub_event_id, is_archived=False)
    except SubEvent.DoesNotExist as exc:
        raise Http404 from exc


def _route(kind: str, suffix: str) -> str:
    return f"events:{kind}-{suffix}"


def _root_redirect(capability: OwnerCapability | GuestCapability) -> HttpResponse:
    kind = "owner" if isinstance(capability, OwnerCapability) else "guest"
    return redirect(_route(kind, "gallery"), capability_id=capability.id)


def _gallery_listing(event, request, *, sub_event=None):
    query = available_gallery_assets(event, sub_event=sub_event)
    if not query.exists():
        return Paginator(query, GALLERY_PAGE_SIZE).get_page(request.GET.get("page")), []
    try:
        return gallery_page(
            event=event,
            page_number=request.GET.get("page"),
            object_store=configured_object_store(),
            sub_event=sub_event,
        )
    except (ImproperlyConfigured, IngestionError):
        messages.error(request, "Gallery media is temporarily unavailable. Please try again.")
        return Paginator(query.none(), GALLERY_PAGE_SIZE).get_page(1), []


def _render_gallery(
    request: HttpRequest,
    capability: OwnerCapability | GuestCapability,
    *,
    selected_sub_event: SubEvent | None,
    search_form: FaceSearchForm | None = None,
    status: int | None = None,
) -> HttpResponse:
    event = _event(capability)
    page, images = _gallery_listing(event, request, sub_event=selected_sub_event)
    kind = "owner" if isinstance(capability, OwnerCapability) else "guest"
    return _private_render(
        request,
        "openfotos_events/event.html",
        {
            "event": event,
            "capability": capability,
            "share_kind": kind,
            "page": page,
            "images": images,
            "sub_events": event.sub_events.filter(is_archived=False),
            "selected_sub_event": selected_sub_event,
            "search_form": search_form or FaceSearchForm(),
            "guest_form": (
                GuestCapabilityForm(event=event)
                if isinstance(capability, OwnerCapability)
                else None
            ),
            "guest_capabilities": (
                capability.guest_capabilities.select_related("sub_event").all()
                if isinstance(capability, OwnerCapability)
                else None
            ),
        },
        status=status,
    )


def owner_gallery(request: HttpRequest, capability_id, sub_event_id=None) -> HttpResponse:
    return _gallery_access(request, _owner(request, capability_id), sub_event_id=sub_event_id)


def guest_gallery(request: HttpRequest, capability_id, sub_event_id=None) -> HttpResponse:
    return _gallery_access(request, _guest(request, capability_id), sub_event_id=sub_event_id)


@require_GET
def _gallery_access(request, capability, *, sub_event_id=None):
    if has_valid_access_cookie(request, capability):
        return _render_gallery(
            request,
            capability,
            selected_sub_event=_scope(capability, sub_event_id),
        )
    if has_valid_presented_cookie(request, capability):
        return _private_render(
            request,
            "openfotos_events/unlock.html",
            {
                "form": SharePinForm(),
                "unlock_url": reverse(
                    _route(
                        "owner" if isinstance(capability, OwnerCapability) else "guest",
                        "unlock",
                    ),
                    args=(capability.id,),
                ),
            },
        )
    response = _private_render(
        request,
        "openfotos_events/share_landing.html",
        {
            "present_url": reverse(
                _route(
                    "owner" if isinstance(capability, OwnerCapability) else "guest",
                    "present",
                ),
                args=(capability.id,),
            )
        },
    )
    delete_share_cookies(response, capability)
    return response


@sensitive_post_parameters("secret")
@require_POST
def owner_present(request: HttpRequest, capability_id) -> HttpResponse:
    return _present_secret(request, _owner(request, capability_id))


@sensitive_post_parameters("secret")
@require_POST
def guest_present(request: HttpRequest, capability_id) -> HttpResponse:
    return _present_secret(request, _guest(request, capability_id))


def _present_secret(request: HttpRequest, capability) -> HttpResponse:
    secret = request.POST.get("secret", "")
    if not secret_matches(capability, secret):
        return _private_render(
            request,
            "openfotos_events/share_unavailable.html",
            status=404,
        )
    response = _root_redirect(capability)
    set_presented_cookie(response, capability)
    return _private_response(response)


@sensitive_post_parameters("pin")
@require_POST
def owner_unlock(request: HttpRequest, capability_id) -> HttpResponse:
    return _unlock(request, _owner(request, capability_id))


@sensitive_post_parameters("pin")
@require_POST
def guest_unlock(request: HttpRequest, capability_id) -> HttpResponse:
    return _unlock(request, _guest(request, capability_id))


def _unlock(request: HttpRequest, capability) -> HttpResponse:
    if not has_valid_presented_cookie(request, capability):
        raise Http404
    is_owner = isinstance(capability, OwnerCapability)
    purpose = RateLimitPurpose.OWNER_PIN if is_owner else RateLimitPurpose.GUEST_PIN
    action = AuditAction.OWNER_PIN_UNLOCK if is_owner else AuditAction.GUEST_PIN_UNLOCK
    form = SharePinForm(request.POST)
    subject = str(capability.id)
    current_limit = rate_limit_status(purpose=purpose, subject=subject, request=request)
    if current_limit.limited:
        record_audit(
            photographer=_event(capability).photographer,
            event=_event(capability),
            action=action,
            result=AuditResult.RATE_LIMITED,
            request=request,
            metadata={"capability_id": str(capability.id)},
        )
        return _pin_response(request, capability, form, current_limit.retry_after_seconds)
    if form.is_valid() and capability.check_pin(form.cleaned_data["pin"]):
        clear_failures(purpose=purpose, subject=subject, request=request)
        record_audit(
            photographer=_event(capability).photographer,
            event=_event(capability),
            action=action,
            result=AuditResult.SUCCEEDED,
            request=request,
            metadata={"capability_id": str(capability.id)},
        )
        response = _root_redirect(capability)
        set_access_cookie(response, capability)
        delete_presented_cookie(response, capability)
        return _private_response(response)
    failed_limit = register_failure(purpose=purpose, subject=subject, request=request)
    record_audit(
        photographer=_event(capability).photographer,
        event=_event(capability),
        action=action,
        result=AuditResult.RATE_LIMITED if failed_limit.limited else AuditResult.DENIED,
        request=request,
        metadata={"capability_id": str(capability.id)},
    )
    if failed_limit.limited:
        return _pin_response(request, capability, form, failed_limit.retry_after_seconds)
    form.add_error(None, GENERIC_PIN_ERROR)
    return _private_render(
        request,
        "openfotos_events/unlock.html",
        {"form": form, "unlock_url": request.path},
    )


def _pin_response(request, capability, form, retry_after_seconds):
    form.add_error(None, GENERIC_RATE_LIMIT_ERROR)
    response = _private_render(
        request,
        "openfotos_events/unlock.html",
        {"form": form, "unlock_url": request.path},
        status=429,
    )
    response.headers["Retry-After"] = str(retry_after_seconds)
    return response


@require_GET
def owner_photo(request: HttpRequest, capability_id, asset_id, sub_event_id=None) -> HttpResponse:
    return _photo(
        request,
        _owner(request, capability_id),
        asset_id=asset_id,
        sub_event_id=sub_event_id,
    )


@require_GET
def guest_photo(request: HttpRequest, capability_id, asset_id, sub_event_id=None) -> HttpResponse:
    return _photo(
        request,
        _guest(request, capability_id),
        asset_id=asset_id,
        sub_event_id=sub_event_id,
    )


def _photo(request, capability, *, asset_id, sub_event_id=None):
    if not has_valid_access_cookie(request, capability):
        raise Http404
    selected_sub_event = _scope(capability, sub_event_id)
    try:
        image, previous_asset, next_asset = gallery_photo(
            event=_event(capability),
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
            "event": _event(capability),
            "capability": capability,
            "share_kind": "owner" if isinstance(capability, OwnerCapability) else "guest",
            "image": image,
            "previous_asset": previous_asset,
            "next_asset": next_asset,
            "dashboard_mode": False,
            "selected_sub_event": selected_sub_event,
        },
    )


@require_GET
def owner_download(
    request: HttpRequest, capability_id, asset_id, sub_event_id=None
) -> HttpResponse:
    return _download(
        request,
        _owner(request, capability_id),
        asset_id=asset_id,
        sub_event_id=sub_event_id,
    )


@require_GET
def guest_download(
    request: HttpRequest, capability_id, asset_id, sub_event_id=None
) -> HttpResponse:
    return _download(
        request,
        _guest(request, capability_id),
        asset_id=asset_id,
        sub_event_id=sub_event_id,
    )


def _download(request, capability, *, asset_id, sub_event_id=None):
    if not has_valid_access_cookie(request, capability):
        raise Http404
    selected_sub_event = _scope(capability, sub_event_id)
    try:
        download = original_download(
            event=_event(capability),
            asset_id=asset_id,
            object_store=configured_object_store(),
            sub_event=selected_sub_event,
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
        photographer=_event(capability).photographer,
        event=_event(capability),
        action=AuditAction.ORIGINAL_DOWNLOAD_ISSUED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={
            "asset_id": str(download.asset.id),
            "capability_id": str(capability.id),
            "sha256": download.sha256,
        },
    )
    return _private_response(redirect(download.url))


@sensitive_post_parameters("label")
@require_POST
def owner_create_guest(request: HttpRequest, capability_id) -> HttpResponse:
    owner = _owner(request, capability_id)
    if not has_valid_access_cookie(request, owner):
        raise Http404
    form = GuestCapabilityForm(request.POST, event=owner.event)
    if not form.is_valid():
        messages.error(request, "Choose a valid gallery scope and optional label.")
        return _root_redirect(owner)
    raw_sub_event_id = form.cleaned_data["sub_event_id"]
    try:
        issued = create_guest_capability(
            owner=owner,
            label=form.cleaned_data["label"],
            sub_event_id=UUID(raw_sub_event_id) if raw_sub_event_id else None,
            request=request,
        )
    except ShareAccessError as exc:
        messages.error(request, str(exc))
        return _root_redirect(owner)
    path = reverse("events:guest-gallery", args=(issued.capability.id,))
    share_url = f"{request.build_absolute_uri(path)}#secret={issued.secret}"
    return _private_render(
        request,
        "openfotos_events/credential_reveal.html",
        {
            "event": owner.event,
            "share_url": share_url,
            "pin": issued.pin,
            "capability_label": "Guest",
            "expires_at": issued.capability.expires_at,
            "return_url": reverse("events:owner-gallery", args=(owner.id,)),
        },
    )


@require_POST
def owner_revoke_guest(request: HttpRequest, capability_id, guest_id) -> HttpResponse:
    owner = _owner(request, capability_id)
    if not has_valid_access_cookie(request, owner):
        raise Http404
    try:
        revoke_guest_capability(owner=owner, guest_id=guest_id, request=request)
    except ShareAccessError as exc:
        raise Http404 from exc
    messages.success(request, "Guest link revoked.")
    return _root_redirect(owner)


@sensitive_post_parameters("reference_photo")
@require_POST
def owner_search(request: HttpRequest, capability_id, sub_event_id=None) -> HttpResponse:
    return _search(
        request,
        _owner(request, capability_id),
        sub_event_id=sub_event_id,
    )


@sensitive_post_parameters("reference_photo")
@require_POST
def guest_search(request: HttpRequest, capability_id, sub_event_id=None) -> HttpResponse:
    return _search(
        request,
        _guest(request, capability_id),
        sub_event_id=sub_event_id,
    )


def _search(request, capability, *, sub_event_id=None):
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
        record_audit(
            photographer=_event(capability).photographer,
            event=_event(capability),
            action=AuditAction.FACE_SEARCH_REJECTED,
            result=AuditResult.RATE_LIMITED,
            request=request,
            metadata={"capability_id": str(capability.id), "code": "rate_limited"},
        )
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
        engine = configured_face_search_engine()
        result_set = create_face_search(
            capability=capability,
            sub_event=selected_sub_event,
            uploaded_photo=form.cleaned_data["reference_photo"],
            engine=engine,
        )
    except FaceSearchError as exc:
        record_audit(
            photographer=_event(capability).photographer,
            event=_event(capability),
            action=AuditAction.FACE_SEARCH_REJECTED,
            result=AuditResult.DENIED,
            request=request,
            metadata={"capability_id": str(capability.id), "code": exc.code},
        )
        form.add_error("reference_photo", str(exc))
        return _render_gallery(
            request,
            capability,
            selected_sub_event=selected_sub_event,
            search_form=form,
            status=400,
        )
    except (FaceEngineError, ImproperlyConfigured):
        record_audit(
            photographer=_event(capability).photographer,
            event=_event(capability),
            action=AuditAction.FACE_SEARCH_REJECTED,
            result=AuditResult.DENIED,
            request=request,
            metadata={
                "capability_id": str(capability.id),
                "code": "service_unavailable",
            },
        )
        return _private_response(
            HttpResponse("Face search is temporarily unavailable.", status=503)
        )
    finally:
        _close_uploaded(request)
    record_audit(
        photographer=_event(capability).photographer,
        event=_event(capability),
        action=AuditAction.FACE_SEARCH_COMPLETED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={
            "capability_id": str(capability.id),
            "result_count": len(result_set.ordered_asset_ids),
            "sub_event_id": str(selected_sub_event.id) if selected_sub_event else None,
        },
    )
    kind = "owner" if isinstance(capability, OwnerCapability) else "guest"
    return redirect(
        _route(kind, "search-results"),
        capability_id=capability.id,
        result_id=result_set.id,
    )


@require_GET
def owner_search_results(request: HttpRequest, capability_id, result_id) -> HttpResponse:
    return _search_results(request, _owner(request, capability_id), result_id=result_id)


@require_GET
def guest_search_results(request: HttpRequest, capability_id, result_id) -> HttpResponse:
    return _search_results(request, _guest(request, capability_id), result_id=result_id)


def _search_results(request, capability, *, result_id):
    if not has_valid_access_cookie(request, capability):
        raise Http404
    query = FaceSearchResultSet.objects.select_related("event", "sub_event").filter(
        pk=result_id,
        event=_event(capability),
        expires_at__gt=timezone.now(),
    )
    if isinstance(capability, OwnerCapability):
        query = query.filter(owner_capability=capability, guest_capability__isnull=True)
    else:
        query = query.filter(guest_capability=capability, owner_capability__isnull=True)
    result_set = query.first()
    if result_set is None:
        raise Http404
    if result_set.sub_event_id is not None and result_set.sub_event.is_archived:
        raise Http404
    if (
        isinstance(capability, GuestCapability)
        and capability.sub_event_id is not None
        and result_set.sub_event_id != capability.sub_event_id
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
            "event": _event(capability),
            "capability": capability,
            "share_kind": "owner" if isinstance(capability, OwnerCapability) else "guest",
            "page": page,
            "images": images,
            "sub_events": _event(capability).sub_events.filter(is_archived=False),
            "selected_sub_event": result_set.sub_event,
            "search_form": FaceSearchForm(),
            "search_mode": True,
            "search_result": result_set,
        },
    )


@require_POST
def owner_clear_search(request: HttpRequest, capability_id, result_id) -> HttpResponse:
    return _clear_search(request, _owner(request, capability_id), result_id=result_id)


@require_POST
def guest_clear_search(request: HttpRequest, capability_id, result_id) -> HttpResponse:
    return _clear_search(request, _guest(request, capability_id), result_id=result_id)


def _clear_search(request, capability, *, result_id):
    if not has_valid_access_cookie(request, capability):
        raise Http404
    delete_face_search(result_id=result_id, capability=capability)
    return _root_redirect(capability)


def _close_uploaded(request: HttpRequest) -> None:
    uploaded = request.FILES.get("reference_photo")
    if uploaded is not None:
        uploaded.close()
