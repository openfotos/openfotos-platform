"""Signed, event-scoped visitor authorization cookies."""

from math import ceil

from django.conf import settings
from django.core import signing
from django.http import HttpRequest, HttpResponse
from django.utils import timezone

from .models import PortalCapability

ACCESS_COOKIE_PREFIX = "openfotos_event_"


def capability_cookie_path(capability: PortalCapability) -> str:
    return f"/portfolio/events/{capability.event.slug}/"


def access_cookie_name(capability: PortalCapability) -> str:
    return f"{ACCESS_COOKIE_PREFIX}{capability.id.hex}"


def _session_ttl(capability: PortalCapability) -> int:
    remaining = min(
        ceil((expiry - timezone.now()).total_seconds())
        for expiry in (capability.expires_at, capability.event.expires_at)
    )
    return max(0, min(settings.PORTAL_SESSION_TTL_SECONDS, remaining))


def _payload(capability: PortalCapability) -> dict[str, str]:
    return {
        "capability": str(capability.id),
        "event": str(capability.event_id),
        "access_version": str(capability.access_version),
        "event_access_version": str(capability.event.share_access_version),
    }


def has_valid_access_cookie(request: HttpRequest, capability: PortalCapability) -> bool:
    signed_value = request.COOKIES.get(access_cookie_name(capability))
    if not signed_value:
        return False
    try:
        payload = signing.loads(
            signed_value,
            salt="openfotos.event.access.v1",
            max_age=settings.PORTAL_SESSION_TTL_SECONDS,
        )
    except signing.BadSignature:
        return False
    return payload == _payload(capability)


def set_access_cookie(response: HttpResponse, capability: PortalCapability) -> None:
    ttl = _session_ttl(capability)
    if ttl <= 0:
        return
    response.set_cookie(
        access_cookie_name(capability),
        signing.dumps(_payload(capability), salt="openfotos.event.access.v1", compress=True),
        max_age=ttl,
        secure=settings.SESSION_COOKIE_SECURE,
        httponly=True,
        samesite="Lax",
        path=capability_cookie_path(capability),
    )


def delete_access_cookie(response: HttpResponse, capability: PortalCapability) -> None:
    response.delete_cookie(
        access_cookie_name(capability),
        path=capability_cookie_path(capability),
        samesite="Lax",
    )
