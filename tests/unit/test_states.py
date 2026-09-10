from openfotos_contracts import (
    AssetState,
    EventState,
    can_transition_asset,
    can_transition_event,
)


def test_event_transitions_are_idempotent_and_reject_skips() -> None:
    assert can_transition_event(EventState.DRAFT, EventState.DRAFT)
    assert can_transition_event(EventState.DRAFT, EventState.UPLOADING)
    assert not can_transition_event(EventState.DRAFT, EventState.PUBLISHED)


def test_asset_transitions_move_one_step_and_can_retry_after_failure() -> None:
    assert can_transition_asset(AssetState.VALIDATED, AssetState.DERIVED)
    assert not can_transition_asset(AssetState.VALIDATED, AssetState.UPLOADED)
    assert can_transition_asset(AssetState.UPLOADED, AssetState.FAILED)
    assert can_transition_asset(AssetState.FAILED, AssetState.UPLOADED)
