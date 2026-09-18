from unittest.mock import patch
from uuid import uuid4

import pytest
from django.contrib.auth import authenticate, get_user_model
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.test import Client

from openfotos_server.events.account_services import revoke_user_sessions
from openfotos_server.events.desktop_auth import authenticate_photographer
from openfotos_server.events.models import (
    AuditAction,
    DesktopSession,
    Photographer,
    PhotographerMembership,
)

pytestmark = pytest.mark.django_db


def photographer_account():
    photographer = Photographer.objects.create(slug="alpha", display_name="Alpha Photos")
    user = get_user_model().objects.create_user(
        username="photographer",
        password="correct-password",
    )
    PhotographerMembership.objects.create(photographer=photographer, user=user)
    return photographer, user


def test_recovery_revokes_desktop_and_browser_sessions_for_only_the_selected_user() -> None:
    photographer, user = photographer_account()
    other = get_user_model().objects.create_user(username="other", password="other-password")
    tokens = authenticate_photographer(
        photographer=photographer,
        username=user.username,
        password="correct-password",
        installation_id=uuid4(),
    )
    client = Client()
    client.force_login(user)
    selected_session_key = client.session.session_key
    other_client = Client()
    other_client.force_login(other)
    other_session_key = other_client.session.session_key

    result = revoke_user_sessions(user=user)

    assert result.desktop_sessions == 1
    assert result.browser_sessions == 1
    assert DesktopSession.objects.get(pk=tokens.session.pk).revoked_at is not None
    assert not Session.objects.filter(session_key=selected_session_key).exists()
    assert Session.objects.filter(session_key=other_session_key).exists()
    audit = photographer.audit_events.get(action=AuditAction.MEMBERSHIP_CHANGED)
    assert audit.metadata["change"] == "account_sessions_revoked"


def test_password_reset_prompts_securely_and_revokes_existing_sessions() -> None:
    photographer, user = photographer_account()
    authenticate_photographer(
        photographer=photographer,
        username=user.username,
        password="correct-password",
        installation_id=uuid4(),
    )
    client = Client()
    client.force_login(user)
    session_key = client.session.session_key
    new_password = "A-longer-recovery-password-482!"

    with patch("getpass.getpass", side_effect=(new_password, new_password)):
        call_command(
            "reset_photographer_password",
            username=user.username,
            confirm=True,
        )

    assert authenticate(username=user.username, password="correct-password") is None
    assert authenticate(username=user.username, password=new_password) is not None
    assert not Session.objects.filter(session_key=session_key).exists()
    assert not DesktopSession.objects.filter(user=user, revoked_at__isnull=True).exists()
