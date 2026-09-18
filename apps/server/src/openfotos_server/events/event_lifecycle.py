"""Pure event lifecycle decisions shared by server commands."""

from dataclasses import dataclass

from openfotos_contracts import (
    EventState,
    IngestionManifestState,
    IntakeState,
    can_transition_event,
)


class LifecycleViolation(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TransitionFacts:
    intake_state: IntakeState
    intake_generation: int
    manifest_exists: bool
    manifest_state: IngestionManifestState | None
    manifest_generation: int | None
    expiry_is_future: bool
    derivatives_ready_generation: int | None
    face_index_ready_generation: int | None
    preview_policy_confirmed: bool
    has_active_sub_event: bool

    @property
    def current_manifest_is_committed(self) -> bool:
        return (
            self.manifest_exists
            and self.manifest_state is IngestionManifestState.COMMITTED
            and self.manifest_generation == self.intake_generation
        )


def state_for_contribution(current: EventState) -> EventState:
    if current not in {EventState.DRAFT, EventState.UPLOADING}:
        raise LifecycleViolation("event_not_uploading", "The event is not accepting uploads.")
    return EventState.UPLOADING


def state_for_reopened_intake(current: EventState) -> EventState:
    if current in {EventState.PUBLISHED, EventState.ARCHIVED, EventState.CANCELLED}:
        raise LifecycleViolation(
            "event_not_reopenable", "Published or closed events cannot reopen."
        )
    return EventState.UPLOADING


def state_for_finalized_ingestion(current: EventState) -> EventState:
    if current is not EventState.UPLOADING:
        raise LifecycleViolation("event_not_uploading", "The event cannot be finalized now.")
    return EventState.PROCESSING


def state_for_derivative_readiness(current: EventState, *, ready: bool) -> EventState:
    if ready and current is EventState.PROCESSING:
        return EventState.REVIEW
    if not ready and current is EventState.REVIEW:
        return EventState.PROCESSING
    return current


def state_for_manual_transition(
    current: EventState,
    target: EventState,
    *,
    facts: TransitionFacts,
) -> EventState:
    if target is current:
        return current
    if not can_transition_event(current, target):
        raise LifecycleViolation(
            "invalid_transition",
            f"Cannot transition an event from {current} to {target}.",
        )
    if target is EventState.UPLOADING and (
        current in {EventState.PROCESSING, EventState.REVIEW} or facts.manifest_exists
    ):
        raise LifecycleViolation(
            "reopen_required",
            "Reopen intake to create a new ingestion generation before returning to Uploading.",
        )
    if target is EventState.PROCESSING and (
        facts.intake_state is not IntakeState.CLOSED or not facts.current_manifest_is_committed
    ):
        raise LifecycleViolation(
            "finalization_required",
            "Finalize the current ingestion manifest before Processing.",
        )
    if target is EventState.PUBLISHED:
        if not facts.has_active_sub_event:
            raise LifecycleViolation(
                "sub_event_required",
                "Create and retain at least one active sub-event before publication.",
            )
        if not facts.expiry_is_future:
            raise LifecycleViolation(
                "future_expiry_required", "Set a future event expiry before publication."
            )
        if not facts.current_manifest_is_committed:
            raise LifecycleViolation(
                "finalization_required",
                "Finalize the current ingestion manifest before publication.",
            )
        if facts.derivatives_ready_generation != facts.intake_generation:
            raise LifecycleViolation(
                "derivatives_required",
                "Complete private gallery derivatives before publication.",
            )
        if facts.face_index_ready_generation != facts.intake_generation:
            raise LifecycleViolation(
                "face_index_required",
                "Complete face analysis for every visible photo before publication.",
            )
        if not facts.preview_policy_confirmed:
            raise LifecycleViolation(
                "preview_policy_required",
                "Confirm the event preview settings before publication.",
            )
    return target
