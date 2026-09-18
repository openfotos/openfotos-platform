"""Capability issuance, revocation, and immutable gallery scopes."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from openfotos_contracts import EventState

from .audit import record_audit
from .models import (
    AuditAction,
    AuditResult,
    Event,
    FaceSearchResultSet,
    GuestCapability,
    OwnerCapability,
    Photographer,
    PortalCapability,
    SubEvent,
    generate_share_pin,
)


class ShareAccessError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class IssuedCapability:
    capability: OwnerCapability | GuestCapability
    secret: str
    pin: str


def capability_secret_digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii", errors="strict")).hexdigest()


def secret_matches(capability: OwnerCapability | GuestCapability, secret: str) -> bool:
    try:
        digest = capability_secret_digest(secret)
    except UnicodeError:
        return False
    return secrets.compare_digest(capability.secret_digest, digest)


def owner_is_available(owner: OwnerCapability, *, at=None) -> bool:
    checked_at = at or timezone.now()
    return owner.has_live_credentials(at=checked_at) and owner.event.is_publicly_available(
        at=checked_at
    )


def guest_is_available(guest: GuestCapability, *, at=None) -> bool:
    checked_at = at or timezone.now()
    return (
        guest.has_live_credentials(at=checked_at)
        and owner_is_available(guest.owner, at=checked_at)
        and (guest.sub_event_id is None or not guest.sub_event.is_archived)
    )


def portal_is_available(portal: PortalCapability, *, at=None) -> bool:
    checked_at = at or timezone.now()
    return portal.has_live_credentials(at=checked_at) and portal.event.is_publicly_available(
        at=checked_at
    )


def portal_for_tenant(*, photographer: Photographer, capability_id: UUID) -> PortalCapability:
    try:
        portal = PortalCapability.objects.select_related("event__photographer").get(
            pk=capability_id,
            event__photographer=photographer,
        )
    except PortalCapability.DoesNotExist as exc:
        raise ShareAccessError("capability_not_found", "This gallery is unavailable.") from exc
    if not portal_is_available(portal):
        raise ShareAccessError("capability_not_found", "This gallery is unavailable.")
    return portal


def owner_for_tenant(*, photographer: Photographer, capability_id: UUID) -> OwnerCapability:
    try:
        owner = OwnerCapability.objects.select_related("event__photographer").get(
            pk=capability_id,
            event__photographer=photographer,
        )
    except OwnerCapability.DoesNotExist as exc:
        raise ShareAccessError("capability_not_found", "This private link is unavailable.") from exc
    if not owner_is_available(owner):
        raise ShareAccessError("capability_not_found", "This private link is unavailable.")
    return owner


def guest_for_tenant(*, photographer: Photographer, capability_id: UUID) -> GuestCapability:
    try:
        guest = GuestCapability.objects.select_related(
            "owner__event__photographer", "sub_event"
        ).get(
            pk=capability_id,
            owner__event__photographer=photographer,
        )
    except GuestCapability.DoesNotExist as exc:
        raise ShareAccessError("capability_not_found", "This private link is unavailable.") from exc
    if not guest_is_available(guest):
        raise ShareAccessError("capability_not_found", "This private link is unavailable.")
    return guest


@transaction.atomic
def issue_owner_capability(*, event: Event, actor, request=None) -> IssuedCapability:
    locked_event = Event.objects.select_for_update().select_related("photographer").get(pk=event.pk)
    if locked_event.state != EventState.PUBLISHED.value or not locked_event.is_publicly_available():
        raise ShareAccessError(
            "event_not_published", "Publish the event before issuing owner access."
        )
    now = timezone.now()
    expires_at = min(
        now + timedelta(seconds=settings.SHARE_CAPABILITY_TTL_SECONDS),
        locked_event.expires_at,
    )
    existing = OwnerCapability.objects.select_for_update().filter(event=locked_event).first()
    if existing is not None and owner_is_available(existing, at=now):
        raise ShareAccessError(
            "owner_capability_exists", "Revoke the current owner link before issuing another."
        )

    secret = secrets.token_urlsafe(32)
    pin = generate_share_pin()
    if existing is None:
        owner = OwnerCapability(
            event=locked_event,
            created_by=actor,
            secret_digest=capability_secret_digest(secret),
            expires_at=expires_at,
        )
    else:
        owner = existing
        _revoke_guests(owner=owner, at=now)
        FaceSearchResultSet.objects.filter(owner_capability=owner).delete()
        owner.created_by = actor
        owner.secret_digest = capability_secret_digest(secret)
        owner.access_version = uuid4()
        owner.expires_at = expires_at
        owner.revoked_at = None
    owner.set_pin(pin)
    owner.save()
    record_audit(
        photographer=locked_event.photographer,
        event=locked_event,
        actor=actor,
        action=AuditAction.OWNER_CAPABILITY_ISSUED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"owner_capability_id": str(owner.id), "expires_at": expires_at.isoformat()},
    )
    return IssuedCapability(capability=owner, secret=secret, pin=pin)


@transaction.atomic
def revoke_owner_capability(*, event: Event, actor, request=None) -> OwnerCapability:
    try:
        owner = (
            OwnerCapability.objects.select_for_update()
            .select_related("event__photographer")
            .get(event=event, event__photographer=event.photographer)
        )
    except OwnerCapability.DoesNotExist as exc:
        raise ShareAccessError(
            "owner_capability_not_found", "This event has no owner capability."
        ) from exc
    if owner.revoked_at is not None:
        return owner
    now = timezone.now()
    owner.revoked_at = now
    owner.access_version = uuid4()
    owner.save(update_fields=("revoked_at", "access_version", "updated_at"))
    _revoke_guests(owner=owner, at=now)
    FaceSearchResultSet.objects.filter(owner_capability=owner).delete()
    record_audit(
        photographer=owner.event.photographer,
        event=owner.event,
        actor=actor,
        action=AuditAction.OWNER_CAPABILITY_REVOKED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"owner_capability_id": str(owner.id)},
    )
    return owner


@transaction.atomic
def create_guest_capability(
    *,
    owner: OwnerCapability,
    label: str,
    sub_event_id: UUID | None,
    request=None,
) -> IssuedCapability:
    locked_owner = (
        OwnerCapability.objects.select_for_update()
        .select_related("event__photographer")
        .get(pk=owner.pk)
    )
    now = timezone.now()
    if not owner_is_available(locked_owner, at=now):
        raise ShareAccessError("owner_capability_unavailable", "Owner access is unavailable.")
    active_count = locked_owner.guest_capabilities.filter(
        revoked_at__isnull=True,
        expires_at__gt=now,
    ).count()
    if active_count >= settings.MAX_ACTIVE_GUEST_CAPABILITIES:
        raise ShareAccessError(
            "guest_capability_limit",
            "Revoke a guest link before creating more than "
            f"{settings.MAX_ACTIVE_GUEST_CAPABILITIES}.",
        )
    sub_event = None
    if sub_event_id is not None:
        try:
            sub_event = SubEvent.objects.get(
                pk=sub_event_id,
                event=locked_owner.event,
                is_archived=False,
            )
        except SubEvent.DoesNotExist as exc:
            raise ShareAccessError(
                "sub_event_not_found", "Select an active sub-event from this event."
            ) from exc
    normalized_label = label.strip()
    if len(normalized_label) > 80 or any(
        not character.isprintable() for character in normalized_label
    ):
        raise ShareAccessError(
            "invalid_guest_label", "Guest labels may contain up to 80 characters."
        )

    secret = secrets.token_urlsafe(32)
    pin = generate_share_pin()
    expires_at = min(
        now + timedelta(seconds=settings.SHARE_CAPABILITY_TTL_SECONDS),
        locked_owner.expires_at,
        locked_owner.event.expires_at,
    )
    guest = GuestCapability(
        owner=locked_owner,
        sub_event=sub_event,
        label=normalized_label,
        secret_digest=capability_secret_digest(secret),
        expires_at=expires_at,
    )
    guest.set_pin(pin)
    guest.save()
    record_audit(
        photographer=locked_owner.event.photographer,
        event=locked_owner.event,
        action=AuditAction.GUEST_CAPABILITY_CREATED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={
            "guest_capability_id": str(guest.id),
            "sub_event_id": str(sub_event.id) if sub_event else None,
            "expires_at": expires_at.isoformat(),
        },
    )
    return IssuedCapability(capability=guest, secret=secret, pin=pin)


@transaction.atomic
def revoke_guest_capability(
    *,
    owner: OwnerCapability,
    guest_id: UUID,
    actor=None,
    request=None,
) -> GuestCapability:
    try:
        guest = (
            GuestCapability.objects.select_for_update()
            .select_related("owner__event__photographer")
            .get(pk=guest_id, owner=owner)
        )
    except GuestCapability.DoesNotExist as exc:
        raise ShareAccessError(
            "guest_capability_not_found", "This guest link is unavailable."
        ) from exc
    if guest.revoked_at is not None:
        return guest
    guest.revoked_at = timezone.now()
    guest.access_version = uuid4()
    guest.save(update_fields=("revoked_at", "access_version", "updated_at"))
    FaceSearchResultSet.objects.filter(guest_capability=guest).delete()
    record_audit(
        photographer=guest.owner.event.photographer,
        event=guest.owner.event,
        actor=actor,
        action=AuditAction.GUEST_CAPABILITY_REVOKED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"guest_capability_id": str(guest.id)},
    )
    return guest


def _revoke_guests(*, owner: OwnerCapability, at) -> None:
    guest_ids = list(owner.guest_capabilities.values_list("id", flat=True))
    owner.guest_capabilities.filter(revoked_at__isnull=True).update(
        revoked_at=at,
        access_version=uuid4(),
    )
    FaceSearchResultSet.objects.filter(guest_capability_id__in=guest_ids).delete()
