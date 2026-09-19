from datetime import timedelta
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from openfotos_contracts import EventState
from openfotos_server.events.cookies import access_cookie_name
from openfotos_server.events.models import (
    AuditAction,
    AuditResult,
    Event,
    Photographer,
    PhotographerMembership,
    PortalCapability,
    RateLimitBucket,
)
from openfotos_server.events.portfolio_services import rotate_portal_pin

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def configure_test_static_files(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    settings.MIDDLEWARE = [
        item for item in settings.MIDDLEWARE if item != "whitenoise.middleware.WhiteNoiseMiddleware"
    ]


@pytest.fixture
def tenants():
    return (
        Photographer.objects.create(slug="alpha", display_name="Alpha Photos"),
        Photographer.objects.create(slug="beta", display_name="Beta Photos"),
    )


def make_event(photographer, *, name, state=EventState.DRAFT, expires_at=None):
    return Event.objects.create(
        photographer=photographer,
        name=name,
        slug=f"event-{uuid4().hex[:8]}",
        state=state,
        expires_at=expires_at,
    )


def create_portal(event, *, pin="0427"):
    portal = PortalCapability(event=event, expires_at=event.expires_at)
    portal.set_pin(pin)
    portal.save()
    return portal


def test_admin_is_available_only_on_the_base_host(tenants) -> None:
    client = Client()
    assert client.get("/admin/", headers={"host": "localhost"}).status_code == 302
    assert client.get("/admin/", headers={"host": "alpha.localhost"}).status_code == 404


def test_photographer_login_and_dashboard_are_tenant_scoped(tenants) -> None:
    alpha, beta = tenants
    user = get_user_model().objects.create_user(username="owner", password="correct-password")
    PhotographerMembership.objects.create(photographer=alpha, user=user)
    make_event(alpha, name="Visible Alpha Event")
    make_event(beta, name="Hidden Beta Event")
    client = Client()

    response = client.post(
        reverse("events:login"),
        {"username": "owner", "password": "correct-password"},
        headers={"host": "alpha.localhost"},
    )
    assert response.status_code == 302
    assert response.url == reverse("events:dashboard")
    cookie = response.cookies["openfotos_account_session"]
    assert cookie["httponly"] and cookie["secure"] and cookie["domain"] == ""

    dashboard = client.get(reverse("events:dashboard"), headers={"host": "alpha.localhost"})
    assert dashboard.status_code == 200
    assert b"Visible Alpha Event" in dashboard.content
    assert b"Hidden Beta Event" not in dashboard.content
    assert (
        client.get(reverse("events:dashboard"), headers={"host": "beta.localhost"}).status_code
        == 404
    )


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
    assert audit.result == AuditResult.DENIED and audit.actor is None


def test_event_slug_and_pin_unlock_the_portal(tenants) -> None:
    alpha, _ = tenants
    event = make_event(
        alpha,
        name="Private Reception",
        state=EventState.PUBLISHED,
        expires_at=timezone.now() + timedelta(days=2),
    )
    portal = create_portal(event)
    url = reverse("events:portal-gallery", args=(event.slug,))
    client = Client()

    locked = client.get(url, headers={"host": "alpha.localhost"})
    assert locked.status_code == 200
    assert b"View gallery" in locked.content
    # The cover page is intentionally public: the event name and cover are the invitation.
    assert b"Private Reception" in locked.content

    unlock_page = client.get(
        reverse("events:portal-unlock", args=(event.slug,)),
        headers={"host": "alpha.localhost"},
    )
    assert unlock_page.status_code == 200
    assert b"Enter the four-digit PIN" in unlock_page.content

    unlocked = client.post(
        reverse("events:portal-unlock", args=(event.slug,)),
        {"pin": "0427"},
        headers={"host": "alpha.localhost"},
    )
    assert unlocked.status_code == 302
    cookie = unlocked.cookies[access_cookie_name(portal)]
    assert cookie["httponly"] and cookie["secure"] and cookie["samesite"] == "Lax"
    assert cookie["domain"] == "" and int(cookie["max-age"]) <= 86_400

    gallery = client.get(url, headers={"host": "alpha.localhost"})
    assert gallery.status_code == 200
    assert b"Private Reception" in gallery.content
    assert b"Alpha Photos" in gallery.content


def test_portal_is_tenant_and_lifecycle_scoped(tenants) -> None:
    alpha, _ = tenants
    event = make_event(
        alpha,
        name="Private Reception",
        state=EventState.PUBLISHED,
        expires_at=timezone.now() + timedelta(days=2),
    )
    create_portal(event)
    url = reverse("events:portal-gallery", args=(event.slug,))
    client = Client()

    assert client.get(url, headers={"host": "beta.localhost"}).status_code == 404
    assert (
        client.get("/portfolio/events/missing/", headers={"host": "alpha.localhost"}).status_code
        == 404
    )
    Event.objects.filter(pk=event.pk).update(state=EventState.ARCHIVED)
    assert client.get(url, headers={"host": "alpha.localhost"}).status_code == 404


def test_pin_rotation_and_expiry_invalidate_existing_access(tenants) -> None:
    alpha, _ = tenants
    actor = get_user_model().objects.create_superuser(username="admin", password="safe-pass")
    event = make_event(
        alpha,
        name="Private Reception",
        state=EventState.PUBLISHED,
        expires_at=timezone.now() + timedelta(days=2),
    )
    portal = create_portal(event)
    url = reverse("events:portal-gallery", args=(event.slug,))
    client = Client()
    client.post(
        reverse("events:portal-unlock", args=(event.slug,)),
        {"pin": "0427"},
        headers={"host": "alpha.localhost"},
    )

    rotate_portal_pin(event=event, actor=actor)
    locked = client.get(url, headers={"host": "alpha.localhost"})
    assert locked.status_code == 200 and b"View gallery" in locked.content
    unlock_page = client.get(
        reverse("events:portal-unlock", args=(event.slug,)),
        headers={"host": "alpha.localhost"},
    )
    assert b"Enter the four-digit PIN" in unlock_page.content

    PortalCapability.objects.filter(pk=portal.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    assert client.get(url, headers={"host": "alpha.localhost"}).status_code == 404


def test_tenth_wrong_portal_pin_is_rate_limited_without_logging_pin_or_ip(tenants) -> None:
    alpha, _ = tenants
    event = make_event(
        alpha,
        name="Private Reception",
        state=EventState.PUBLISHED,
        expires_at=timezone.now() + timedelta(days=2),
    )
    create_portal(event)
    client = Client(REMOTE_ADDR="203.0.113.8")
    responses = [
        client.post(
            reverse("events:portal-unlock", args=(event.slug,)),
            {"pin": "9999"},
            headers={"host": "alpha.localhost"},
        )
        for _ in range(10)
    ]

    assert [response.status_code for response in responses] == [200] * 9 + [429]
    assert int(responses[-1].headers["Retry-After"]) > 0
    assert RateLimitBucket.objects.get().failures == 10
    last_audit = event.audit_events.first()
    assert last_audit.result == AuditResult.RATE_LIMITED
    assert last_audit.client_hash != "203.0.113.8"
    assert "203.0.113.8" not in str(last_audit.__dict__)
    assert "9999" not in str(last_audit.__dict__)


def test_unknown_photographer_host_is_not_found() -> None:
    assert (
        Client().get(reverse("events:login"), headers={"host": "missing.localhost"}).status_code
        == 404
    )


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
