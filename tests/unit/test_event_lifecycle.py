import pytest

from openfotos_contracts import EventState, IngestionManifestState, IntakeState
from openfotos_server.events.event_lifecycle import (
    LifecycleViolation,
    TransitionFacts,
    state_for_contribution,
    state_for_derivative_readiness,
    state_for_finalized_ingestion,
    state_for_manual_transition,
    state_for_reopened_intake,
)


def _facts(**changes) -> TransitionFacts:
    values = {
        "intake_state": IntakeState.CLOSED,
        "intake_generation": 2,
        "manifest_exists": True,
        "manifest_state": IngestionManifestState.COMMITTED,
        "manifest_generation": 2,
        "pin_configured": True,
        "expiry_is_future": True,
        "derivatives_ready_generation": 2,
        "face_index_ready_generation": 2,
        "preview_policy_confirmed": True,
        "has_active_sub_event": True,
    }
    values.update(changes)
    return TransitionFacts(**values)


def test_ingestion_commands_follow_the_existing_lifecycle_path() -> None:
    state = state_for_contribution(EventState.DRAFT)
    assert state is EventState.UPLOADING

    state = state_for_finalized_ingestion(state)
    assert state is EventState.PROCESSING

    state = state_for_derivative_readiness(state, ready=True)
    assert state is EventState.REVIEW

    state = state_for_reopened_intake(state)
    assert state is EventState.UPLOADING


def test_derivative_readiness_only_moves_processing_and_review() -> None:
    assert state_for_derivative_readiness(EventState.PUBLISHED, ready=False) is EventState.PUBLISHED
    assert state_for_derivative_readiness(EventState.REVIEW, ready=False) is EventState.PROCESSING


def test_manual_publication_requires_all_current_generation_facts() -> None:
    assert (
        state_for_manual_transition(
            EventState.REVIEW,
            EventState.PUBLISHED,
            facts=_facts(),
        )
        is EventState.PUBLISHED
    )

    with pytest.raises(LifecycleViolation, match="active sub-event"):
        state_for_manual_transition(
            EventState.REVIEW,
            EventState.PUBLISHED,
            facts=_facts(has_active_sub_event=False),
        )

    with pytest.raises(LifecycleViolation, match="private gallery derivatives"):
        state_for_manual_transition(
            EventState.REVIEW,
            EventState.PUBLISHED,
            facts=_facts(derivatives_ready_generation=1),
        )

    with pytest.raises(LifecycleViolation, match="face analysis"):
        state_for_manual_transition(
            EventState.REVIEW,
            EventState.PUBLISHED,
            facts=_facts(face_index_ready_generation=1),
        )


def test_manual_processing_requires_the_current_committed_manifest() -> None:
    with pytest.raises(LifecycleViolation, match="Finalize the current ingestion manifest"):
        state_for_manual_transition(
            EventState.UPLOADING,
            EventState.PROCESSING,
            facts=_facts(manifest_generation=1),
        )
