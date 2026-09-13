"""Generate server-owned object keys from validated UUIDs."""

from uuid import UUID

from openfotos_contracts import AssetVariant


def _canonical_uuid(value: UUID | str) -> str:
    return str(value if isinstance(value, UUID) else UUID(value))


def asset_key(event_id: UUID | str, asset_id: UUID | str, variant: AssetVariant) -> str:
    """Return the only accepted object-key shape for an event asset."""
    return f"events/{_canonical_uuid(event_id)}/{variant.value}/{_canonical_uuid(asset_id)}.jpg"


def event_manifest_key(event_id: UUID | str) -> str:
    """Return the publication-time final ingestion manifest key for an event."""
    return f"events/{_canonical_uuid(event_id)}/manifests/final.json"


def ingestion_manifest_key(event_id: UUID | str, generation: int) -> str:
    """Return an immutable key for one finalized intake generation."""
    if generation <= 0:
        raise ValueError("Manifest generations must be positive.")
    return f"events/{_canonical_uuid(event_id)}/manifests/generation-{generation:06d}.json"


def preview_policy_mark_key(event_id: UUID | str, policy_id: UUID | str) -> str:
    """Return the immutable private mark used by every contributor desktop."""
    return (
        f"events/{_canonical_uuid(event_id)}/branding/"
        f"preview-policy-{_canonical_uuid(policy_id)}.png"
    )
