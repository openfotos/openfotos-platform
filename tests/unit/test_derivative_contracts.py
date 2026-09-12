import base64
import hashlib
from uuid import uuid4

import pytest

from openfotos_contracts import (
    DERIVATIVE_PROFILE_ID,
    AssetDerivativesInput,
    ContractError,
    PreviewPolicyInput,
)


def _md5(content: bytes) -> str:
    return base64.b64encode(hashlib.md5(content, usedforsecurity=False).digest()).decode()


def test_preview_policy_requires_content_only_when_enabled() -> None:
    disabled = PreviewPolicyInput.from_dict(
        {
            "enabled": False,
            "template": "compact-bottom-right",
            "text": "",
            "logo_kind": "none",
            "mark_png_base64": "",
        }
    )
    assert not disabled.enabled

    with pytest.raises(ContractError) as raised:
        PreviewPolicyInput.from_dict(
            {
                "enabled": True,
                "template": "compact-bottom-right",
                "text": "",
                "logo_kind": "none",
                "mark_png_base64": "",
            }
        )
    assert raised.value.code == "empty_watermark"


def test_disabled_policy_rejects_hidden_branding_fields_and_non_string_text() -> None:
    for text in ("hidden", None):
        with pytest.raises(ContractError):
            PreviewPolicyInput.from_dict(
                {
                    "enabled": False,
                    "template": "compact-bottom-right",
                    "text": text,
                    "logo_kind": "none",
                    "mark_png_base64": "",
                }
            )


def test_derivative_contract_requires_exactly_preview_and_thumbnail() -> None:
    content = b"synthetic derivative"
    common = {
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_md5": _md5(content),
        "width": 100,
        "height": 60,
    }
    parsed = AssetDerivativesInput.from_dict(
        {
            "asset_id": str(uuid4()),
            "source_sha256": hashlib.sha256(b"source").hexdigest(),
            "policy_id": str(uuid4()),
            "profile_id": DERIVATIVE_PROFILE_ID,
            "captured_at": "2026-09-12T10:20:30",
            "objects": [
                {"variant": "previews", **common},
                {"variant": "thumbnails", **common},
            ],
        }
    )
    assert {item.variant.value for item in parsed.objects} == {"previews", "thumbnails"}

    with pytest.raises(ContractError) as raised:
        AssetDerivativesInput.from_dict(
            {
                "asset_id": str(uuid4()),
                "source_sha256": hashlib.sha256(b"source").hexdigest(),
                "policy_id": str(uuid4()),
                "profile_id": DERIVATIVE_PROFILE_ID,
                "captured_at": None,
                "objects": [
                    {"variant": "previews", **common},
                    {"variant": "previews", **common},
                ],
            }
        )
    assert raised.value.code == "invalid_derivative_count"
