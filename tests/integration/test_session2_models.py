from datetime import timedelta

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import identify_hasher
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, override_settings
from django.utils import timezone

from openfotos_contracts import EventState, IngestionManifestState, IntakeState
from openfotos_server.events.admin import EventAdmin, EventAdminForm
from openfotos_server.events.models import (
    AuditAction,
    Event,
    IngestionManifest,
    Photographer,
    PhotographerMembership,
    PreviewPolicy,
    SubEvent,
)
from openfotos_server.events.services import change_event_pin, transition_event

pytestmark = pytest.mark.django_db


def make_event(photographer: Photographer, *, pin: str = "0123", expires=True) -> Event:
    event = Event(
        photographer=photographer,
        name="Reception",
        expires_at=timezone.now() + timedelta(days=30) if expires else None,
    )
    event.set_pin(pin)
    event.save()
    SubEvent.objects.create(event=event, name="Reception", position=1)
    return event


def attach_committed_manifest(event: Event) -> IngestionManifest:
    manifest = IngestionManifest.objects.create(
        event=event,
        generation=event.intake_generation,
        state=IngestionManifestState.COMMITTED.value,
        object_key=f"events/{event.id}/manifests/generation-{event.intake_generation:06d}.json",
        content_sha256="a" * 64,
        document={},
        asset_count=1,
        original_bytes=1,
    )
    event.current_ingestion_manifest = manifest
    event.intake_state = IntakeState.CLOSED.value
    event.save(update_fields=("current_ingestion_manifest", "intake_state"))
    return manifest


def test_event_pin_is_four_ascii_digits_peppered_and_uses_argon2() -> None:
    photographer = Photographer.objects.create(slug="alpha", display_name="Alpha Photos")
    event = make_event(photographer)

    assert event.pin_hash != "0123"
    assert "0123" not in event.pin_hash
    assert identify_hasher(event.pin_hash).algorithm == "argon2"
    assert event.check_pin("0123")
    assert not event.check_pin("9999")
    assert len(event.public_token) >= 43

    with override_settings(EVENT_PIN_PEPPER="another-independent-pepper-value-1234"):
        assert not event.check_pin("0123")

    for invalid_pin in ("123", "12345", "１２３４", "ab12"):
        with pytest.raises(ValidationError, match="four ASCII digits"):
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

    event = transition_event(event_id=event.id, target=EventState.UPLOADING, actor=actor)
    with pytest.raises(ValidationError, match="Finalize the current ingestion manifest"):
        transition_event(event_id=event.id, target=EventState.PROCESSING, actor=actor)
    attach_committed_manifest(event)
    event = transition_event(event_id=event.id, target=EventState.PROCESSING, actor=actor)
    event = transition_event(event_id=event.id, target=EventState.REVIEW, actor=actor)

    with pytest.raises(ValidationError, match="Reopen intake"):
        transition_event(event_id=event.id, target=EventState.UPLOADING, actor=actor)
    with pytest.raises(ValidationError, match="derivatives"):
        transition_event(event_id=event.id, target=EventState.PUBLISHED, actor=actor)
    event.derivatives_ready_generation = event.intake_generation
    event.face_index_ready_generation = event.intake_generation
    event.save(update_fields=("derivatives_ready_generation", "face_index_ready_generation"))
    PreviewPolicy.objects.create(event=event, enabled=False, confirmed_by=actor)

    event = transition_event(event_id=event.id, target=EventState.PUBLISHED, actor=actor)

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
    event = transition_event(event_id=event.id, target=EventState.UPLOADING, actor=actor)
    attach_committed_manifest(event)
    event = transition_event(event_id=event.id, target=EventState.PROCESSING, actor=actor)
    event = transition_event(event_id=event.id, target=EventState.REVIEW, actor=actor)

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

    changed = change_event_pin(event_id=event.id, raw_pin="6543", actor=actor)

    assert changed.visitor_access_version != previous_version
    assert changed.check_pin("6543")
    audit = changed.audit_events.get(action=AuditAction.EVENT_PIN_CHANGED)
    assert audit.metadata == {}
    assert "6543" not in str(audit.__dict__)


def test_admin_provisions_a_draft_event_with_a_generated_write_only_pin(monkeypatch) -> None:
    photographer = Photographer.objects.create(slug="alpha", display_name="Alpha Photos")
    actor = get_user_model().objects.create_superuser(username="admin", password="safe-pass")
    form = EventAdminForm(
        data={
            "photographer": photographer.id,
            "name": "Pilot Reception",
            "storage_limit_bytes": 25_000_000_000,
            "max_contribution_devices": 10,
            "processing_profile_id": "pilot-profile-v1",
            "expires_at": (timezone.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    assert form.is_valid(), form.errors
    event = form.save(commit=False)
    request = RequestFactory().post("/admin/events/event/add/")
    request.user = actor

    messages = []
    event_admin = EventAdmin(Event, AdminSite())
    monkeypatch.setattr(
        event_admin,
        "message_user",
        lambda _request, message: messages.append(message),
    )
    event_admin.save_model(request, event, form, change=False)

    event.refresh_from_db()
    assert event.state == EventState.DRAFT
    generated_pin = messages[0].split(": ", 1)[1].split(".", 1)[0]
    assert len(generated_pin) == 4 and generated_pin.isascii() and generated_pin.isdigit()
    assert event.check_pin(generated_pin)
    assert generated_pin not in event.pin_hash
    assert event.audit_events.filter(action=AuditAction.EVENT_CREATED).exists()
