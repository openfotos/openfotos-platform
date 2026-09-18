"""Server-rendered photographer and private gallery flows."""

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from openfotos_contracts import AssetVariant, EventState, UploadObjectState

from .audit import record_audit
from .face_services import reset_face_analysis
from .forms import (
    BatchReassignmentForm,
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
    original_download,
    restore_to_gallery,
)
from .ingestion_services import IngestionError
from .models import (
    Asset,
    AuditAction,
    AuditResult,
    ContributionBatch,
    Event,
    FaceAnalysis,
    FaceAnalysisState,
    GuestCapability,
    OwnerCapability,
    PhotographerMembership,
    PreviewPolicy,
    RateLimitPurpose,
    SubEvent,
)
from .object_store import configured_object_store
from .rate_limits import clear_failures, rate_limit_status, register_failure
from .services import transition_event
from .sharing_services import (
    ShareAccessError,
    issue_owner_capability,
    owner_is_available,
    revoke_guest_capability,
    revoke_owner_capability,
)
from .sub_event_services import (
    create_sub_event,
    reassign_contribution,
    set_sub_event_archived,
    update_sub_event,
)

GENERIC_LOGIN_ERROR = "We could not sign you in with those details."
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
    visible_face_assets = Asset.objects.filter(
        batch__installation__event=event,
        batch__sub_event__is_archived=False,
        variant_objects__variant=AssetVariant.ORIGINAL.value,
        variant_objects__state=UploadObjectState.VERIFIED.value,
        gallery_excluded_at__isnull=True,
    ).distinct()
    face_analyses = FaceAnalysis.objects.filter(asset__in=visible_face_assets)
    face_total_count = visible_face_assets.count()
    indexed_count = face_analyses.filter(state=FaceAnalysisState.INDEXED).count()
    no_face_count = face_analyses.filter(state=FaceAnalysisState.NO_USABLE_FACE).count()
    failed_face_count = face_analyses.filter(
        state__in=(FaceAnalysisState.FAILED, FaceAnalysisState.CONFLICT)
    ).count()
    face_failed_assets = (
        visible_face_assets.filter(
            face_analysis__state__in=(FaceAnalysisState.FAILED, FaceAnalysisState.CONFLICT)
        )
        .select_related("face_analysis")
        .order_by("id")
    )
    gallery_ready = event.derivatives_ready_generation == event.intake_generation
    face_ready = event.face_index_ready_generation == event.intake_generation
    owner_capability = OwnerCapability.objects.filter(event=event).first()
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
            "face_failed_assets": face_failed_assets,
            "face_total_count": face_total_count,
            "face_indexed_count": indexed_count,
            "face_no_usable_count": no_face_count,
            "face_failed_count": failed_face_count,
            "face_pending_count": max(
                0,
                face_total_count - indexed_count - no_face_count - failed_face_count,
            ),
            "sub_events": event.sub_events.all(),
            "active_sub_events": event.sub_events.filter(is_archived=False),
            "selected_sub_event": selected_sub_event,
            "sub_event_form": SubEventForm(initial={"position": event.sub_events.count() + 1}),
            "batches": ContributionBatch.objects.filter(installation__event=event).select_related(
                "sub_event", "installation"
            ),
            "gallery_ready": gallery_ready,
            "face_ready": face_ready,
            "ready": gallery_ready and face_ready,
            "owner_capability": owner_capability,
            "owner_capability_active": (
                owner_is_available(owner_capability) if owner_capability else False
            ),
            "guest_capabilities": (
                owner_capability.guest_capabilities.select_related("sub_event").all()
                if owner_capability
                else GuestCapability.objects.none()
            ),
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
    try:
        action = request.POST.get("action")
        if action not in {"archive", "restore"}:
            raise IngestionError(
                "invalid_sub_event_action",
                "Choose whether to archive or restore the sub-event.",
            )
        archived = action == "archive"
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
            "The gallery returned to Review and owner/guest sessions were invalidated.",
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


@require_POST
def reset_asset_face_analysis(request: HttpRequest, event_id, asset_id) -> HttpResponse:
    event = _photographer_event(request, event_id)
    try:
        reset_face_analysis(
            event=event,
            asset_id=asset_id,
            actor=request.user,
            request=request,
        )
    except IngestionError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Face analysis reset. Retry this photo on its workstation.")
    return redirect("events:photographer-event", event_id=event.id)


@require_GET
def photographer_photo(request: HttpRequest, event_id, asset_id, sub_event_id=None) -> HttpResponse:
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


@require_GET
def photographer_download(
    request: HttpRequest, event_id, asset_id, sub_event_id=None
) -> HttpResponse:
    event = _photographer_event(request, event_id)
    selected_sub_event = _sub_event(event, sub_event_id)
    try:
        download = original_download(
            event=event,
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
        photographer=event.photographer,
        event=event,
        actor=request.user,
        action=AuditAction.ORIGINAL_DOWNLOAD_ISSUED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"asset_id": str(download.asset.id), "sha256": download.sha256},
    )
    return _private_response(redirect(download.url))


@require_POST
def issue_event_owner_capability(request: HttpRequest, event_id) -> HttpResponse:
    event = _photographer_event(request, event_id)
    try:
        issued = issue_owner_capability(event=event, actor=request.user, request=request)
    except ShareAccessError as exc:
        messages.error(request, str(exc))
        return redirect("events:photographer-event", event_id=event.id)
    path = reverse("events:owner-gallery", args=(issued.capability.id,))
    share_url = f"{request.build_absolute_uri(path)}#secret={issued.secret}"
    return _private_render(
        request,
        "openfotos_events/credential_reveal.html",
        {
            "event": event,
            "share_url": share_url,
            "pin": issued.pin,
            "capability_label": "Owner",
            "expires_at": issued.capability.expires_at,
            "return_url": reverse("events:photographer-event", args=(event.id,)),
        },
    )


@require_POST
def revoke_event_owner_capability(request: HttpRequest, event_id) -> HttpResponse:
    event = _photographer_event(request, event_id)
    try:
        revoke_owner_capability(event=event, actor=request.user, request=request)
    except ShareAccessError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Owner access and every guest link were revoked.")
    return redirect("events:photographer-event", event_id=event.id)


@require_POST
def revoke_event_guest_capability(request: HttpRequest, event_id, guest_id) -> HttpResponse:
    event = _photographer_event(request, event_id)
    try:
        owner = OwnerCapability.objects.get(event=event)
        revoke_guest_capability(
            owner=owner,
            guest_id=guest_id,
            actor=request.user,
            request=request,
        )
    except (OwnerCapability.DoesNotExist, ShareAccessError):
        raise Http404 from None
    messages.success(request, "Guest link revoked.")
    return redirect("events:photographer-event", event_id=event.id)


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
