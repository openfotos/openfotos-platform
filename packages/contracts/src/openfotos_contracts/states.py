"""Stable lifecycle values shared across process boundaries."""

from enum import StrEnum


class EventState(StrEnum):
    DRAFT = "draft"
    UPLOADING = "uploading"
    PROCESSING = "processing"
    REVIEW = "review"
    PUBLISHED = "published"
    ARCHIVED = "archived"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AssetState(StrEnum):
    DISCOVERED = "discovered"
    VALIDATED = "validated"
    DERIVED = "derived"
    UPLOADED = "uploaded"
    ANALYZED = "analyzed"
    COMMITTED = "committed"
    FAILED = "failed"


_EVENT_TRANSITIONS = {
    EventState.DRAFT: {EventState.UPLOADING, EventState.CANCELLED},
    EventState.UPLOADING: {EventState.PROCESSING, EventState.FAILED},
    EventState.PROCESSING: {EventState.REVIEW, EventState.FAILED},
    EventState.REVIEW: {EventState.PUBLISHED, EventState.PROCESSING},
    EventState.PUBLISHED: {EventState.ARCHIVED, EventState.REVIEW},
    EventState.ARCHIVED: {EventState.PUBLISHED},
    EventState.FAILED: {EventState.UPLOADING, EventState.PROCESSING, EventState.CANCELLED},
    EventState.CANCELLED: set(),
}

_ASSET_FORWARD_STATES = (
    AssetState.DISCOVERED,
    AssetState.VALIDATED,
    AssetState.DERIVED,
    AssetState.UPLOADED,
    AssetState.ANALYZED,
    AssetState.COMMITTED,
)


def can_transition_event(current: EventState, target: EventState) -> bool:
    """Return whether the event state machine permits a transition."""
    return target == current or target in _EVENT_TRANSITIONS[current]


def can_transition_asset(current: AssetState, target: AssetState) -> bool:
    """Allow idempotent retries, failure, and one-step forward asset progress."""
    if target == current or target is AssetState.FAILED:
        return True
    if current is AssetState.FAILED:
        return target in _ASSET_FORWARD_STATES[:-1]
    try:
        return _ASSET_FORWARD_STATES.index(target) == _ASSET_FORWARD_STATES.index(current) + 1
    except ValueError:
        return False
