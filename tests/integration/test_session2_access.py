from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from openfotos_contracts import EventState
from openfotos_server.events.cookies import visitor_cookie_name
from openfotos_server.events.models import (
    AuditAction,
    AuditResult,
    Event,
    Photographer,
    PhotographerMembership,
    RateLimitBucket,
)
from openfotos_server.events.services import revoke_event_sessions, rotate_event_token

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def configure_test_static_files(settings):
    """Render templates without requiring a production collectstatic manifest."""
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
    settings.MIDDLEWARE = [
        middleware
        for middleware in settings.MIDDLEWARE
        if middleware != "whitenoise.middleware.WhiteNoiseMiddleware"
    ]


def make_event(
    photographer: Photographer,
    *,
    name: str,
    state: EventState = EventState.DRAFT,
    pin: str = "1234",
    expires_at=None,
) -> Event:
    event = Event(
        photographer=photographer,
        name=name,
        state=state,
        expires_at=expires_at,
    )
    event.set_pin(pin)
    event.save()
    return event


@pytest.fixture
def tenants():
    alpha = Photographer.objects.create(slug="alpha", display_name="Alpha Photos")
    beta = Photographer.objects.create(slug="beta", display_name="Beta Photos")
    return alpha, beta


def test_admin_is_available_only_on_the_base_host(tenants) -> None:
    client = Client()

    base_response = client.get("/admin/", headers={"host": "localhost"})
    tenant_response = client.get("/admin/", headers={"host": "alpha.localhost"})

    assert base_response.status_code == 302
    assert tenant_response.status_code == 404


def test_photographer_login_and_dashboard_are_tenant_scoped(tenants) -> None:
    alpha, beta = tenants
    user = get_user_model().objects.create_user(username="owner", password="correct-password")
    PhotographerMembership.objects.create(photographer=alpha, user=user)
    make_event(alpha, name="Visible Alpha Event")
    make_event(beta, name="Hidden Beta Event")
    client = Client()

    login_response = client.post(
        reverse("events:login"),
        {"username": "owner", "password": "correct-password"},
        headers={"host": "alpha.localhost"},
    )
    assert login_response.status_code == 302
    assert login_response.url == reverse("events:dashboard")
    account_cookie = login_response.cookies["openfotos_account_session"]
    assert account_cookie["httponly"]
    assert account_cookie["secure"]
    assert account_cookie["domain"] == ""

    alpha_dashboard = client.get(
        reverse("events:dashboard"),
        headers={"host": "alpha.localhost"},
    )
    assert alpha_dashboard.status_code == 200
    assert b"Visible Alpha Event" in alpha_dashboard.content
    assert b"Hidden Beta Event" not in alpha_dashboard.content

    beta_dashboard = client.get(
        reverse("events:dashboard"),
        headers={"host": "beta.localhost"},
    )
    assert beta_dashboard.status_code == 404


def test_valid_credentials_do_not_authorize_the_wrong_tenant(tenants) -> None:
    alpha, beta = tenants
    user = get_user_model().objects.create_user(username="owner", password="correct-password")
    PhotographerMembership.objects.create(photographer=alpha, user=user)
    client = Client()

    response = client.post(
        reverse("events:login"),
        {"username": "owner", "password": "correct-password"},
        headers={"host": "beta.localhost"},
    )

    assert response.status_code == 200
    assert b"could not sign you in" in response.content
    assert "_auth_user_id" not in client.session
    audit = beta.audit_events.get(action=AuditAction.PHOTOGRAPHER_LOGIN)
    assert audit.result == AuditResult.DENIED
    assert audit.actor is None


def test_locked_event_reveals_no_event_metadata_and_unlocks_with_scoped_cookie(tenants) -> None:
    alpha, _ = tenants
    event = make_event(
        alpha,
        name="Private Reception",
        state=EventState.PUBLISHED,
        expires_at=timezone.now() + timedelta(days=2),
    )
    event_url = reverse("events:event-access", args=(event.public_token,))
    client = Client()

    locked = client.get(event_url, headers={"host": "alpha.localhost"})
    assert locked.status_code == 200
    assert b"Private Reception" not in locked.content
    assert b"Alpha Photos" not in locked.content

    unlocked = client.post(event_url, {"pin": "1234"}, headers={"host": "alpha.localhost"})
    assert unlocked.status_code == 302
    cookie = unlocked.cookies[visitor_cookie_name(event)]
    assert cookie["httponly"]
    assert cookie["secure"]
    assert cookie["samesite"] == "Lax"
    assert cookie["domain"] == ""
    assert int(cookie["max-age"]) <= 86_400

    protected = client.get(event_url, headers={"host": "alpha.localhost"})
    assert protected.status_code == 200
    assert b"Private Reception" in protected.content
    assert b"Alpha Photos" in protected.content


def test_event_token_host_and_lifecycle_must_all_match(tenants) -> None:
    alpha, _ = tenants
    event = make_event(
        alpha,
        name="Private Reception",
        state=EventState.PUBLISHED,
        expires_at=timezone.now() + timedelta(days=2),
    )
    event_url = reverse("events:event-access", args=(event.public_token,))
    client = Client()

    assert client.get(event_url, headers={"host": "beta.localhost"}).status_code == 404
    assert (
        client.get(
            reverse("events:event-access", args=("altered-token",)),
            headers={"host": "alpha.localhost"},
        ).status_code
        == 404
    )

    Event.objects.filter(pk=event.pk).update(state=EventState.ARCHIVED)
    assert client.get(event_url, headers={"host": "alpha.localhost"}).status_code == 404


def test_expiry_and_access_revocation_invalidate_an_existing_cookie(tenants) -> None:
    alpha, _ = tenants
    admin = get_user_model().objects.create_superuser(username="admin", password="safe-pass")
    event = make_event(
        alpha,
        name="Private Reception",
        state=EventState.PUBLISHED,
        expires_at=timezone.now() + timedelta(days=2),
    )
    event_url = reverse("events:event-access", args=(event.public_token,))
    client = Client()
    client.post(event_url, {"pin": "1234"}, headers={"host": "alpha.localhost"})

    revoke_event_sessions(event_id=event.id, actor=admin)
    revoked = client.get(event_url, headers={"host": "alpha.localhost"})
    assert revoked.status_code == 200
    assert b"Private Reception" not in revoked.content
    assert b"Enter the event PIN" in revoked.content

    Event.objects.filter(pk=event.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    expired = client.get(event_url, headers={"host": "alpha.localhost"})
    assert expired.status_code == 404


def test_fifth_wrong_pin_is_rate_limited_without_logging_the_pin_or_ip(tenants) -> None:
    alpha, _ = tenants
    event = make_event(
        alpha,
        name="Private Reception",
        state=EventState.PUBLISHED,
        expires_at=timezone.now() + timedelta(days=2),
    )
    event_url = reverse("events:event-access", args=(event.public_token,))
    client = Client(REMOTE_ADDR="203.0.113.8")

    responses = [
        client.post(event_url, {"pin": "9999"}, headers={"host": "alpha.localhost"})
        for _ in range(5)
    ]

    assert [response.status_code for response in responses] == [200, 200, 200, 200, 429]
    assert int(responses[-1].headers["Retry-After"]) > 0
    assert RateLimitBucket.objects.get().failures == 5
    last_audit = event.audit_events.first()
    assert last_audit.result == AuditResult.RATE_LIMITED
    assert last_audit.client_hash != "203.0.113.8"
    assert "203.0.113.8" not in str(last_audit.__dict__)
    assert "9999" not in str(last_audit.__dict__)


def test_token_rotation_breaks_old_links_and_requires_a_fresh_unlock(tenants) -> None:
    alpha, _ = tenants
    admin = get_user_model().objects.create_superuser(username="admin", password="safe-pass")
    event = make_event(
        alpha,
        name="Private Reception",
        state=EventState.PUBLISHED,
        expires_at=timezone.now() + timedelta(days=2),
    )
    old_url = reverse("events:event-access", args=(event.public_token,))
    client = Client()
    client.post(old_url, {"pin": "1234"}, headers={"host": "alpha.localhost"})

    rotated = rotate_event_token(event_id=event.id, actor=admin)
    new_url = reverse("events:event-access", args=(rotated.public_token,))

    assert client.get(old_url, headers={"host": "alpha.localhost"}).status_code == 404
    fresh_link = client.get(new_url, headers={"host": "alpha.localhost"})
    assert fresh_link.status_code == 200
    assert b"Private Reception" not in fresh_link.content
    assert rotated.audit_events.filter(action=AuditAction.EVENT_TOKEN_CHANGED).exists()


def test_unknown_photographer_host_is_not_found() -> None:
    response = Client().get(reverse("events:login"), headers={"host": "missing.localhost"})
    assert response.status_code == 404


def test_fifth_failed_photographer_login_is_rate_limited(tenants) -> None:
    alpha, _ = tenants
    user = get_user_model().objects.create_user(username="owner", password="correct-password")
    PhotographerMembership.objects.create(photographer=alpha, user=user)
    client = Client(REMOTE_ADDR="203.0.113.9")

    responses = [
        client.post(
            reverse("events:login"),
            {"username": "owner", "password": "wrong-password"},
            headers={"host": "alpha.localhost"},
        )
        for _ in range(5)
    ]

    assert [response.status_code for response in responses] == [200, 200, 200, 200, 429]
    assert int(responses[-1].headers["Retry-After"]) > 0
    assert "_auth_user_id" not in client.session
