from uuid import uuid4

import pytest

from openfotos_contracts import (
    DERIVATIVE_PROFILE_ID,
    WATERMARK_RENDERER_ID,
    ContractError,
    EventSnapshot,
)


def _event_response() -> dict:
    return {
        "id": str(uuid4()),
        "name": "Reception",
        "state": "processing",
        "role": "lead",
        "storage_limit_bytes": 10_000,
        "reserved_original_bytes": 4_000,
        "verified_original_bytes": 3_000,
        "remaining_original_bytes": 6_000,
        "intake_state": "closed",
        "intake_generation": 2,
        "processing_profile_id": "pilot-profile-v1",
        "max_contribution_devices": 10,
        "active_contribution_devices": 3,
        "device_label": "Lead workstation",
        "preview_policy": {
            "id": str(uuid4()),
            "enabled": True,
            "template": "compact-bottom-right",
            "text": "Studio",
            "logo_kind": "none",
            "renderer_id": WATERMARK_RENDERER_ID,
            "derivative_profile_id": DERIVATIVE_PROFILE_ID,
            "mark_sha256": "a" * 64,
        },
    }


def test_event_snapshot_round_trips_the_versioned_response() -> None:
    value = _event_response()

    assert EventSnapshot.from_dict(value).as_dict() == value


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("verified_original_bytes", 4_001),
        ("remaining_original_bytes", 5_999),
        ("active_contribution_devices", 11),
    ],
)
def test_event_snapshot_rejects_inconsistent_aggregates(field: str, value: int) -> None:
    response = _event_response()
    response[field] = value

    with pytest.raises(ContractError) as raised:
        EventSnapshot.from_dict(response)

    assert raised.value.code == "invalid_response"


def test_event_snapshot_rejects_an_unsupported_gallery_profile() -> None:
    response = _event_response()
    response["preview_policy"]["derivative_profile_id"] = "future-profile"

    with pytest.raises(ContractError) as raised:
        EventSnapshot.from_dict(response)

    assert raised.value.code == "profile_mismatch"
