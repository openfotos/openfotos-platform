"""Server-rendered photographer and private gallery flows."""

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from openfotos_contracts import EventState

from .audit import record_audit
from .cookies import delete_visitor_cookie, has_valid_visitor_cookie, set_visitor_cookie
from .forms import (
    BatchReassignmentForm,
    EventPinForm,
    GalleryExclusionForm,
    PhotographerLoginForm,
    SubEventForm,
)
from .gallery_services import (
    GALLERY_PAGE_SIZE,
    available_gallery_assets,
    exclude_from_gallery,
    gallery_page,
    gallery_photo,
    restore_to_gallery,
)
from .ingestion_services import IngestionError
from .models import (
    Asset,
    AuditAction,
    AuditResult,
    ContributionBatch,
    Event,
    PhotographerMembership,
    PreviewPolicy,
    RateLimitPurpose,
    SubEvent,
)
from .object_store import configured_object_store
from .rate_limits import clear_failures, rate_limit_status, register_failure
from .services import transition_event
from .sub_event_services import (
    create_sub_event,
    reassign_contribution,
    set_sub_event_archived,
    update_sub_event,
)

GENERIC_LOGIN_ERROR = "We could not sign you in with those details."
GENERIC_PIN_ERROR = "We could not unlock this event. Check the PIN and try again."
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


def _active_membership(request: HttpRequest, photographer):
    if not request.user.is_authenticated or not request.user.is_active:
        return None
    return PhotographerMembership.objects.filter(
        photographer=photographer,
        user=request.user,
        is_active=True,
    ).first()


def _throttled_response(request, template_name, form, retry_after_seconds):
    form.add_error(None, GENERIC_RATE_LIMIT_ERROR)
    response = _private_render(request, template_name, {"form": form}, status=429)
    response.headers["Retry-After"] = str(retry_after_seconds)
    return response


@require_http_methods(["GET", "POST"])
def photographer_login(request: HttpRequest) -> HttpResponse:
    photographer = _tenant(request)
    if _active_membership(request, photographer) is not None:
        return redirect("events:dashboard")

    form = PhotographerLoginForm(request.POST or None)
    if request.method == "GET":
        return _private_render(request, "openfotos_events/login.html", {"form": form})

    subject = f"{photographer.id}:{request.POST.get('username', '')}"
    current_limit = rate_limit_status(
        purpose=RateLimitPurpose.PHOTOGRAPHER_LOGIN,
        subject=subject,
        request=request,
    )
    if current_limit.limited:
        record_audit(
            photographer=photographer,
            action=AuditAction.PHOTOGRAPHER_LOGIN,
            result=AuditResult.RATE_LIMITED,
            request=request,
        )
        return _throttled_response(
            request,
            "openfotos_events/login.html",
            form,
            current_limit.retry_after_seconds,
        )

    user = None
    if form.is_valid():
        user = authenticate(
            request,
            username=form.cleaned_data["username"],
            password=form.cleaned_data["password"],
        )
    membership_exists = (
        user is not None
        and PhotographerMembership.objects.filter(
            photographer=photographer,
            user=user,
            is_active=True,
        ).exists()
    )
    if membership_exists:
        login(request, user)
        clear_failures(
            purpose=RateLimitPurpose.PHOTOGRAPHER_LOGIN,
            subject=subject,
            request=request,
        )
        record_audit(
            photographer=photographer,
            actor=user,
            action=AuditAction.PHOTOGRAPHER_LOGIN,
            result=AuditResult.SUCCEEDED,
            request=request,
        )
        return redirect("events:dashboard")

    failed_limit = register_failure(
        purpose=RateLimitPurpose.PHOTOGRAPHER_LOGIN,
        subject=subject,
        request=request,
    )
    result = AuditResult.RATE_LIMITED if failed_limit.limited else AuditResult.DENIED
    record_audit(
        photographer=photographer,
        action=AuditAction.PHOTOGRAPHER_LOGIN,
        result=result,
        request=request,
    )
    if failed_limit.limited:
        return _throttled_response(
            request,
            "openfotos_events/login.html",
            form,
            failed_limit.retry_after_seconds,
        )
    form.add_error(None, GENERIC_LOGIN_ERROR)
    return _private_render(request, "openfotos_events/login.html", {"form": form})


def dashboard(request: HttpRequest) -> HttpResponse:
    photographer = _tenant(request)
    if not request.user.is_authenticated:
        login_url = reverse("events:login")
        return redirect(f"{login_url}?next={request.path}")
    if _active_membership(request, photographer) is None:
        raise Http404
    events = Event.objects.filter(photographer=photographer)
    return _private_render(
        request,
        "openfotos_events/dashboard.html",
        {"photographer": photographer, "events": events},
    )


def _photographer_event(request: HttpRequest, event_id) -> Event:
    photographer = _tenant(request)
    if _active_membership(request, photographer) is None:
        raise Http404
    try:
        return Event.objects.select_related("photographer", "current_ingestion_manifest").get(
            pk=event_id,
            photographer=photographer,
        )
    except Event.DoesNotExist as exc:
        raise Http404 from exc


def _sub_event(event: Event, sub_event_id, *, include_archived: bool = False) -> SubEvent | None:
    if sub_event_id is None:
        return None
    query = event.sub_events.all()
    if not include_archived:
        query = query.filter(is_archived=False)
    try:
        return query.get(pk=sub_event_id)
    except SubEvent.DoesNotExist as exc:
        raise Http404 from exc


def _gallery_listing(
    event: Event, request: HttpRequest, *, sub_event: SubEvent | None = None
) -> tuple[object, list]:
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


@require_GET
def photographer_event(request: HttpRequest, event_id, sub_event_id=None) -> HttpResponse:
    event = _photographer_event(request, event_id)
    selected_sub_event = _sub_event(event, sub_event_id)
    page, images = _gallery_listing(event, request, sub_event=selected_sub_event)
    excluded_assets = Asset.objects.filter(
        batch__installation__event=event,
        gallery_excluded_at__isnull=False,
    ).order_by("original_filename", "id")
    failed_assets = Asset.objects.filter(
        batch__installation__event=event,
        derivative_failure_code__gt="",
        gallery_excluded_at__isnull=True,
    ).order_by("original_filename", "id")
    return _private_render(
        request,
        "openfotos_events/dashboard_event.html",
        {
            "event": event,
            "policy": PreviewPolicy.objects.filter(event=event).first(),
            "page": page,
            "images": images,
            "excluded_assets": excluded_assets,
            "failed_assets": failed_assets,
            "sub_events": event.sub_events.all(),
            "active_sub_events": event.sub_events.filter(is_archived=False),
            "selected_sub_event": selected_sub_event,
            "sub_event_form": SubEventForm(
                initial={"position": event.sub_events.count() + 1}
            ),
            "batches": ContributionBatch.objects.filter(
                installation__event=event
            ).select_related("sub_event", "installation"),
            "ready": event.derivatives_ready_generation == event.intake_generation,
        },
    )


@require_POST
def create_event_sub_event(request: HttpRequest, event_id) -> HttpResponse:
    event = _photographer_event(request, event_id)
    form = SubEventForm(request.POST)
    try:
        if not form.is_valid():
            raise IngestionError(
                "invalid_sub_event", "Enter a name and a positive display position."
            )
        create_sub_event(
            event=event,
            name=form.cleaned_data["name"],
            position=form.cleaned_data["position"],
            actor=request.user,
            request=request,
        )
    except IngestionError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Sub-event created.")
    return redirect("events:photographer-event", event_id=event.id)


@require_POST
def update_event_sub_event(request: HttpRequest, event_id, sub_event_id) -> HttpResponse:
    event = _photographer_event(request, event_id)
    sub_event = _sub_event(event, sub_event_id, include_archived=True)
    form = SubEventForm(request.POST)
    try:
        if not form.is_valid():
            raise IngestionError(
                "invalid_sub_event", "Enter a name and a positive display position."
            )
        update_sub_event(
            sub_event=sub_event,
            name=form.cleaned_data["name"],
            position=form.cleaned_data["position"],
            actor=request.user,
            request=request,
        )
    except IngestionError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Sub-event updated.")
    return redirect("events:photographer-event", event_id=event.id)


@require_POST
def archive_event_sub_event(request: HttpRequest, event_id, sub_event_id) -> HttpResponse:
    event = _photographer_event(request, event_id)
    sub_event = _sub_event(event, sub_event_id, include_archived=True)
    archived = request.POST.get("action") == "archive"
    try:
        set_sub_event_archived(
            sub_event=sub_event,
            archived=archived,
            actor=request.user,
            request=request,
        )
    except IngestionError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Sub-event archived." if archived else "Sub-event restored.")
    return redirect("events:photographer-event", event_id=event.id)


@require_POST
def reassign_event_batch(request: HttpRequest, event_id, batch_id) -> HttpResponse:
    event = _photographer_event(request, event_id)
    form = BatchReassignmentForm(request.POST)
    try:
        if not form.is_valid():
            raise IngestionError("sub_event_not_found", "Select an active sub-event.")
        reassign_contribution(
            event=event,
            batch_id=batch_id,
            sub_event_id=form.cleaned_data["sub_event_id"],
            actor=request.user,
            request=request,
        )
    except IngestionError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Contribution moved to the selected sub-event.")
    return redirect("events:photographer-event", event_id=event.id)


@require_POST
def publish_event(request: HttpRequest, event_id) -> HttpResponse:
    event = _photographer_event(request, event_id)
    try:
        transition_event(
            event_id=event.id,
            target=EventState.PUBLISHED,
            actor=request.user,
            request=request,
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, "The private gallery is published.")
    return redirect("events:photographer-event", event_id=event.id)


@require_POST
def unpublish_event(request: HttpRequest, event_id) -> HttpResponse:
    event = _photographer_event(request, event_id)
    try:
        transition_event(
            event_id=event.id,
            target=EventState.REVIEW,
            actor=request.user,
            request=request,
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(
            request,
            "The gallery returned to Review and visitor sessions were revoked.",
        )
    return redirect("events:photographer-event", event_id=event.id)


@require_POST
def exclude_gallery_asset(request: HttpRequest, event_id, asset_id) -> HttpResponse:
    event = _photographer_event(request, event_id)
    form = GalleryExclusionForm(request.POST)
    try:
        if not form.is_valid():
            raise IngestionError("invalid_exclusion_reason", "An exclusion reason is required.")
        exclude_from_gallery(
            event=event,
            asset_id=asset_id,
            actor=request.user,
            reason=form.cleaned_data["reason"],
            request=request,
        )
    except IngestionError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "The failed photo was excluded from publication.")
    return redirect("events:photographer-event", event_id=event.id)


@require_POST
def restore_gallery_asset(request: HttpRequest, event_id, asset_id) -> HttpResponse:
    event = _photographer_event(request, event_id)
    try:
        restore_to_gallery(
            event=event,
            asset_id=asset_id,
            actor=request.user,
            request=request,
        )
    except IngestionError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "The photo was restored to derivative processing.")
    return redirect("events:photographer-event", event_id=event.id)


@require_GET
def photographer_photo(
    request: HttpRequest, event_id, asset_id, sub_event_id=None
) -> HttpResponse:
    event = _photographer_event(request, event_id)
    selected_sub_event = _sub_event(event, sub_event_id)
    try:
        image, previous_asset, next_asset = gallery_photo(
            event=event,
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
            "event": event,
            "image": image,
            "previous_asset": previous_asset,
            "next_asset": next_asset,
            "dashboard_mode": True,
            "selected_sub_event": selected_sub_event,
        },
    )


@require_POST
def photographer_logout(request: HttpRequest) -> HttpResponse:
    photographer = _tenant(request)
    membership = _active_membership(request, photographer)
    if membership is None:
        raise Http404
    actor = request.user
    record_audit(
        photographer=photographer,
        actor=actor,
        action=AuditAction.PHOTOGRAPHER_LOGOUT,
        result=AuditResult.SUCCEEDED,
        request=request,
    )
    logout(request)
    return redirect("events:login")


def _available_event(request: HttpRequest, token: str) -> Event:
    photographer = _tenant(request)
    try:
        event = Event.objects.select_related("photographer").get(
            photographer=photographer,
            public_token=token,
        )
    except Event.DoesNotExist as exc:
        raise Http404 from exc
    if not event.is_publicly_available():
        raise Http404
    return event


@require_http_methods(["GET", "POST"])
def event_access(request: HttpRequest, token: str, sub_event_id=None) -> HttpResponse:
    event = _available_event(request, token)
    selected_sub_event = _sub_event(event, sub_event_id)
    if request.method == "GET" and has_valid_visitor_cookie(request, event):
        page, images = _gallery_listing(event, request, sub_event=selected_sub_event)
        return _private_render(
            request,
            "openfotos_events/event.html",
            {
                "event": event,
                "page": page,
                "images": images,
                "sub_events": event.sub_events.filter(is_archived=False),
                "selected_sub_event": selected_sub_event,
            },
        )

    form = EventPinForm(request.POST or None)
    if request.method == "GET":
        response = _private_render(request, "openfotos_events/unlock.html", {"form": form})
        if request.COOKIES.get(f"openfotos_event_{event.id.hex}"):
            delete_visitor_cookie(response, event)
        return response

    subject = str(event.id)
    current_limit = rate_limit_status(
        purpose=RateLimitPurpose.EVENT_PIN,
        subject=subject,
        request=request,
    )
    if current_limit.limited:
        record_audit(
            photographer=event.photographer,
            event=event,
            action=AuditAction.EVENT_PIN_UNLOCK,
            result=AuditResult.RATE_LIMITED,
            request=request,
        )
        return _throttled_response(
            request,
            "openfotos_events/unlock.html",
            form,
            current_limit.retry_after_seconds,
        )

    pin_is_valid = form.is_valid() and event.check_pin(form.cleaned_data["pin"])
    if pin_is_valid:
        clear_failures(
            purpose=RateLimitPurpose.EVENT_PIN,
            subject=subject,
            request=request,
        )
        record_audit(
            photographer=event.photographer,
            event=event,
            action=AuditAction.EVENT_PIN_UNLOCK,
            result=AuditResult.SUCCEEDED,
            request=request,
        )
        if selected_sub_event is None:
            response = redirect("events:event-access", token=event.public_token)
        else:
            response = redirect(
                "events:visitor-sub-event",
                token=event.public_token,
                sub_event_id=selected_sub_event.id,
            )
        set_visitor_cookie(response, event)
        return response

    failed_limit = register_failure(
        purpose=RateLimitPurpose.EVENT_PIN,
        subject=subject,
        request=request,
    )
    result = AuditResult.RATE_LIMITED if failed_limit.limited else AuditResult.DENIED
    record_audit(
        photographer=event.photographer,
        event=event,
        action=AuditAction.EVENT_PIN_UNLOCK,
        result=result,
        request=request,
    )
    if failed_limit.limited:
        return _throttled_response(
            request,
            "openfotos_events/unlock.html",
            form,
            failed_limit.retry_after_seconds,
        )
    form.add_error(None, GENERIC_PIN_ERROR)
    return _private_render(request, "openfotos_events/unlock.html", {"form": form})


@require_GET
def visitor_photo(
    request: HttpRequest, token: str, asset_id, sub_event_id=None
) -> HttpResponse:
    event = _available_event(request, token)
    selected_sub_event = _sub_event(event, sub_event_id)
    if not has_valid_visitor_cookie(request, event):
        raise Http404
    try:
        image, previous_asset, next_asset = gallery_photo(
            event=event,
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
            "event": event,
            "image": image,
            "previous_asset": previous_asset,
            "next_asset": next_asset,
            "dashboard_mode": False,
            "selected_sub_event": selected_sub_event,
        },
    )
