"""Generate server-owned object keys from validated UUIDs."""

from enum import StrEnum
from uuid import UUID


class AssetVariant(StrEnum):
    ORIGINAL = "originals"
    PREVIEW = "previews"
    THUMBNAIL = "thumbnails"


def _canonical_uuid(value: UUID | str) -> str:
    return str(value if isinstance(value, UUID) else UUID(value))


def asset_key(event_id: UUID | str, asset_id: UUID | str, variant: AssetVariant) -> str:
    """Return the only accepted object-key shape for an event asset."""
    return f"events/{_canonical_uuid(event_id)}/{variant.value}/{_canonical_uuid(asset_id)}.jpg"


def event_manifest_key(event_id: UUID | str) -> str:
    """Return the final ingestion manifest key for an event."""
    return f"events/{_canonical_uuid(event_id)}/manifests/final.json"
