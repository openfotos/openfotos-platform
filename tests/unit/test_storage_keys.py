from uuid import UUID

import pytest

from openfotos_storage import AssetVariant, asset_key, event_manifest_key

EVENT_ID = UUID("12345678-1234-5678-1234-567812345678")
ASSET_ID = UUID("87654321-4321-8765-4321-876543218765")


def test_asset_key_is_event_scoped_and_uses_uuid_not_filename() -> None:
    assert asset_key(EVENT_ID, ASSET_ID, AssetVariant.PREVIEW) == (
        "events/12345678-1234-5678-1234-567812345678/"
        "previews/87654321-4321-8765-4321-876543218765.jpg"
    )


def test_manifest_key_is_event_scoped() -> None:
    assert event_manifest_key(EVENT_ID) == (
        "events/12345678-1234-5678-1234-567812345678/manifests/final.json"
    )


def test_invalid_identifiers_cannot_escape_the_event_prefix() -> None:
    with pytest.raises(ValueError):
        asset_key("../../another-event", ASSET_ID, AssetVariant.ORIGINAL)
