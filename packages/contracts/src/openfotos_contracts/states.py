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


class IntakeState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class InstallationStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class ContributionState(StrEnum):
    RESERVED = "reserved"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


class UploadObjectState(StrEnum):
    RESERVED = "reserved"
    VERIFIED = "verified"
    FAILED = "failed"
    EXCLUDED = "excluded"


class IngestionManifestState(StrEnum):
    PREPARED = "prepared"
    COMMITTED = "committed"


_EVENT_TRANSITIONS = {
    EventState.DRAFT: {EventState.UPLOADING, EventState.CANCELLED},
    EventState.UPLOADING: {EventState.PROCESSING, EventState.FAILED},
    EventState.PROCESSING: {EventState.UPLOADING, EventState.REVIEW, EventState.FAILED},
    EventState.REVIEW: {EventState.UPLOADING, EventState.PUBLISHED, EventState.PROCESSING},
    EventState.PUBLISHED: {EventState.ARCHIVED, EventState.REVIEW},
    EventState.ARCHIVED: {EventState.PUBLISHED},
    EventState.FAILED: {EventState.UPLOADING, EventState.PROCESSING, EventState.CANCELLED},
    EventState.CANCELLED: set(),
}


def can_transition_event(current: EventState, target: EventState) -> bool:
    """Return whether the event state machine permits a transition."""
    return target == current or target in _EVENT_TRANSITIONS[current]
