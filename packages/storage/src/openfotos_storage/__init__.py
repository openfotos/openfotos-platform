"""Private object-storage boundaries shared by OpenFotos components."""

from .keys import AssetVariant, asset_key, event_manifest_key, ingestion_manifest_key

__all__ = [
    "AssetVariant",
    "asset_key",
    "event_manifest_key",
    "ingestion_manifest_key",
]
