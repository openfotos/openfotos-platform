"""Manual photographer-account recovery operations."""

from dataclasses import dataclass

from django.contrib.sessions.models import Session
from django.db import transaction
from django.utils import timezone

from .audit import record_audit
from .models import (
    AuditAction,
    AuditResult,
    DesktopSession,
    PhotographerMembership,
)


@dataclass(frozen=True, slots=True)
class SessionRevocationResult:
    desktop_sessions: int
    browser_sessions: int


@transaction.atomic
def revoke_user_sessions(*, user, actor=None, at=None) -> SessionRevocationResult:
    """Revoke desktop tokens and delete every decodable browser session for one user."""
    revoked_at = at or timezone.now()
    desktop_count = DesktopSession.objects.filter(user=user, revoked_at__isnull=True).update(
        revoked_at=revoked_at,
        updated_at=revoked_at,
    )
    browser_count = 0
    for session in Session.objects.filter(expire_date__gt=revoked_at).iterator():
        if str(session.get_decoded().get("_auth_user_id")) != str(user.pk):
            continue
        session.delete()
        browser_count += 1

    memberships = PhotographerMembership.objects.select_related("photographer").filter(
        user=user,
        is_active=True,
    )
    for membership in memberships:
        record_audit(
            photographer=membership.photographer,
            actor=actor,
            action=AuditAction.MEMBERSHIP_CHANGED,
            result=AuditResult.SUCCEEDED,
            metadata={
                "change": "account_sessions_revoked",
                "user_id": user.pk,
                "desktop_session_count": desktop_count,
                "browser_session_count": browser_count,
            },
        )
    return SessionRevocationResult(
        desktop_sessions=desktop_count,
        browser_sessions=browser_count,
    )
