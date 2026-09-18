"""Transactional event lifecycle changes."""

from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone

from openfotos_contracts import (
    EventState,
    IngestionManifestState,
    IntakeState,
)

from .audit import record_audit
from .event_lifecycle import LifecycleViolation, TransitionFacts, state_for_manual_transition
from .models import AuditAction, AuditResult, Event


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
    if target is current:
        return event
    manifest = (
        event.current_ingestion_manifest
        if target in {EventState.PROCESSING, EventState.PUBLISHED}
        else None
    )
    try:
        next_state = state_for_manual_transition(
            current,
            target,
            facts=TransitionFacts(
                intake_state=IntakeState(event.intake_state),
                intake_generation=event.intake_generation,
                manifest_exists=event.current_ingestion_manifest_id is not None,
                manifest_state=(
                    IngestionManifestState(manifest.state) if manifest is not None else None
                ),
                manifest_generation=manifest.generation if manifest is not None else None,
                expiry_is_future=event.expires_at is not None and event.expires_at > timezone.now(),
                derivatives_ready_generation=event.derivatives_ready_generation,
                face_index_ready_generation=event.face_index_ready_generation,
                preview_policy_confirmed=(
                    hasattr(event, "preview_policy") if target is EventState.PUBLISHED else False
                ),
                has_active_sub_event=(
                    event.sub_events.filter(is_archived=False).exists()
                    if target is EventState.PUBLISHED
                    else False
                ),
            ),
        )
    except LifecycleViolation as exc:
        raise ValidationError(str(exc)) from exc
    if next_state is current:
        return event

    event.state = next_state
    update_fields = ["state", "updated_at"]
    if current is EventState.PUBLISHED and target is EventState.REVIEW:
        event.share_access_version = uuid4()
        update_fields.append("share_access_version")
    event.save(update_fields=update_fields)
    record_audit(
        photographer=event.photographer,
        event=event,
        actor=actor,
        action=AuditAction.EVENT_STATE_CHANGED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"from": current.value, "to": next_state.value},
    )
    return event
