"""Transactional event lifecycle and visitor-access changes."""

from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone

from openfotos_contracts import (
    EventState,
    IngestionManifestState,
    IntakeState,
    can_transition_event,
)

from .audit import record_audit
from .models import AuditAction, AuditResult, Event, generate_event_token


@transaction.atomic
def transition_event(
    *,
    event_id,
    target: EventState,
    actor,
    request: HttpRequest | None = None,
) -> Event:
    """Move one event through an allowed transition and audit the change atomically."""
    event = Event.objects.select_for_update().select_related("photographer").get(pk=event_id)
    current = EventState(event.state)
    if target == current:
        return event
    if not can_transition_event(current, target):
        raise ValidationError(f"Cannot transition an event from {current} to {target}.")
    if target is EventState.UPLOADING and (
        current in {EventState.PROCESSING, EventState.REVIEW}
        or event.current_ingestion_manifest_id is not None
    ):
        raise ValidationError(
            "Reopen intake to create a new ingestion generation before returning to Uploading."
        )
    if target is EventState.PROCESSING:
        manifest = event.current_ingestion_manifest
        if (
            event.intake_state != IntakeState.CLOSED.value
            or manifest is None
            or manifest.state != IngestionManifestState.COMMITTED.value
            or manifest.generation != event.intake_generation
        ):
            raise ValidationError("Finalize the current ingestion manifest before Processing.")
    if target is EventState.PUBLISHED:
        if not event.pin_hash:
            raise ValidationError("Set an event PIN before publication.")
        if event.expires_at is None or event.expires_at <= timezone.now():
            raise ValidationError("Set a future event expiry before publication.")
        manifest = event.current_ingestion_manifest
        if (
            manifest is None
            or manifest.state != IngestionManifestState.COMMITTED.value
            or manifest.generation != event.intake_generation
        ):
            raise ValidationError("Finalize the current ingestion manifest before publication.")
        if event.derivatives_ready_generation != event.intake_generation:
            raise ValidationError("Complete private gallery derivatives before publication.")

    event.state = target
    event.save(update_fields=("state", "updated_at"))
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=actor,
        action=AuditAction.EVENT_STATE_CHANGED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"from": current.value, "to": target.value},
    )
    return event


@transaction.atomic
def change_event_pin(
    *,
    event_id,
    raw_pin: str,
    actor,
    request: HttpRequest | None = None,
) -> Event:
    event = Event.objects.select_for_update().select_related("photographer").get(pk=event_id)
    event.set_pin(raw_pin)
    event.visitor_access_version = uuid4()
    event.save(update_fields=("pin_hash", "visitor_access_version", "updated_at"))
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=actor,
        action=AuditAction.EVENT_PIN_CHANGED,
        result=AuditResult.SUCCEEDED,
        request=request,
    )
    return event


@transaction.atomic
def revoke_event_sessions(
    *,
    event_id,
    actor,
    request: HttpRequest | None = None,
) -> Event:
    event = Event.objects.select_for_update().select_related("photographer").get(pk=event_id)
    event.visitor_access_version = uuid4()
    event.save(update_fields=("visitor_access_version", "updated_at"))
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=actor,
        action=AuditAction.EVENT_ACCESS_REVOKED,
        result=AuditResult.SUCCEEDED,
        request=request,
    )
    return event


@transaction.atomic
def rotate_event_token(
    *,
    event_id,
    actor,
    request: HttpRequest | None = None,
) -> Event:
    """Break existing public links and their path-scoped visitor cookies."""
    event = Event.objects.select_for_update().select_related("photographer").get(pk=event_id)
    event.public_token = generate_event_token()
    event.visitor_access_version = uuid4()
    event.save(update_fields=("public_token", "visitor_access_version", "updated_at"))
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=actor,
        action=AuditAction.EVENT_TOKEN_CHANGED,
        result=AuditResult.SUCCEEDED,
        request=request,
    )
    return event
