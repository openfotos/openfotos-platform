from uuid import UUID

import pytest

from openfotos_contracts import AssetVariant
from openfotos_storage import AssetVariant as StorageAssetVariant
from openfotos_storage import asset_key, event_manifest_key

EVENT_ID = UUID("12345678-1234-5678-1234-567812345678")
ASSET_ID = UUID("87654321-4321-8765-4321-876543218765")


def test_storage_reexports_the_canonical_asset_variant() -> None:
    assert StorageAssetVariant is AssetVariant


def test_asset_key_is_event_scoped_and_uses_uuid_not_filename() -> None:
    assert asset_key(EVENT_ID, ASSET_ID, AssetVariant.PREVIEW) == (
        "events/12345678-1234-5678-1234-567812345678/"
        "previews/87654321-4321-8765-4321-876543218765.jpg"
    )
    assert asset_key(
        EVENT_ID,
        ASSET_ID,
        AssetVariant.ORIGINAL,
        content_type="image/webp",
    ).endswith("/originals/87654321-4321-8765-4321-876543218765.webp")


def test_manifest_key_is_event_scoped() -> None:
    assert event_manifest_key(EVENT_ID) == (
        "events/12345678-1234-5678-1234-567812345678/manifests/final.json"
    )


def test_invalid_identifiers_cannot_escape_the_event_prefix() -> None:
    with pytest.raises(ValueError):
        asset_key("../../another-event", ASSET_ID, AssetVariant.ORIGINAL)
