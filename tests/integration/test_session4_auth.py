from datetime import timedelta
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from openfotos_server.events.desktop_auth import (
    DesktopAuthError,
    authenticate_access_token,
    authenticate_photographer,
    refresh_session,
    register_event_installation,
    revoke_session,
)
from openfotos_server.events.models import (
    AuditAction,
    DesktopSession,
    Event,
    EventInstallation,
    Photographer,
    PhotographerMembership,
)

pytestmark = pytest.mark.django_db


def photographer_and_event():
    photographer = Photographer.objects.create(slug="alpha", display_name="Alpha Photos")
    user = get_user_model().objects.create_user(
        username="photographer",
        password="correct-password",
    )
    PhotographerMembership.objects.create(photographer=photographer, user=user)
    event = Event.objects.create(
        photographer=photographer,
        name="Wedding",
        expires_at=timezone.now() + timedelta(days=30),
    )
    return photographer, user, event


def session_for(photographer, *, installation_id=None):
    return authenticate_photographer(
        photographer=photographer,
        username="photographer",
        password="correct-password",
        installation_id=installation_id or uuid4(),
    )


def test_event_allows_up_to_ten_photographer_installations() -> None:
    photographer, user, event = photographer_and_event()
    first_id = uuid4()
    first = session_for(photographer, installation_id=first_id)
    registered = register_event_installation(
        session=first.session,
        event=event,
        label="Studio workstation 1",
    )
    for index in range(2, 11):
        tokens = session_for(photographer)
        register_event_installation(
            session=tokens.session,
            event=event,
            label=f"Studio workstation {index}",
        )

    assert EventInstallation.objects.filter(event=event).count() == 10
    repeated = register_event_installation(
        session=first.session,
        event=event,
        label="Ignored replacement label",
    )
    assert repeated == registered
    assert repeated.user == user

    with pytest.raises(DesktopAuthError) as error:
        register_event_installation(
            session=session_for(photographer).session,
            event=event,
            label="Studio workstation 11",
        )
    assert error.value.code == "event_device_limit"


def test_refresh_response_loss_recovers_within_the_short_replay_window() -> None:
    photographer, _, _ = photographer_and_event()
    original = session_for(photographer)

    rotated = refresh_session(original.refresh_token)

    with pytest.raises(DesktopAuthError) as stale_access:
        authenticate_access_token(original.access_token)
    assert stale_access.value.code == "invalid_access_token"
    assert authenticate_access_token(rotated.access_token).id == original.session.id

    recovered = refresh_session(original.refresh_token)
    assert recovered.refresh_token != rotated.refresh_token
    assert authenticate_access_token(recovered.access_token).id == original.session.id

    with pytest.raises(DesktopAuthError):
        authenticate_access_token(rotated.access_token)


def test_refresh_reuse_outside_the_grace_window_revokes_the_session(settings) -> None:
    photographer, _, _ = photographer_and_event()
    original = session_for(photographer)
    rotated = refresh_session(original.refresh_token)
    DesktopSession.objects.filter(pk=original.session.id).update(
        updated_at=timezone.now()
        - timedelta(seconds=settings.DESKTOP_REFRESH_RETRY_GRACE_SECONDS + 1)
    )

    with pytest.raises(DesktopAuthError) as reused_refresh:
        refresh_session(original.refresh_token)

    assert reused_refresh.value.code == "invalid_refresh_token"
    assert DesktopSession.objects.get(pk=original.session.id).revoked_at is not None
    with pytest.raises(DesktopAuthError):
        authenticate_access_token(rotated.access_token)


def test_revoke_session_blocks_refresh_and_access_and_is_idempotent() -> None:
    photographer, _, _ = photographer_and_event()
    tokens = session_for(photographer)

    revoke_session(tokens.refresh_token, photographer=photographer)

    with pytest.raises(DesktopAuthError) as refresh_error:
        refresh_session(tokens.refresh_token)
    assert refresh_error.value.code == "invalid_refresh_token"
    with pytest.raises(DesktopAuthError):
        authenticate_access_token(tokens.access_token)
    assert photographer.audit_events.filter(action=AuditAction.DESKTOP_LOGOUT).count() == 1

    # A repeated sign-out with the same dead token is a no-op, not an error.
    revoke_session(tokens.refresh_token, photographer=photographer)
    assert photographer.audit_events.filter(action=AuditAction.DESKTOP_LOGOUT).count() == 1


def test_revoke_session_accepts_the_recent_previous_refresh_token() -> None:
    photographer, _, _ = photographer_and_event()
    original = session_for(photographer)
    rotated = refresh_session(original.refresh_token)

    revoke_session(original.refresh_token, photographer=photographer)

    with pytest.raises(DesktopAuthError):
        refresh_session(rotated.refresh_token)
    with pytest.raises(DesktopAuthError):
        authenticate_access_token(rotated.access_token)


def test_revoke_session_rejects_another_tenant_token() -> None:
    photographer, _, _ = photographer_and_event()
    other = Photographer.objects.create(slug="beta", display_name="Beta Photos")
    tokens = session_for(photographer)

    with pytest.raises(DesktopAuthError) as wrong_tenant:
        revoke_session(tokens.refresh_token, photographer=other)

    assert wrong_tenant.value.code == "invalid_refresh_token"
    assert refresh_session(tokens.refresh_token).access_token


def test_inactive_membership_cannot_authenticate_or_keep_using_a_session() -> None:
    photographer, user, _ = photographer_and_event()
    active = session_for(photographer)
    PhotographerMembership.objects.filter(photographer=photographer, user=user).update(
        is_active=False
    )

    with pytest.raises(DesktopAuthError) as login_error:
        session_for(photographer)
    assert login_error.value.code == "invalid_credentials"

    with pytest.raises(DesktopAuthError) as access_error:
        authenticate_access_token(active.access_token)
    assert access_error.value.code == "invalid_access_token"
