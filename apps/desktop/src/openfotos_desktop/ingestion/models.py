"""Values persisted by the desktop inventory boundary."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from openfotos_contracts import AssetVariant, WatermarkLogoKind, WatermarkTemplate
from openfotos_vision import ACCEPTED_FACE_MODEL_CONTRACT


class BatchState(StrEnum):
    DRAFT = "draft"
    SCANNING = "scanning"
    PAUSED = "paused"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    RESERVED = "reserved"
    UPLOADING = "uploading"
    COMPLETE = "complete"
    NOT_INCLUDED = "not_included"


class SelectionKind(StrEnum):
    FILE = "file"
    FOLDER = "folder"


class InventoryStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    BLOCKING = "blocking"


class LocalUploadState(StrEnum):
    PENDING = "pending"
    UPLOADING = "uploading"
    VERIFIED = "verified"
    FAILED = "failed"
    EXCLUDED = "excluded"


class LocalFaceState(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    INDEXED = "indexed"
    NO_USABLE_FACE = "no_usable_face"
    FAILED = "failed"
    CONFLICT = "conflict"
    EXCLUDED = "excluded"


class RejectionReason(StrEnum):
    UNSUPPORTED_EXTENSION = "unsupported_extension"
    CONTENT_TYPE_MISMATCH = "content_type_mismatch"
    INVALID_JPEG = "invalid_jpeg"
    INVALID_IMAGE = "invalid_image"
    FILE_TOO_LARGE = "file_too_large"
    IMAGE_TOO_LARGE = "image_too_large"
    SYMBOLIC_LINK = "symbolic_link"
    UNSUPPORTED_FILE_TYPE = "unsupported_file_type"
    SOURCE_MISSING = "source_missing"
    SOURCE_UNREADABLE = "source_unreadable"
    FILE_CHANGED_DURING_SCAN = "file_changed_during_scan"


class ScanIssueReason(StrEnum):
    SELECTION_MISSING = "selection_missing"
    SELECTION_TYPE_CHANGED = "selection_type_changed"
    DIRECTORY_UNREADABLE = "directory_unreadable"
    FILE_UNREADABLE = "file_unreadable"
    NEW_FILE_AFTER_FREEZE = "new_file_after_freeze"


@dataclass(frozen=True)
class ScanLimits:
    max_file_bytes: int = 100 * 1024 * 1024
    max_pixels: int = 120_000_000

    def __post_init__(self) -> None:
        if self.max_file_bytes <= 0 or self.max_pixels <= 0:
            raise ValueError("Image limits must be positive.")


@dataclass(frozen=True)
class PreviewPolicyCache:
    id: UUID
    enabled: bool
    template: WatermarkTemplate
    text: str
    logo_kind: WatermarkLogoKind
    renderer_id: str
    derivative_profile_id: str
    mark_sha256: str


@dataclass(frozen=True)
class SubEventCache:
    id: UUID
    name: str
    position: int


@dataclass(frozen=True)
class EventCache:
    id: UUID
    name: str
    storage_limit_bytes: int
    processing_profile_id: str
    face_model_id: str = ACCEPTED_FACE_MODEL_CONTRACT.model.id
    face_index_ready: bool = False
    server_url: str = ""
    reserved_original_bytes: int = 0
    verified_original_bytes: int = 0
    intake_state: str = "open"
    intake_generation: int = 1
    device_label: str = ""
    sub_events: tuple[SubEventCache, ...] = ()
    preview_policy: PreviewPolicyCache | None = None


@dataclass(frozen=True)
class ContributionBatch:
    id: UUID
    event_id: UUID
    installation_id: UUID
    sub_event_id: UUID
    state: BatchState
    label: str
    scan_generation: int
    frozen: bool


@dataclass(frozen=True)
class SourceSelection:
    id: UUID
    batch_id: UUID
    kind: SelectionKind
    source_path: Path
    normalized_path: str


@dataclass(frozen=True)
class FileSnapshot:
    size_bytes: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class ValidationResult:
    status: InventoryStatus
    reason: RejectionReason | None
    content_type: str | None
    sha256: str | None
    content_md5: str | None
    width: int | None
    height: int | None


@dataclass(frozen=True)
class InventoryItem:
    id: UUID
    batch_id: UUID
    selection_id: UUID
    relative_path: Path
    source_path: Path
    normalized_path: str
    basename: str
    snapshot: FileSnapshot
    status: InventoryStatus
    reason: RejectionReason | None
    content_type: str | None
    sha256: str | None
    content_md5: str | None
    width: int | None
    height: int | None
    last_seen_generation: int


@dataclass(frozen=True)
class ScanIssue:
    reason: ScanIssueReason
    source_path: Path
    blocking: bool


@dataclass(frozen=True)
class ScanProgress:
    processed_count: int
    accepted_count: int
    rejected_count: int
    current_path: Path


@dataclass(frozen=True)
class ScanSummary:
    batch_id: UUID
    state: BatchState
    accepted_count: int
    accepted_bytes: int
    rejected_count: int
    rejected_bytes: int
    blocking_item_count: int
    blocking_issue_count: int
    warning_count: int
    changed_count: int
    validated_count: int
    reused_count: int

    @property
    def can_approve(self) -> bool:
        return self.accepted_count > 0 and not (
            self.blocking_item_count or self.blocking_issue_count
        )


@dataclass(frozen=True)
class UploadCheckpoint:
    item_id: UUID
    state: LocalUploadState
    attempt_count: int
    last_error_code: str | None


@dataclass(frozen=True)
class DerivativeCheckpoint:
    item_id: UUID
    variant: AssetVariant
    state: LocalUploadState
    attempt_count: int
    last_error_code: str | None


@dataclass(frozen=True)
class FaceAnalysisCheckpoint:
    item_id: UUID
    state: LocalFaceState
    attempt_count: int
    last_error_code: str | None
    detected_face_count: int
    usable_face_count: int
