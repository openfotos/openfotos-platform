"""Tenant-scoped access to the single PIN-protected event portal."""

from django.utils import timezone

from .models import Photographer, PortalCapability


class ShareAccessError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def portal_is_available(portal: PortalCapability, *, at=None) -> bool:
    checked_at = at or timezone.now()
    return portal.has_live_credentials(at=checked_at) and portal.event.is_publicly_available(
        at=checked_at
    )


def portal_for_tenant(*, photographer: Photographer, event_slug: str) -> PortalCapability:
    try:
        portal = PortalCapability.objects.select_related("event__photographer").get(
            event__slug=event_slug,
            event__photographer=photographer,
        )
    except PortalCapability.DoesNotExist as exc:
        raise ShareAccessError("capability_not_found", "This gallery is unavailable.") from exc
    if not portal_is_available(portal):
        raise ShareAccessError("capability_not_found", "This gallery is unavailable.")
    return portal
