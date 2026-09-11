"""Opaque desktop sessions and event-scoped uploader enrollment."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.contrib.auth import authenticate
from django.db import transaction
from django.utils import timezone

from openfotos_contracts import DeviceRole, DeviceStatus, IntakeState

from .audit import record_audit
from .models import (
    AuditAction,
    AuditResult,
    DesktopSession,
    Event,
    Photographer,
    PhotographerMembership,
    UploaderDevice,
    UploaderInvitation,
)


class DesktopAuthError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SessionTokens:
    session: DesktopSession
    access_token: str
    refresh_token: str


@dataclass(frozen=True)
class IssuedInvitation:
    invitation: UploaderInvitation
    token: str


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_token(kind: str) -> str:
    return f"ofts_{kind}_{secrets.token_urlsafe(32)}"


def _active_membership(photographer: Photographer, user) -> bool:
    return bool(
        user
        and user.is_active
        and PhotographerMembership.objects.filter(
            photographer=photographer,
            user=user,
            is_active=True,
        ).exists()
    )


def authenticate_lead(
    *,
    photographer: Photographer,
    username: str,
    password: str,
    installation_id: UUID,
    request=None,
) -> SessionTokens:
    user = authenticate(request, username=username, password=password)
    if not _active_membership(photographer, user):
        record_audit(
            photographer=photographer,
            action=AuditAction.DESKTOP_LOGIN,
            result=AuditResult.DENIED,
            request=request,
        )
        raise DesktopAuthError("invalid_credentials", "The desktop credentials were not accepted.")
    tokens = _issue_session(
        photographer=photographer,
        installation_id=installation_id,
        user=user,
    )
    record_audit(
        photographer=photographer,
        actor=user,
        action=AuditAction.DESKTOP_LOGIN,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"session_id": str(tokens.session.id)},
    )
    return tokens


@transaction.atomic
def _issue_session(
    *,
    photographer: Photographer,
    installation_id: UUID,
    user=None,
    device: UploaderDevice | None = None,
) -> SessionTokens:
    now = timezone.now()
    access_token = _new_token("access")
    refresh_token = _new_token("refresh")
    existing = DesktopSession.objects.filter(
        photographer=photographer,
        installation_id=installation_id,
        user=user,
        device=device,
        revoked_at__isnull=True,
    )
    existing.update(revoked_at=now)
    session = DesktopSession.objects.create(
        photographer=photographer,
        user=user,
        device=device,
        installation_id=installation_id,
        access_token_hash=token_digest(access_token),
        access_expires_at=now + timedelta(seconds=settings.DESKTOP_ACCESS_TTL_SECONDS),
        refresh_token_hash=token_digest(refresh_token),
        refresh_expires_at=now + timedelta(seconds=settings.DESKTOP_REFRESH_TTL_SECONDS),
    )
    return SessionTokens(session=session, access_token=access_token, refresh_token=refresh_token)


def authenticate_access_token(token: str) -> DesktopSession:
    now = timezone.now()
    try:
        session = DesktopSession.objects.select_related(
            "photographer", "user", "device", "device__event"
        ).get(
            access_token_hash=token_digest(token),
            access_expires_at__gt=now,
            revoked_at__isnull=True,
        )
    except DesktopSession.DoesNotExist as exc:
        raise DesktopAuthError(
            "invalid_access_token", "The desktop session is unavailable."
        ) from exc
    _validate_session_actor(session, now=now)
    return session


def refresh_session(refresh_token: str, *, request=None) -> SessionTokens:
    now = timezone.now()
    digest = token_digest(refresh_token)
    with transaction.atomic():
        try:
            session = (
                DesktopSession.objects.select_for_update(of=("self",))
                .select_related("photographer", "user", "device", "device__event")
                .get(refresh_token_hash=digest)
            )
        except DesktopSession.DoesNotExist:
            reused = (
                DesktopSession.objects.select_for_update(of=("self",))
                .filter(
                    previous_refresh_token_hash=digest,
                    revoked_at__isnull=True,
                )
                .first()
            )
            grace = timedelta(seconds=settings.DESKTOP_REFRESH_RETRY_GRACE_SECONDS)
            if (
                reused is not None
                and reused.refresh_expires_at > now
                and now - reused.updated_at <= grace
            ):
                session = reused
            elif reused is not None:
                reused.revoked_at = now
                reused.save(update_fields=("revoked_at", "updated_at"))
                session = None
            else:
                session = None
        if session is not None:
            if session.revoked_at is not None or session.refresh_expires_at <= now:
                session = None
            else:
                _validate_session_actor(session, now=now)
                access_token, new_refresh_token = _rotate_session_tokens(session, now=now)
    if session is None:
        raise DesktopAuthError("invalid_refresh_token", "The desktop session must sign in again.")
    record_audit(
        photographer=session.photographer,
        actor=session.user,
        uploader_device=session.device,
        action=AuditAction.DESKTOP_TOKEN_REFRESH,
        result=AuditResult.SUCCEEDED,
        request=request,
        event=session.device.event if session.device else None,
        metadata={"session_id": str(session.id)},
    )
    return SessionTokens(
        session=session,
        access_token=access_token,
        refresh_token=new_refresh_token,
    )


def _rotate_session_tokens(session: DesktopSession, *, now) -> tuple[str, str]:
    access_token = _new_token("access")
    refresh_token = _new_token("refresh")
    session.previous_refresh_token_hash = session.refresh_token_hash
    session.refresh_token_hash = token_digest(refresh_token)
    session.access_token_hash = token_digest(access_token)
    session.access_expires_at = now + timedelta(seconds=settings.DESKTOP_ACCESS_TTL_SECONDS)
    session.save(
        update_fields=(
            "previous_refresh_token_hash",
            "refresh_token_hash",
            "access_token_hash",
            "access_expires_at",
            "updated_at",
        )
    )
    return access_token, refresh_token


def _validate_session_actor(session: DesktopSession, *, now) -> None:
    if session.user_id is not None:
        if not _active_membership(session.photographer, session.user):
            raise DesktopAuthError("invalid_access_token", "The desktop session is unavailable.")
        return
    device = session.device
    if (
        device is None
        or device.status != DeviceStatus.ACTIVE.value
        or device.event.photographer_id != session.photographer_id
        or (device.event.expires_at is not None and device.event.expires_at <= now)
    ):
        raise DesktopAuthError("invalid_access_token", "The desktop session is unavailable.")


@transaction.atomic
def register_lead_device(*, session: DesktopSession, event: Event, label: str) -> UploaderDevice:
    if session.user_id is None or not _active_membership(event.photographer, session.user):
        raise DesktopAuthError("event_not_found", "The event is unavailable.")
    if session.photographer_id != event.photographer_id:
        raise DesktopAuthError("event_not_found", "The event is unavailable.")
    locked_event = Event.objects.select_for_update().get(pk=event.pk)
    existing = UploaderDevice.objects.filter(
        event=locked_event,
        installation_id=session.installation_id,
    ).first()
    if existing is not None:
        if existing.status != DeviceStatus.ACTIVE.value:
            raise DesktopAuthError("device_revoked", "This workstation was revoked for the event.")
        return existing
    _require_available_device_slot(locked_event)
    return UploaderDevice.objects.create(
        event=locked_event,
        installation_id=session.installation_id,
        label=_validated_label(label),
        role=DeviceRole.LEAD.value,
    )


@transaction.atomic
def create_invitation(
    *,
    session: DesktopSession,
    event: Event,
    request=None,
) -> IssuedInvitation:
    if session.user_id is None or not _active_membership(event.photographer, session.user):
        raise DesktopAuthError("event_not_found", "The event is unavailable.")
    locked_event = Event.objects.select_for_update().get(pk=event.pk)
    if (
        locked_event.photographer_id != session.photographer_id
        or locked_event.intake_state != IntakeState.OPEN.value
    ):
        raise DesktopAuthError("intake_closed", "Uploader enrollment is closed for this event.")
    token = _new_token("invite")
    invitation = UploaderInvitation.objects.create(
        event=locked_event,
        token_hash=token_digest(token),
        created_by=session.user,
        expires_at=timezone.now() + timedelta(seconds=settings.UPLOADER_INVITATION_TTL_SECONDS),
        max_redemptions=locked_event.max_contribution_devices,
    )
    record_audit(
        photographer=locked_event.photographer,
        event=locked_event,
        actor=session.user,
        action=AuditAction.UPLOADER_INVITATION_CREATED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"invitation_id": str(invitation.id)},
    )
    return IssuedInvitation(invitation=invitation, token=token)


@transaction.atomic
def redeem_invitation(
    *,
    photographer: Photographer,
    token: str,
    installation_id: UUID,
    label: str,
    request=None,
) -> tuple[Event, SessionTokens]:
    now = timezone.now()
    try:
        invitation = (
            UploaderInvitation.objects.select_for_update()
            .select_related("event", "event__photographer")
            .get(token_hash=token_digest(token), event__photographer=photographer)
        )
    except UploaderInvitation.DoesNotExist as exc:
        raise DesktopAuthError(
            "invalid_invitation", "The uploader invitation is unavailable."
        ) from exc
    event = Event.objects.select_for_update().get(pk=invitation.event_id)
    if (
        invitation.revoked_at is not None
        or invitation.closed_at is not None
        or invitation.expires_at <= now
        or invitation.redemption_count >= invitation.max_redemptions
        or event.intake_state != IntakeState.OPEN.value
    ):
        raise DesktopAuthError("invalid_invitation", "The uploader invitation is unavailable.")
    existing = UploaderDevice.objects.filter(
        event=event,
        installation_id=installation_id,
    ).first()
    if existing is not None and existing.status == DeviceStatus.ACTIVE.value:
        tokens = _issue_session(
            photographer=photographer,
            installation_id=installation_id,
            device=existing,
        )
        return event, tokens
    if existing is not None:
        raise DesktopAuthError("device_revoked", "This workstation was revoked for the event.")
    _require_available_device_slot(event)
    device = UploaderDevice.objects.create(
        event=event,
        installation_id=installation_id,
        label=_validated_label(label),
        role=DeviceRole.UPLOADER.value,
        invitation=invitation,
    )
    invitation.redemption_count += 1
    invitation.save(update_fields=("redemption_count",))
    tokens = _issue_session(
        photographer=photographer,
        installation_id=installation_id,
        device=device,
    )
    record_audit(
        photographer=photographer,
        event=event,
        uploader_device=device,
        action=AuditAction.UPLOADER_INVITATION_REDEEMED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"invitation_id": str(invitation.id), "device_id": str(device.id)},
    )
    return event, tokens


def _validated_label(label: str) -> str:
    value = label.strip()
    if not value or len(value) > 100 or any(ord(character) < 32 for character in value):
        raise DesktopAuthError(
            "invalid_device_label",
            "Use a device label containing 1 to 100 visible characters.",
        )
    return value


def _require_available_device_slot(event: Event) -> None:
    active_count = UploaderDevice.objects.filter(
        event=event,
        status=DeviceStatus.ACTIVE.value,
    ).count()
    if active_count >= event.max_contribution_devices:
        raise DesktopAuthError(
            "event_device_limit",
            "This event already has the maximum number of active contribution devices.",
        )
