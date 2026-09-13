from openfotos_contracts import EventState, can_transition_event


def test_event_transitions_are_idempotent_and_reject_skips() -> None:
    assert can_transition_event(EventState.DRAFT, EventState.DRAFT)
    assert can_transition_event(EventState.DRAFT, EventState.UPLOADING)
    assert not can_transition_event(EventState.DRAFT, EventState.PUBLISHED)
