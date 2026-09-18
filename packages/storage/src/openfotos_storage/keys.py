"""Generate server-owned object keys from validated UUIDs."""

from uuid import UUID

from openfotos_contracts import ORIGINAL_EXTENSION_BY_CONTENT_TYPE, AssetVariant


def _canonical_uuid(value: UUID | str) -> str:
    return str(value if isinstance(value, UUID) else UUID(value))


def asset_key(
    event_id: UUID | str,
    asset_id: UUID | str,
    variant: AssetVariant,
    *,
    content_type: str = "image/jpeg",
) -> str:
    """Return the only accepted object-key shape for an event asset."""
    extension = ".jpg"
    if variant is AssetVariant.ORIGINAL:
        try:
            extension = ORIGINAL_EXTENSION_BY_CONTENT_TYPE[content_type]
        except KeyError as exc:
            raise ValueError("Unsupported original content type.") from exc
    return (
        f"events/{_canonical_uuid(event_id)}/{variant.value}/{_canonical_uuid(asset_id)}{extension}"
    )


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


def event_cover_key(event_id: UUID | str, sha256: str) -> str:
    if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        raise ValueError("A lowercase SHA-256 digest is required.")
    return f"events/{_canonical_uuid(event_id)}/portfolio/cover-{sha256}.jpg"


def photographer_logo_key(photographer_id: UUID | str, sha256: str) -> str:
    if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        raise ValueError("A lowercase SHA-256 digest is required.")
    return f"photographers/{_canonical_uuid(photographer_id)}/portfolio/logo-{sha256}.png"
