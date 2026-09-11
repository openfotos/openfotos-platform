from datetime import timedelta
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from openfotos_server.events.desktop_auth import (
    DesktopAuthError,
    authenticate_access_token,
    authenticate_lead,
    create_invitation,
    redeem_invitation,
    refresh_session,
    register_lead_device,
)
from openfotos_server.events.models import (
    DesktopSession,
    Event,
    Photographer,
    PhotographerMembership,
    UploaderDevice,
)

pytestmark = pytest.mark.django_db


def lead_and_event():
    photographer = Photographer.objects.create(slug="alpha", display_name="Alpha Photos")
    user = get_user_model().objects.create_user(username="lead", password="correct-password")
    PhotographerMembership.objects.create(photographer=photographer, user=user)
    event = Event.objects.create(
        photographer=photographer,
        name="Reception",
        expires_at=timezone.now() + timedelta(days=30),
    )
    return photographer, user, event


def test_event_allows_one_to_ten_total_contribution_devices_including_lead() -> None:
    photographer, _, event = lead_and_event()
    lead = authenticate_lead(
        photographer=photographer,
        username="lead",
        password="correct-password",
        installation_id=uuid4(),
    )
    register_lead_device(session=lead.session, event=event, label="Lead workstation")
    issued = create_invitation(session=lead.session, event=event)

    first_installation = uuid4()
    redeem_invitation(
        photographer=photographer,
        token=issued.token,
        installation_id=first_installation,
        label="Uploader 1",
    )
    for index in range(2, 10):
        redeem_invitation(
            photographer=photographer,
            token=issued.token,
            installation_id=uuid4(),
            label=f"Uploader {index}",
        )

    assert UploaderDevice.objects.filter(event=event).count() == 10
    event_again, _ = redeem_invitation(
        photographer=photographer,
        token=issued.token,
        installation_id=first_installation,
        label="Ignored replacement label",
    )
    assert event_again == event
    assert UploaderDevice.objects.filter(event=event).count() == 10

    with pytest.raises(DesktopAuthError) as error:
        redeem_invitation(
            photographer=photographer,
            token=issued.token,
            installation_id=uuid4(),
            label="Uploader 10",
        )
    assert error.value.code == "event_device_limit"


def test_refresh_response_loss_recovers_within_the_short_replay_window() -> None:
    photographer, _, _ = lead_and_event()
    original = authenticate_lead(
        photographer=photographer,
        username="lead",
        password="correct-password",
        installation_id=uuid4(),
    )

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
    photographer, _, _ = lead_and_event()
    original = authenticate_lead(
        photographer=photographer,
        username="lead",
        password="correct-password",
        installation_id=uuid4(),
    )
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


def test_invitation_secret_is_hashed_and_wrong_tenant_cannot_redeem() -> None:
    photographer, _, event = lead_and_event()
    lead = authenticate_lead(
        photographer=photographer,
        username="lead",
        password="correct-password",
        installation_id=uuid4(),
    )
    issued = create_invitation(session=lead.session, event=event)
    other = Photographer.objects.create(slug="beta", display_name="Beta Photos")

    assert issued.invitation.token_hash != issued.token
    assert issued.token not in str(issued.invitation.__dict__)
    with pytest.raises(DesktopAuthError) as error:
        redeem_invitation(
            photographer=other,
            token=issued.token,
            installation_id=uuid4(),
            label="Wrong tenant",
        )
    assert error.value.code == "invalid_invitation"
