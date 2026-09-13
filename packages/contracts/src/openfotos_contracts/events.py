"""Strict server-to-desktop event snapshot values."""

import re
from dataclasses import dataclass
from uuid import UUID

from .derivatives import (
    DERIVATIVE_PROFILE_ID,
    WATERMARK_RENDERER_ID,
    WatermarkLogoKind,
    WatermarkTemplate,
)
from .ingestion import ContractError, _positive_integer, _strict_fields, _uuid
from .states import EventState, IntakeState

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def _non_negative_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError("invalid_response", f"{field} must be a non-negative integer.")
    return value


def _text(value: object, *, field: str, maximum_length: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ContractError("invalid_response", f"{field} must be text.")
    if (required and not value.strip()) or len(value) > maximum_length:
        raise ContractError("invalid_response", f"{field} is invalid.")
    return value


@dataclass(frozen=True)
class PreviewPolicySnapshot:
    id: UUID
    enabled: bool
    template: WatermarkTemplate
    text: str
    logo_kind: WatermarkLogoKind
    renderer_id: str
    derivative_profile_id: str
    mark_sha256: str

    @classmethod
    def from_dict(cls, raw: object) -> "PreviewPolicySnapshot":
        value = _strict_fields(
            raw,
            {
                "id",
                "enabled",
                "template",
                "text",
                "logo_kind",
                "renderer_id",
                "derivative_profile_id",
                "mark_sha256",
            },
            context="Preview policy response",
        )
        if not isinstance(value["enabled"], bool):
            raise ContractError("invalid_response", "enabled must be a boolean.")
        try:
            template = WatermarkTemplate(value["template"])
            logo_kind = WatermarkLogoKind(value["logo_kind"])
        except (TypeError, ValueError) as exc:
            raise ContractError("invalid_response", "The preview policy is unsupported.") from exc
        text = _text(value["text"], field="text", maximum_length=60, required=False)
        renderer_id = _text(value["renderer_id"], field="renderer_id", maximum_length=100)
        derivative_profile_id = _text(
            value["derivative_profile_id"],
            field="derivative_profile_id",
            maximum_length=100,
        )
        mark_sha256 = _text(
            value["mark_sha256"], field="mark_sha256", maximum_length=64, required=False
        )
        if renderer_id != WATERMARK_RENDERER_ID or derivative_profile_id != DERIVATIVE_PROFILE_ID:
            raise ContractError(
                "profile_mismatch", "The gallery processing profile is unsupported."
            )
        if value["enabled"] and (
            not _SHA256_PATTERN.fullmatch(mark_sha256)
            or (logo_kind is WatermarkLogoKind.NONE and not text)
        ):
            raise ContractError("invalid_response", "The preview policy is invalid.")
        if not value["enabled"] and mark_sha256:
            raise ContractError("invalid_response", "A disabled preview policy cannot have a mark.")
        return cls(
            id=_uuid(value["id"], field="preview policy id"),
            enabled=value["enabled"],
            template=template,
            text=text,
            logo_kind=logo_kind,
            renderer_id=renderer_id,
            derivative_profile_id=derivative_profile_id,
            mark_sha256=mark_sha256,
        )

    def as_dict(self) -> dict:
        return {
            "id": str(self.id),
            "enabled": self.enabled,
            "template": self.template.value,
            "text": self.text,
            "logo_kind": self.logo_kind.value,
            "renderer_id": self.renderer_id,
            "derivative_profile_id": self.derivative_profile_id,
            "mark_sha256": self.mark_sha256,
        }


@dataclass(frozen=True)
class SubEventSnapshot:
    id: UUID
    name: str
    position: int

    @classmethod
    def from_dict(cls, raw: object) -> "SubEventSnapshot":
        value = _strict_fields(
            raw,
            {"id", "name", "position"},
            context="Sub-event response",
        )
        return cls(
            id=_uuid(value["id"], field="sub-event id"),
            name=_text(value["name"], field="sub-event name", maximum_length=120),
            position=_positive_integer(value["position"], field="sub-event position"),
        )

    def as_dict(self) -> dict:
        return {"id": str(self.id), "name": self.name, "position": self.position}


@dataclass(frozen=True)
class EventSnapshot:
    id: UUID
    name: str
    state: EventState
    storage_limit_bytes: int
    reserved_original_bytes: int
    verified_original_bytes: int
    remaining_original_bytes: int
    intake_state: IntakeState
    intake_generation: int
    processing_profile_id: str
    max_contribution_devices: int
    active_contribution_devices: int
    device_label: str
    sub_events: tuple[SubEventSnapshot, ...]
    preview_policy: PreviewPolicySnapshot | None

    @classmethod
    def from_dict(cls, raw: object) -> "EventSnapshot":
        value = _strict_fields(
            raw,
            {
                "id",
                "name",
                "state",
                "storage_limit_bytes",
                "reserved_original_bytes",
                "verified_original_bytes",
                "remaining_original_bytes",
                "intake_state",
                "intake_generation",
                "processing_profile_id",
                "max_contribution_devices",
                "active_contribution_devices",
                "device_label",
                "sub_events",
                "preview_policy",
            },
            context="Event response",
        )
        try:
            state = EventState(value["state"])
            intake_state = IntakeState(value["intake_state"])
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "invalid_response", "The event lifecycle value is unsupported."
            ) from exc
        storage_limit_bytes = _positive_integer(
            value["storage_limit_bytes"], field="storage_limit_bytes"
        )
        reserved_original_bytes = _non_negative_integer(
            value["reserved_original_bytes"], field="reserved_original_bytes"
        )
        verified_original_bytes = _non_negative_integer(
            value["verified_original_bytes"], field="verified_original_bytes"
        )
        remaining_original_bytes = _non_negative_integer(
            value["remaining_original_bytes"], field="remaining_original_bytes"
        )
        if (
            verified_original_bytes > reserved_original_bytes
            or remaining_original_bytes != storage_limit_bytes - reserved_original_bytes
        ):
            raise ContractError("invalid_response", "The event byte totals are inconsistent.")
        maximum_devices = _positive_integer(
            value["max_contribution_devices"], field="max_contribution_devices"
        )
        active_devices = _non_negative_integer(
            value["active_contribution_devices"], field="active_contribution_devices"
        )
        if active_devices > maximum_devices:
            raise ContractError("invalid_response", "The event device totals are inconsistent.")
        policy = (
            PreviewPolicySnapshot.from_dict(value["preview_policy"])
            if value["preview_policy"] is not None
            else None
        )
        sub_events_value = value["sub_events"]
        if not isinstance(sub_events_value, list):
            raise ContractError("invalid_response", "sub_events must be a list.")
        sub_events = tuple(SubEventSnapshot.from_dict(item) for item in sub_events_value)
        if len({item.id for item in sub_events}) != len(sub_events):
            raise ContractError("invalid_response", "Sub-event identifiers must be unique.")
        if tuple(sorted(sub_events, key=lambda item: (item.position, item.name, str(item.id)))) != sub_events:
            raise ContractError("invalid_response", "Sub-events are not in canonical order.")
        return cls(
            id=_uuid(value["id"], field="event id"),
            name=_text(value["name"], field="name", maximum_length=200),
            state=state,
            storage_limit_bytes=storage_limit_bytes,
            reserved_original_bytes=reserved_original_bytes,
            verified_original_bytes=verified_original_bytes,
            remaining_original_bytes=remaining_original_bytes,
            intake_state=intake_state,
            intake_generation=_positive_integer(
                value["intake_generation"], field="intake_generation"
            ),
            processing_profile_id=_text(
                value["processing_profile_id"],
                field="processing_profile_id",
                maximum_length=100,
            ),
            max_contribution_devices=maximum_devices,
            active_contribution_devices=active_devices,
            device_label=_text(
                value["device_label"], field="device_label", maximum_length=100, required=False
            ),
            sub_events=sub_events,
            preview_policy=policy,
        )

    def as_dict(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "state": self.state.value,
            "storage_limit_bytes": self.storage_limit_bytes,
            "reserved_original_bytes": self.reserved_original_bytes,
            "verified_original_bytes": self.verified_original_bytes,
            "remaining_original_bytes": self.remaining_original_bytes,
            "intake_state": self.intake_state.value,
            "intake_generation": self.intake_generation,
            "processing_profile_id": self.processing_profile_id,
            "max_contribution_devices": self.max_contribution_devices,
            "active_contribution_devices": self.active_contribution_devices,
            "device_label": self.device_label,
            "sub_events": [sub_event.as_dict() for sub_event in self.sub_events],
            "preview_policy": self.preview_policy.as_dict() if self.preview_policy else None,
        }
