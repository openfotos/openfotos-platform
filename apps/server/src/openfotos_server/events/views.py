"""Server-rendered photographer and visitor authorization flows."""

from django.contrib.auth import authenticate, login, logout
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from .audit import record_audit
from .cookies import delete_visitor_cookie, has_valid_visitor_cookie, set_visitor_cookie
from .forms import EventPinForm, PhotographerLoginForm
from .models import (
    AuditAction,
    AuditResult,
    Event,
    PhotographerMembership,
    RateLimitPurpose,
)
from .rate_limits import clear_failures, rate_limit_status, register_failure

GENERIC_LOGIN_ERROR = "We could not sign you in with those details."
GENERIC_PIN_ERROR = "We could not unlock this event. Check the PIN and try again."
GENERIC_RATE_LIMIT_ERROR = "Too many attempts. Please wait before trying again."


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
    response = render(request, template_name, {"form": form}, status=429)
    response.headers["Retry-After"] = str(retry_after_seconds)
    return response


@require_http_methods(["GET", "POST"])
def photographer_login(request: HttpRequest) -> HttpResponse:
    photographer = _tenant(request)
    if _active_membership(request, photographer) is not None:
        return redirect("events:dashboard")

    form = PhotographerLoginForm(request.POST or None)
    if request.method == "GET":
        return render(request, "openfotos_events/login.html", {"form": form})

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
    return render(request, "openfotos_events/login.html", {"form": form})


def dashboard(request: HttpRequest) -> HttpResponse:
    photographer = _tenant(request)
    if not request.user.is_authenticated:
        login_url = reverse("events:login")
        return redirect(f"{login_url}?next={request.path}")
    if _active_membership(request, photographer) is None:
        raise Http404
    events = Event.objects.filter(photographer=photographer)
    return render(
        request,
        "openfotos_events/dashboard.html",
        {"photographer": photographer, "events": events},
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
def event_access(request: HttpRequest, token: str) -> HttpResponse:
    event = _available_event(request, token)
    if request.method == "GET" and has_valid_visitor_cookie(request, event):
        return render(request, "openfotos_events/event.html", {"event": event})

    form = EventPinForm(request.POST or None)
    if request.method == "GET":
        response = render(request, "openfotos_events/unlock.html", {"form": form})
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
        response = redirect("events:event-access", token=event.public_token)
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
    return render(request, "openfotos_events/unlock.html", {"form": form})
