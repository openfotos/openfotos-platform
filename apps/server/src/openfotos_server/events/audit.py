"""Privacy-preserving audit helpers for security-sensitive actions."""

import hashlib
import hmac
from typing import Any
from uuid import UUID, uuid4

from django.conf import settings
from django.http import HttpRequest

from .models import AuditAction, AuditEvent, AuditResult, Event, Photographer


def privacy_hash(value: str, *, purpose: str) -> str:
    """Return a keyed digest so low-entropy identifiers are not recoverable from logs."""
    message = f"openfotos:{purpose}:{value}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()


def request_client_hash(request: HttpRequest) -> str:
    """Hash the direct peer address without trusting caller-controlled proxy headers."""
    peer_address = request.META.get("REMOTE_ADDR", "unknown")
    return privacy_hash(peer_address, purpose="client-address")


def request_id(request: HttpRequest | None) -> UUID:
    if request is None:
        return uuid4()
    return getattr(request, "openfotos_request_id", uuid4())


def record_audit(
    *,
    photographer: Photographer,
    action: AuditAction,
    result: AuditResult,
    request: HttpRequest | None = None,
    event: Event | None = None,
    actor=None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """Persist a deliberately small audit record; callers must not pass secrets."""
    return AuditEvent.objects.create(
        photographer=photographer,
        event=event,
        actor=actor,
        action=action,
        result=result,
        client_hash=request_client_hash(request) if request is not None else "",
        request_id=request_id(request),
        metadata=metadata or {},
    )
