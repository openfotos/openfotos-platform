"""Opaque desktop sessions for authenticated photographer installations."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.contrib.auth import authenticate
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from openfotos_contracts import InstallationStatus

from .audit import record_audit
from .models import (
    AuditAction,
    AuditResult,
    DesktopSession,
    Event,
    EventInstallation,
    Photographer,
    PhotographerMembership,
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


def authenticate_photographer(
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
def _issue_session(*, photographer: Photographer, installation_id: UUID, user) -> SessionTokens:
    now = timezone.now()
    access_token = _new_token("access")
    refresh_token = _new_token("refresh")
    DesktopSession.objects.filter(
        photographer=photographer,
        installation_id=installation_id,
        user=user,
        revoked_at__isnull=True,
    ).update(revoked_at=now)
    session = DesktopSession.objects.create(
        photographer=photographer,
        user=user,
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
        session = DesktopSession.objects.select_related("photographer", "user").get(
            access_token_hash=token_digest(token),
            access_expires_at__gt=now,
            revoked_at__isnull=True,
        )
    except DesktopSession.DoesNotExist as exc:
        raise DesktopAuthError(
            "invalid_access_token", "The desktop session is unavailable."
        ) from exc
    _validate_session_actor(session)
    return session


def refresh_session(refresh_token: str, *, request=None) -> SessionTokens:
    now = timezone.now()
    digest = token_digest(refresh_token)
    with transaction.atomic():
        try:
            session = (
                DesktopSession.objects.select_for_update(of=("self",))
                .select_related("photographer", "user")
                .get(refresh_token_hash=digest)
            )
        except DesktopSession.DoesNotExist:
            session = _session_for_previous_refresh(digest, now=now)
        if session is not None:
            if session.revoked_at is not None or session.refresh_expires_at <= now:
                session = None
            else:
                _validate_session_actor(session)
                access_token, new_refresh_token = _rotate_session_tokens(session, now=now)
    if session is None:
        raise DesktopAuthError("invalid_refresh_token", "The desktop session must sign in again.")
    record_audit(
        photographer=session.photographer,
        actor=session.user,
        action=AuditAction.DESKTOP_TOKEN_REFRESH,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"session_id": str(session.id)},
    )
    return SessionTokens(
        session=session,
        access_token=access_token,
        refresh_token=new_refresh_token,
    )


def _session_for_previous_refresh(digest: str, *, now) -> DesktopSession | None:
    reused = (
        DesktopSession.objects.select_for_update(of=("self",))
        .select_related("photographer", "user")
        .filter(previous_refresh_token_hash=digest, revoked_at__isnull=True)
        .first()
    )
    grace = timedelta(seconds=settings.DESKTOP_REFRESH_RETRY_GRACE_SECONDS)
    if reused is not None and reused.refresh_expires_at > now and now - reused.updated_at <= grace:
        return reused
    if reused is not None:
        reused.revoked_at = now
        reused.save(update_fields=("revoked_at", "updated_at"))
    return None


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


def _validate_session_actor(session: DesktopSession) -> None:
    if not _active_membership(session.photographer, session.user):
        raise DesktopAuthError("invalid_access_token", "The desktop session is unavailable.")


@transaction.atomic
def revoke_session(
    refresh_token: str,
    *,
    photographer: Photographer,
    request=None,
) -> None:
    """Revoke a desktop session for sign-out; unknown or already-dead tokens succeed."""
    digest = token_digest(refresh_token)
    session = (
        DesktopSession.objects.select_for_update(of=("self",))
        .select_related("photographer", "user")
        .filter(
            Q(refresh_token_hash=digest) | Q(previous_refresh_token_hash=digest),
        )
        .first()
    )
    if session is not None and session.photographer_id != photographer.id:
        raise DesktopAuthError("invalid_refresh_token", "The desktop session must sign in again.")
    if session is None or session.revoked_at is not None:
        return
    session.revoked_at = timezone.now()
    session.save(update_fields=("revoked_at", "updated_at"))
    record_audit(
        photographer=session.photographer,
        actor=session.user,
        action=AuditAction.DESKTOP_LOGOUT,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"session_id": str(session.id)},
    )


@transaction.atomic
def register_event_installation(
    *, session: DesktopSession, event: Event, label: str, request=None
) -> EventInstallation:
    if not _active_membership(event.photographer, session.user):
        raise DesktopAuthError("event_not_found", "The event is unavailable.")
    if session.photographer_id != event.photographer_id:
        raise DesktopAuthError("event_not_found", "The event is unavailable.")
    locked_event = Event.objects.select_for_update().get(pk=event.pk)
    existing = EventInstallation.objects.filter(
        event=locked_event,
        installation_id=session.installation_id,
    ).first()
    if existing is not None:
        if (
            existing.status != InstallationStatus.ACTIVE.value
            or existing.user_id != session.user_id
        ):
            raise DesktopAuthError(
                "installation_revoked", "This workstation is unavailable for the event."
            )
        return existing
    active_count = EventInstallation.objects.filter(
        event=locked_event,
        status=InstallationStatus.ACTIVE.value,
    ).count()
    if active_count >= locked_event.max_contribution_devices:
        raise DesktopAuthError(
            "event_device_limit",
            "This event already has the maximum number of active photographer workstations.",
        )
    installation = EventInstallation.objects.create(
        event=locked_event,
        user=session.user,
        installation_id=session.installation_id,
        label=_validated_label(label),
    )
    record_audit(
        photographer=locked_event.photographer,
        event=locked_event,
        actor=session.user,
        event_installation=installation,
        action=AuditAction.EVENT_INSTALLATION_REGISTERED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"installation_id": str(installation.id)},
    )
    return installation


def _validated_label(label: str) -> str:
    value = label.strip()
    if not value or len(value) > 100 or any(ord(character) < 32 for character in value):
        raise DesktopAuthError(
            "invalid_device_label",
            "Use a workstation label containing 1 to 100 visible characters.",
        )
    return value
