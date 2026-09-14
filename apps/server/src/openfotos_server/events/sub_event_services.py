"""Photographer-managed photo categorization within one main event."""

from uuid import UUID

from django.db import transaction

from openfotos_contracts import EventState

from .audit import record_audit
from .derivative_services import refresh_derivative_readiness
from .ingestion_services import IngestionError
from .models import (
    AuditAction,
    AuditResult,
    ContributionBatch,
    Event,
    SubEvent,
)

MAX_SUB_EVENTS = 50
_LOCKED_STATES = {
    EventState.PUBLISHED.value,
    EventState.ARCHIVED.value,
    EventState.CANCELLED.value,
}


def _normalized_name(value: str) -> str:
    name = value.strip()
    if not 1 <= len(name) <= 120 or any(ord(character) < 32 for character in name):
        raise IngestionError(
            "invalid_sub_event_name",
            "Use a sub-event name containing 1 to 120 visible characters.",
        )
    return name


def _validated_position(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 32_767:
        raise IngestionError(
            "invalid_sub_event_position",
            "Use a sub-event display position from 1 to 32767.",
        )
    return value


def _require_editable(event: Event) -> None:
    if event.state in _LOCKED_STATES:
        raise IngestionError(
            "event_structure_locked",
            "Return the main event to an unpublished state before changing sub-events.",
        )


def _require_unique_name(event: Event, name: str, *, excluding: UUID | None = None) -> None:
    matches = SubEvent.objects.filter(event=event, name__iexact=name)
    if excluding is not None:
        matches = matches.exclude(pk=excluding)
    if matches.exists():
        raise IngestionError(
            "sub_event_name_conflict", "Sub-event names must be unique in the main event."
        )


@transaction.atomic
def create_sub_event(*, event: Event, name: str, position: int, actor, request=None) -> SubEvent:
    locked_event = Event.objects.select_for_update().get(pk=event.pk)
    _require_editable(locked_event)
    if SubEvent.objects.filter(event=locked_event).count() >= MAX_SUB_EVENTS:
        raise IngestionError(
            "sub_event_limit", f"A main event may contain at most {MAX_SUB_EVENTS} sub-events."
        )
    normalized_name = _normalized_name(name)
    _require_unique_name(locked_event, normalized_name)
    sub_event = SubEvent.objects.create(
        event=locked_event,
        name=normalized_name,
        position=_validated_position(position),
    )
    record_audit(
        photographer=locked_event.photographer,
        event=locked_event,
        actor=actor,
        action=AuditAction.SUB_EVENT_CREATED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"sub_event_id": str(sub_event.id)},
    )
    return sub_event


@transaction.atomic
def update_sub_event(
    *, sub_event: SubEvent, name: str, position: int, actor, request=None
) -> SubEvent:
    locked_event = Event.objects.select_for_update().get(pk=sub_event.event_id)
    _require_editable(locked_event)
    locked = SubEvent.objects.select_for_update().get(pk=sub_event.pk, event=locked_event)
    normalized_name = _normalized_name(name)
    validated_position = _validated_position(position)
    _require_unique_name(locked_event, normalized_name, excluding=locked.id)
    if locked.name == normalized_name and locked.position == validated_position:
        return locked
    previous = {"name": locked.name, "position": locked.position}
    locked.name = normalized_name
    locked.position = validated_position
    locked.save(update_fields=("name", "position", "updated_at"))
    record_audit(
        photographer=locked_event.photographer,
        event=locked_event,
        actor=actor,
        action=AuditAction.SUB_EVENT_CHANGED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={
            "sub_event_id": str(locked.id),
            "from": previous,
            "to": {"name": locked.name, "position": locked.position},
        },
    )
    return locked


@transaction.atomic
def set_sub_event_archived(*, sub_event: SubEvent, archived: bool, actor, request=None) -> SubEvent:
    locked_event = Event.objects.select_for_update().get(pk=sub_event.event_id)
    _require_editable(locked_event)
    locked = SubEvent.objects.select_for_update().get(pk=sub_event.pk, event=locked_event)
    if locked.is_archived == archived:
        return locked
    locked.is_archived = archived
    locked.save(update_fields=("is_archived", "updated_at"))
    locked_event.derivatives_ready_generation = None
    locked_event.save(update_fields=("derivatives_ready_generation", "updated_at"))
    record_audit(
        photographer=locked_event.photographer,
        event=locked_event,
        actor=actor,
        action=(AuditAction.SUB_EVENT_ARCHIVED if archived else AuditAction.SUB_EVENT_RESTORED),
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={"sub_event_id": str(locked.id)},
    )
    transaction.on_commit(lambda: refresh_derivative_readiness(locked_event.id))
    return locked


@transaction.atomic
def reassign_contribution(
    *, event: Event, batch_id: UUID, sub_event_id: UUID, actor, request=None
) -> ContributionBatch:
    locked_event = Event.objects.select_for_update().get(pk=event.pk)
    _require_editable(locked_event)
    try:
        target = SubEvent.objects.get(
            pk=sub_event_id,
            event=locked_event,
            is_archived=False,
        )
        batch = (
            ContributionBatch.objects.select_for_update()
            .select_related("sub_event")
            .get(pk=batch_id, installation__event=locked_event)
        )
    except SubEvent.DoesNotExist as exc:
        raise IngestionError(
            "sub_event_not_found", "Select an active sub-event in this event."
        ) from exc
    except ContributionBatch.DoesNotExist as exc:
        raise IngestionError("batch_not_found", "The contribution is unavailable.") from exc
    if batch.sub_event_id == target.id:
        return batch
    previous_id = batch.sub_event_id
    batch.sub_event = target
    batch.save(update_fields=("sub_event", "updated_at"))
    record_audit(
        photographer=locked_event.photographer,
        event=locked_event,
        actor=actor,
        event_installation=batch.installation,
        action=AuditAction.CONTRIBUTION_REASSIGNED,
        result=AuditResult.SUCCEEDED,
        request=request,
        metadata={
            "batch_id": str(batch.id),
            "from_sub_event_id": str(previous_id),
            "to_sub_event_id": str(target.id),
        },
    )
    return batch
