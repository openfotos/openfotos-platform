"""Signed, capability-scoped visitor authorization cookies."""

from math import ceil

from django.conf import settings
from django.core import signing
from django.http import HttpRequest, HttpResponse
from django.utils import timezone

from .models import GuestCapability, OwnerCapability, PortalCapability

VisitorCapability = OwnerCapability | GuestCapability | PortalCapability

ACCESS_COOKIE_PREFIX = "openfotos_share_"
PRESENTED_COOKIE_PREFIX = "openfotos_presented_"


def capability_kind(capability: VisitorCapability) -> str:
    if isinstance(capability, OwnerCapability):
        return "owner"
    if isinstance(capability, PortalCapability):
        return "portal"
    return "guest"


def capability_cookie_path(capability: VisitorCapability) -> str:
    if isinstance(capability, PortalCapability):
        return f"/portfolio/events/{capability.id}/"
    return f"/share/{capability_kind(capability)}/{capability.id}/"


def access_cookie_name(capability: VisitorCapability) -> str:
    return f"{ACCESS_COOKIE_PREFIX}{capability_kind(capability)}_{capability.id.hex}"


def presented_cookie_name(capability: VisitorCapability) -> str:
    return f"{PRESENTED_COOKIE_PREFIX}{capability_kind(capability)}_{capability.id.hex}"


def _session_ttl(capability: VisitorCapability) -> int:
    if isinstance(capability, OwnerCapability):
        configured = settings.OWNER_SESSION_TTL_SECONDS
    elif isinstance(capability, PortalCapability):
        configured = settings.PORTAL_SESSION_TTL_SECONDS
    else:
        configured = settings.GUEST_SESSION_TTL_SECONDS
    event = (
        capability.event if not isinstance(capability, GuestCapability) else capability.owner.event
    )
    expiries = [capability.expires_at, event.expires_at]
    if isinstance(capability, GuestCapability):
        expiries.append(capability.owner.expires_at)
    remaining = min(ceil((expiry - timezone.now()).total_seconds()) for expiry in expiries)
    return max(0, min(configured, remaining))


def _payload(capability: VisitorCapability) -> dict[str, str]:
    event = (
        capability.event if not isinstance(capability, GuestCapability) else capability.owner.event
    )
    return {
        "capability": str(capability.id),
        "event": str(event.id),
        "access_version": str(capability.access_version),
        "event_access_version": str(event.share_access_version),
        "kind": capability_kind(capability),
    }


def _salt(capability: VisitorCapability, purpose: str) -> str:
    return f"openfotos.share.{capability_kind(capability)}.{purpose}.v1"


def has_valid_access_cookie(request: HttpRequest, capability: VisitorCapability) -> bool:
    signed_value = request.COOKIES.get(access_cookie_name(capability))
    if not signed_value:
        return False
    try:
        payload = signing.loads(
            signed_value,
            salt=_salt(capability, "access"),
            max_age=(
                settings.OWNER_SESSION_TTL_SECONDS
                if isinstance(capability, OwnerCapability)
                else (
                    settings.PORTAL_SESSION_TTL_SECONDS
                    if isinstance(capability, PortalCapability)
                    else settings.GUEST_SESSION_TTL_SECONDS
                )
            ),
        )
    except signing.BadSignature:
        return False
    return payload == _payload(capability)


def has_valid_presented_cookie(request: HttpRequest, capability: VisitorCapability) -> bool:
    signed_value = request.COOKIES.get(presented_cookie_name(capability))
    if not signed_value:
        return False
    try:
        payload = signing.loads(
            signed_value,
            salt=_salt(capability, "presented"),
            max_age=settings.SHARE_PRESENTATION_TTL_SECONDS,
        )
    except signing.BadSignature:
        return False
    return payload == _payload(capability)


def set_access_cookie(response: HttpResponse, capability: VisitorCapability) -> None:
    ttl = _session_ttl(capability)
    if ttl <= 0:
        return
    response.set_cookie(
        access_cookie_name(capability),
        signing.dumps(
            _payload(capability),
            salt=_salt(capability, "access"),
            compress=True,
        ),
        max_age=ttl,
        httponly=True,
        secure=not settings.DEBUG,
        samesite="Lax",
        path=capability_cookie_path(capability),
    )


def set_presented_cookie(response: HttpResponse, capability: VisitorCapability) -> None:
    response.set_cookie(
        presented_cookie_name(capability),
        signing.dumps(
            _payload(capability),
            salt=_salt(capability, "presented"),
            compress=True,
        ),
        max_age=settings.SHARE_PRESENTATION_TTL_SECONDS,
        httponly=True,
        secure=not settings.DEBUG,
        samesite="Lax",
        path=capability_cookie_path(capability),
    )


def delete_share_cookies(response: HttpResponse, capability: VisitorCapability) -> None:
    path = capability_cookie_path(capability)
    for name in (access_cookie_name(capability), presented_cookie_name(capability)):
        response.delete_cookie(name, path=path, samesite="Lax")


def delete_presented_cookie(response: HttpResponse, capability: VisitorCapability) -> None:
    response.delete_cookie(
        presented_cookie_name(capability),
        path=capability_cookie_path(capability),
        samesite="Lax",
    )
