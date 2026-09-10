"""Signed, event-scoped visitor authorization cookies."""

from math import ceil

from django.conf import settings
from django.core import signing
from django.http import HttpRequest, HttpResponse
from django.utils import timezone

from .models import Event

COOKIE_PREFIX = "openfotos_event_"
SIGNING_SALT = "openfotos.events.visitor-access.v1"


def visitor_cookie_name(event: Event) -> str:
    return f"{COOKIE_PREFIX}{event.id.hex}"


def visitor_cookie_path(event: Event) -> str:
    return f"/e/{event.public_token}/"


def visitor_cookie_ttl(event: Event) -> int:
    if event.expires_at is None:
        return 0
    until_expiry = ceil((event.expires_at - timezone.now()).total_seconds())
    return max(0, min(settings.EVENT_SESSION_TTL_SECONDS, until_expiry))


def has_valid_visitor_cookie(request: HttpRequest, event: Event) -> bool:
    signed_value = request.COOKIES.get(visitor_cookie_name(event))
    if not signed_value:
        return False
    try:
        payload = signing.loads(
            signed_value,
            salt=SIGNING_SALT,
            max_age=settings.EVENT_SESSION_TTL_SECONDS,
        )
    except signing.BadSignature:
        return False
    return payload == {
        "event": str(event.id),
        "photographer": str(event.photographer_id),
        "access_version": str(event.visitor_access_version),
    }


def set_visitor_cookie(response: HttpResponse, event: Event) -> None:
    ttl = visitor_cookie_ttl(event)
    if ttl <= 0:
        return
    signed_value = signing.dumps(
        {
            "event": str(event.id),
            "photographer": str(event.photographer_id),
            "access_version": str(event.visitor_access_version),
        },
        salt=SIGNING_SALT,
        compress=True,
    )
    response.set_cookie(
        visitor_cookie_name(event),
        signed_value,
        max_age=ttl,
        httponly=True,
        secure=not settings.DEBUG,
        samesite="Lax",
        path=visitor_cookie_path(event),
    )


def delete_visitor_cookie(response: HttpResponse, event: Event) -> None:
    response.delete_cookie(
        visitor_cookie_name(event),
        path=visitor_cookie_path(event),
        samesite="Lax",
    )
