from datetime import timedelta

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import identify_hasher
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory
from django.utils import timezone

from openfotos_contracts import EventState
from openfotos_server.events.admin import EventAdmin, EventAdminForm
from openfotos_server.events.models import (
    AuditAction,
    Event,
    Photographer,
    PhotographerMembership,
)
from openfotos_server.events.services import change_event_pin, transition_event

pytestmark = pytest.mark.django_db


def make_event(photographer: Photographer, *, pin: str = "012345", expires=True) -> Event:
    event = Event(
        photographer=photographer,
        name="Reception",
        expires_at=timezone.now() + timedelta(days=30) if expires else None,
    )
    event.set_pin(pin)
    event.save()
    return event


def test_event_pin_is_six_ascii_digits_and_uses_argon2() -> None:
    photographer = Photographer.objects.create(slug="alpha", display_name="Alpha Photos")
    event = make_event(photographer)

    assert event.pin_hash != "012345"
    assert "012345" not in event.pin_hash
    assert identify_hasher(event.pin_hash).algorithm == "argon2"
    assert event.check_pin("012345")
    assert not event.check_pin("999999")
    assert len(event.public_token) >= 43

    for invalid_pin in ("12345", "1234567", "１２３４５６", "abc123"):
        with pytest.raises(ValidationError, match="six ASCII digits"):
            event.set_pin(invalid_pin)


def test_reserved_and_non_dns_photographer_slugs_are_rejected() -> None:
    with pytest.raises(ValidationError, match="reserved"):
        Photographer(slug="admin", display_name="Reserved").full_clean()
    with pytest.raises(ValidationError, match="lowercase DNS label"):
        Photographer(slug="not_valid", display_name="Invalid").full_clean()


def test_membership_is_unique_at_the_tenant_boundary() -> None:
    photographer = Photographer.objects.create(slug="alpha", display_name="Alpha Photos")
    user = get_user_model().objects.create_user(username="photographer", password="safe-pass")
    PhotographerMembership.objects.create(photographer=photographer, user=user)

    with pytest.raises(IntegrityError), transaction.atomic():
        PhotographerMembership.objects.create(photographer=photographer, user=user)


def test_event_transitions_are_legal_idempotent_and_audited() -> None:
    photographer = Photographer.objects.create(slug="alpha", display_name="Alpha Photos")
    actor = get_user_model().objects.create_superuser(username="admin", password="safe-pass")
    event = make_event(photographer)

    for target in (
        EventState.UPLOADING,
        EventState.PROCESSING,
        EventState.REVIEW,
        EventState.PUBLISHED,
    ):
        event = transition_event(event_id=event.id, target=target, actor=actor)

    assert event.state == EventState.PUBLISHED
    assert event.audit_events.filter(action=AuditAction.EVENT_STATE_CHANGED).count() == 4

    event = transition_event(event_id=event.id, target=EventState.PUBLISHED, actor=actor)
    assert event.state == EventState.PUBLISHED
    assert event.audit_events.filter(action=AuditAction.EVENT_STATE_CHANGED).count() == 4

    with pytest.raises(ValidationError, match="Cannot transition"):
        transition_event(event_id=event.id, target=EventState.UPLOADING, actor=actor)


def test_publication_requires_a_pin_and_future_expiry() -> None:
    photographer = Photographer.objects.create(slug="alpha", display_name="Alpha Photos")
    actor = get_user_model().objects.create_superuser(username="admin", password="safe-pass")
    event = make_event(photographer, expires=False)
    for target in (EventState.UPLOADING, EventState.PROCESSING, EventState.REVIEW):
        event = transition_event(event_id=event.id, target=target, actor=actor)

    with pytest.raises(ValidationError, match="future event expiry"):
        transition_event(event_id=event.id, target=EventState.PUBLISHED, actor=actor)

    event.expires_at = timezone.now() - timedelta(seconds=1)
    event.save(update_fields=("expires_at",))
    with pytest.raises(ValidationError, match="future event expiry"):
        transition_event(event_id=event.id, target=EventState.PUBLISHED, actor=actor)

    event.expires_at = timezone.now() + timedelta(days=1)
    event.pin_hash = ""
    event.save(update_fields=("expires_at", "pin_hash"))
    with pytest.raises(ValidationError, match="event PIN"):
        transition_event(event_id=event.id, target=EventState.PUBLISHED, actor=actor)


def test_pin_change_rotates_access_version_without_recording_the_pin() -> None:
    photographer = Photographer.objects.create(slug="alpha", display_name="Alpha Photos")
    actor = get_user_model().objects.create_superuser(username="admin", password="safe-pass")
    event = make_event(photographer)
    previous_version = event.visitor_access_version

    changed = change_event_pin(event_id=event.id, raw_pin="654321", actor=actor)

    assert changed.visitor_access_version != previous_version
    assert changed.check_pin("654321")
    audit = changed.audit_events.get(action=AuditAction.EVENT_PIN_CHANGED)
    assert audit.metadata == {}
    assert "654321" not in str(audit.__dict__)


def test_admin_can_provision_a_draft_event_with_a_write_only_pin() -> None:
    photographer = Photographer.objects.create(slug="alpha", display_name="Alpha Photos")
    actor = get_user_model().objects.create_superuser(username="admin", password="safe-pass")
    form = EventAdminForm(
        data={
            "photographer": photographer.id,
            "name": "Pilot Reception",
            "storage_limit_bytes": 25_000_000_000,
            "expires_at": (timezone.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),
            "pin": "123456",
        }
    )
    assert form.is_valid(), form.errors
    event = form.save(commit=False)
    request = RequestFactory().post("/admin/events/event/add/")
    request.user = actor

    EventAdmin(Event, AdminSite()).save_model(request, event, form, change=False)

    event.refresh_from_db()
    assert event.state == EventState.DRAFT
    assert event.check_pin("123456")
    assert event.pin_hash != "123456"
    assert event.audit_events.filter(action=AuditAction.EVENT_CREATED).exists()
