"""Durable, event-scoped SQLite checkpoints for desktop inventory."""

import os
import shutil
import sqlite3
from collections.abc import Iterable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from uuid import UUID, uuid4

from openfotos_contracts import (
    DERIVATIVE_VARIANTS,
    AssetVariant,
    WatermarkLogoKind,
    WatermarkTemplate,
)

from .models import (
    BatchState,
    ContributionBatch,
    DerivativeCheckpoint,
    EventCache,
    FileSnapshot,
    InventoryItem,
    InventoryStatus,
    LocalUploadState,
    RejectionReason,
    ScanIssue,
    ScanIssueReason,
    ScanSummary,
    SelectionKind,
    SourceSelection,
    SubEventCache,
    UploadCheckpoint,
    ValidationResult,
)

_DERIVATIVE_VARIANT_SQL = ", ".join(f"'{variant.value}'" for variant in DERIVATIVE_VARIANTS)


def _derivative_variant(value: AssetVariant | str) -> AssetVariant:
    try:
        variant = AssetVariant(value)
    except ValueError as exc:
        raise ValueError("Derivative variants are previews or thumbnails.") from exc
    if variant not in DERIVATIVE_VARIANTS:
        raise ValueError("Derivative variants are previews or thumbnails.")
    return variant


_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    storage_limit_bytes INTEGER NOT NULL CHECK (storage_limit_bytes > 0),
    processing_profile_id TEXT NOT NULL,
    server_url TEXT NOT NULL DEFAULT '',
    reserved_original_bytes INTEGER NOT NULL DEFAULT 0 CHECK (reserved_original_bytes >= 0),
    verified_original_bytes INTEGER NOT NULL DEFAULT 0 CHECK (verified_original_bytes >= 0),
    intake_state TEXT NOT NULL DEFAULT 'open',
    intake_generation INTEGER NOT NULL DEFAULT 1 CHECK (intake_generation > 0),
    device_label TEXT NOT NULL DEFAULT '',
    preview_policy_id TEXT,
    preview_watermark_enabled INTEGER CHECK (preview_watermark_enabled IN (0, 1)),
    preview_template TEXT,
    preview_text TEXT NOT NULL DEFAULT '',
    preview_logo_kind TEXT,
    preview_renderer_id TEXT,
    derivative_profile_id TEXT,
    preview_mark_sha256 TEXT NOT NULL DEFAULT '',
    cached_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sub_events (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position > 0),
    is_active INTEGER NOT NULL CHECK (is_active IN (0, 1)),
    UNIQUE (event_id, name)
);

CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    installation_id TEXT NOT NULL,
    sub_event_id TEXT NOT NULL REFERENCES sub_events(id) ON DELETE RESTRICT,
    state TEXT NOT NULL,
    label TEXT NOT NULL,
    scan_generation INTEGER NOT NULL DEFAULT 0,
    frozen_at TEXT,
    approved_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS selections (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    source_path TEXT NOT NULL,
    normalized_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (batch_id, normalized_path)
);

CREATE TABLE IF NOT EXISTS inventory_items (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    selection_id TEXT NOT NULL REFERENCES selections(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    source_path TEXT NOT NULL,
    normalized_path TEXT NOT NULL,
    basename TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    modified_ns INTEGER NOT NULL,
    changed_ns INTEGER NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    content_type TEXT,
    sha256 TEXT,
    content_md5 TEXT,
    width INTEGER,
    height INTEGER,
    last_seen_generation INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (batch_id, normalized_path)
);

CREATE INDEX IF NOT EXISTS inventory_batch_status_idx
ON inventory_items(batch_id, status);

CREATE TABLE IF NOT EXISTS scan_issues (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    selection_id TEXT REFERENCES selections(id) ON DELETE CASCADE,
    generation INTEGER NOT NULL,
    reason TEXT NOT NULL,
    source_path TEXT NOT NULL,
    blocking INTEGER NOT NULL CHECK (blocking IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS scan_issue_batch_generation_idx
ON scan_issues(batch_id, generation);

CREATE TABLE IF NOT EXISTS upload_checkpoints (
    item_id TEXT PRIMARY KEY REFERENCES inventory_items(id) ON DELETE CASCADE,
    state TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error_code TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derivative_checkpoints (
    item_id TEXT NOT NULL REFERENCES inventory_items(id) ON DELETE CASCADE,
    variant TEXT NOT NULL CHECK (variant IN ({_DERIVATIVE_VARIANT_SQL})),
    state TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error_code TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (item_id, variant)
);
"""
_SCHEMA_VERSION = 5


def normalize_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _now() -> str:
    return datetime.now(UTC).isoformat()


class CheckpointStore(AbstractContextManager["CheckpointStore"]):
    """Own one installation's local state; credentials never enter this database."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._connection = sqlite3.connect(self.database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        try:
            with self._connection:
                self._connection.execute("PRAGMA foreign_keys = ON")
                self._connection.execute("PRAGMA journal_mode = WAL")
                self._connection.execute("PRAGMA synchronous = FULL")
                schema_version = self._connection.execute("PRAGMA user_version").fetchone()[0]
                if schema_version > _SCHEMA_VERSION:
                    raise ValueError(
                        "This checkpoint was created by a newer OpenFotos desktop version."
                    )
                if schema_version == 0:
                    self._connection.executescript(_SCHEMA)
                    self._connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                elif schema_version == 1:
                    self._migrate_version_1()
                    self._migrate_version_3()
                    self._migrate_version_4()
                elif schema_version == 2:
                    self._migrate_version_2()
                    self._migrate_version_3()
                    self._migrate_version_4()
                elif schema_version == 3:
                    self._migrate_version_3()
                    self._migrate_version_4()
                elif schema_version == 4:
                    self._migrate_version_4()
        except Exception:
            self._connection.close()
            raise
        if os.name != "nt":
            self.database_path.chmod(0o600)
        self._installation_id = self._load_or_create_installation_id()

    def _migrate_version_1(self) -> None:
        self._connection.executescript(
            """
            ALTER TABLE events ADD COLUMN server_url TEXT NOT NULL DEFAULT '';
            ALTER TABLE events ADD COLUMN role TEXT NOT NULL DEFAULT 'uploader';
            ALTER TABLE events ADD COLUMN reserved_original_bytes INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE events ADD COLUMN verified_original_bytes INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE events ADD COLUMN intake_state TEXT NOT NULL DEFAULT 'open';
            ALTER TABLE events ADD COLUMN intake_generation INTEGER NOT NULL DEFAULT 1;
            ALTER TABLE inventory_items ADD COLUMN content_md5 TEXT;
            CREATE TABLE upload_checkpoints (
                item_id TEXT PRIMARY KEY REFERENCES inventory_items(id) ON DELETE CASCADE,
                state TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                last_error_code TEXT,
                completed_at TEXT,
                updated_at TEXT NOT NULL
            );
            ALTER TABLE events ADD COLUMN device_label TEXT NOT NULL DEFAULT '';
            PRAGMA user_version = 3;
            """
        )

    def _migrate_version_2(self) -> None:
        self._connection.executescript(
            """
            ALTER TABLE events ADD COLUMN device_label TEXT NOT NULL DEFAULT '';
            PRAGMA user_version = 3;
            """
        )

    def _migrate_version_3(self) -> None:
        self._connection.executescript(
            """
            ALTER TABLE events ADD COLUMN preview_policy_id TEXT;
            ALTER TABLE events ADD COLUMN preview_watermark_enabled INTEGER;
            ALTER TABLE events ADD COLUMN preview_template TEXT;
            ALTER TABLE events ADD COLUMN preview_text TEXT NOT NULL DEFAULT '';
            ALTER TABLE events ADD COLUMN preview_logo_kind TEXT;
            ALTER TABLE events ADD COLUMN preview_renderer_id TEXT;
            ALTER TABLE events ADD COLUMN derivative_profile_id TEXT;
            ALTER TABLE events ADD COLUMN preview_mark_sha256 TEXT NOT NULL DEFAULT '';
            CREATE TABLE derivative_checkpoints (
                item_id TEXT NOT NULL REFERENCES inventory_items(id) ON DELETE CASCADE,
                variant TEXT NOT NULL CHECK (variant IN ('previews', 'thumbnails')),
                state TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                last_error_code TEXT,
                completed_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (item_id, variant)
            );
            INSERT INTO derivative_checkpoints(item_id, variant, state, updated_at)
            SELECT item_id, 'previews', 'pending', updated_at FROM upload_checkpoints;
            INSERT INTO derivative_checkpoints(item_id, variant, state, updated_at)
            SELECT item_id, 'thumbnails', 'pending', updated_at FROM upload_checkpoints;
            PRAGMA user_version = 4;
            """
        )

    def _migrate_version_4(self) -> None:
        self._connection.executescript(
            """
            ALTER TABLE events DROP COLUMN role;
            ALTER TABLE batches RENAME COLUMN device_id TO installation_id;
            ALTER TABLE batches ADD COLUMN sub_event_id TEXT;
            CREATE TABLE sub_events (
                id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                position INTEGER NOT NULL CHECK (position > 0),
                is_active INTEGER NOT NULL CHECK (is_active IN (0, 1)),
                UNIQUE (event_id, name)
            );
            PRAGMA user_version = 5;
            """
        )

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @property
    def installation_id(self) -> UUID:
        return self._installation_id

    def _load_or_create_installation_id(self) -> UUID:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT value FROM metadata WHERE key = 'installation_id'"
            ).fetchone()
            if row is not None:
                return UUID(row["value"])
            installation_id = uuid4()
            self._connection.execute(
                "INSERT INTO metadata(key, value) VALUES ('installation_id', ?)",
                (str(installation_id),),
            )
            return installation_id

    def cache_event(self, event: EventCache) -> None:
        if not event.name.strip() or not event.processing_profile_id.strip():
            raise ValueError("Cached event name and processing profile are required.")
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO events(
                    id, name, storage_limit_bytes, processing_profile_id, server_url,
                    reserved_original_bytes, verified_original_bytes, intake_state,
                    intake_generation, device_label, preview_policy_id,
                    preview_watermark_enabled, preview_template, preview_text,
                    preview_logo_kind, preview_renderer_id, derivative_profile_id,
                    preview_mark_sha256, cached_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    storage_limit_bytes = excluded.storage_limit_bytes,
                    processing_profile_id = excluded.processing_profile_id,
                    server_url = excluded.server_url,
                    reserved_original_bytes = excluded.reserved_original_bytes,
                    verified_original_bytes = excluded.verified_original_bytes,
                    intake_state = excluded.intake_state,
                    intake_generation = excluded.intake_generation,
                    device_label = CASE
                        WHEN excluded.device_label = '' THEN events.device_label
                        ELSE excluded.device_label
                    END,
                    preview_policy_id = excluded.preview_policy_id,
                    preview_watermark_enabled = excluded.preview_watermark_enabled,
                    preview_template = excluded.preview_template,
                    preview_text = excluded.preview_text,
                    preview_logo_kind = excluded.preview_logo_kind,
                    preview_renderer_id = excluded.preview_renderer_id,
                    derivative_profile_id = excluded.derivative_profile_id,
                    preview_mark_sha256 = excluded.preview_mark_sha256,
                    cached_at = excluded.cached_at
                """,
                (
                    str(event.id),
                    event.name.strip(),
                    event.storage_limit_bytes,
                    event.processing_profile_id.strip(),
                    event.server_url.strip(),
                    event.reserved_original_bytes,
                    event.verified_original_bytes,
                    event.intake_state,
                    event.intake_generation,
                    event.device_label.strip(),
                    str(event.preview_policy.id) if event.preview_policy else None,
                    int(event.preview_policy.enabled) if event.preview_policy else None,
                    event.preview_policy.template.value if event.preview_policy else None,
                    event.preview_policy.text if event.preview_policy else "",
                    event.preview_policy.logo_kind.value if event.preview_policy else None,
                    event.preview_policy.renderer_id if event.preview_policy else None,
                    event.preview_policy.derivative_profile_id if event.preview_policy else None,
                    event.preview_policy.mark_sha256 if event.preview_policy else "",
                    _now(),
                ),
            )
            self._connection.execute(
                "UPDATE sub_events SET is_active = 0 WHERE event_id = ?",
                (str(event.id),),
            )
            for sub_event in event.sub_events:
                self._connection.execute(
                    """
                    INSERT INTO sub_events(id, event_id, name, position, is_active)
                    VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(id) DO UPDATE SET
                        event_id = excluded.event_id,
                        name = excluded.name,
                        position = excluded.position,
                        is_active = 1
                    """,
                    (
                        str(sub_event.id),
                        str(event.id),
                        sub_event.name,
                        sub_event.position,
                    ),
                )

    def get_event(self, event_id: UUID) -> EventCache:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM events WHERE id = ?", (str(event_id),)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown cached event {event_id}.")
        return self._event_from_row(row)

    def list_events(self) -> list[EventCache]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM events ORDER BY name, id").fetchall()
        return [self._event_from_row(row) for row in rows]

    def _event_from_row(self, row: sqlite3.Row) -> EventCache:
        from .models import PreviewPolicyCache

        policy = None
        if row["preview_policy_id"]:
            policy = PreviewPolicyCache(
                id=UUID(row["preview_policy_id"]),
                enabled=bool(row["preview_watermark_enabled"]),
                template=WatermarkTemplate(row["preview_template"]),
                text=row["preview_text"],
                logo_kind=WatermarkLogoKind(row["preview_logo_kind"]),
                renderer_id=row["preview_renderer_id"],
                derivative_profile_id=row["derivative_profile_id"],
                mark_sha256=row["preview_mark_sha256"],
            )
        return EventCache(
            id=UUID(row["id"]),
            name=row["name"],
            storage_limit_bytes=row["storage_limit_bytes"],
            processing_profile_id=row["processing_profile_id"],
            server_url=row["server_url"],
            reserved_original_bytes=row["reserved_original_bytes"],
            verified_original_bytes=row["verified_original_bytes"],
            intake_state=row["intake_state"],
            intake_generation=row["intake_generation"],
            device_label=row["device_label"],
            sub_events=self._sub_events_for(UUID(row["id"])),
            preview_policy=policy,
        )

    def _sub_events_for(self, event_id: UUID) -> tuple[SubEventCache, ...]:
        rows = self._connection.execute(
            """
            SELECT id, name, position FROM sub_events
            WHERE event_id = ? AND is_active = 1
            ORDER BY position, name, id
            """,
            (str(event_id),),
        ).fetchall()
        return tuple(
            SubEventCache(id=UUID(row["id"]), name=row["name"], position=row["position"])
            for row in rows
        )

    def delete_local_event(self, event_id: UUID) -> None:
        batch_ids = [batch.id for batch in self.list_batches(event_id)]
        with self._lock, self._connection:
            cursor = self._connection.execute("DELETE FROM events WHERE id = ?", (str(event_id),))
        if cursor.rowcount == 0:
            raise KeyError(f"Unknown cached event {event_id}.")
        for batch_id in batch_ids:
            self.cleanup_derivative_cache(batch_id)

    def create_batch(self, event_id: UUID, sub_event_id: UUID, *, label: str = "") -> UUID:
        self.get_event(event_id)
        sub_event = self._connection.execute(
            "SELECT id FROM sub_events WHERE id = ? AND event_id = ? AND is_active = 1",
            (str(sub_event_id), str(event_id)),
        ).fetchone()
        if sub_event is None:
            raise ValueError("Select an active sub-event before creating a contribution.")
        batch_id = uuid4()
        timestamp = _now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO batches(
                    id, event_id, installation_id, sub_event_id, state, label, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(batch_id),
                    str(event_id),
                    str(self.installation_id),
                    str(sub_event_id),
                    BatchState.DRAFT.value,
                    label.strip(),
                    timestamp,
                    timestamp,
                ),
            )
        return batch_id

    def get_batch(self, batch_id: UUID) -> ContributionBatch:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM batches WHERE id = ?", (str(batch_id),)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown contribution batch {batch_id}.")
        return self._batch_from_row(row)

    def list_batches(self, event_id: UUID) -> list[ContributionBatch]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM batches WHERE event_id = ? ORDER BY created_at, id",
                (str(event_id),),
            ).fetchall()
        return [self._batch_from_row(row) for row in rows]

    @staticmethod
    def _batch_from_row(row: sqlite3.Row) -> ContributionBatch:
        return ContributionBatch(
            id=UUID(row["id"]),
            event_id=UUID(row["event_id"]),
            installation_id=UUID(row["installation_id"]),
            sub_event_id=UUID(row["sub_event_id"]),
            state=BatchState(row["state"]),
            label=row["label"],
            scan_generation=row["scan_generation"],
            frozen=row["frozen_at"] is not None,
        )

    def add_folder(self, batch_id: UUID, path: Path) -> UUID:
        return self._add_selection(batch_id, Path(path), SelectionKind.FOLDER)

    def add_files(self, batch_id: UUID, paths: Iterable[Path]) -> list[UUID]:
        return [self._add_selection(batch_id, Path(path), SelectionKind.FILE) for path in paths]

    def _add_selection(self, batch_id: UUID, path: Path, kind: SelectionKind) -> UUID:
        batch = self.get_batch(batch_id)
        if batch.frozen:
            raise ValueError("This contribution batch is frozen; create a new batch for additions.")
        normalized = normalize_path(path)
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT id FROM selections WHERE batch_id = ? AND normalized_path = ?",
                (str(batch_id), normalized),
            ).fetchone()
            if existing is not None:
                return UUID(existing["id"])
            selection_id = uuid4()
            self._connection.execute(
                """
                INSERT INTO selections(
                    id, batch_id, kind, source_path, normalized_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(selection_id),
                    str(batch_id),
                    kind.value,
                    os.path.abspath(path),
                    normalized,
                    _now(),
                ),
            )
        return selection_id

    def get_selection(self, selection_id: UUID) -> SourceSelection:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM selections WHERE id = ?", (str(selection_id),)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown source selection {selection_id}.")
        return self._selection_from_row(row)

    def remove_selection(self, selection_id: UUID) -> None:
        selection = self.get_selection(selection_id)
        if self.get_batch(selection.batch_id).frozen:
            raise ValueError("This contribution batch is frozen; its selections cannot be removed.")
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM selections WHERE id = ?", (str(selection_id),))

    def list_selections(self, batch_id: UUID) -> list[SourceSelection]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM selections WHERE batch_id = ? ORDER BY created_at, id",
                (str(batch_id),),
            ).fetchall()
        return [self._selection_from_row(row) for row in rows]

    @staticmethod
    def _selection_from_row(row: sqlite3.Row) -> SourceSelection:
        return SourceSelection(
            id=UUID(row["id"]),
            batch_id=UUID(row["batch_id"]),
            kind=SelectionKind(row["kind"]),
            source_path=Path(row["source_path"]),
            normalized_path=row["normalized_path"],
        )

    def start_scan(self, batch_id: UUID) -> tuple[int, bool]:
        batch = self.get_batch(batch_id)
        if batch.state in {BatchState.RESERVED, BatchState.UPLOADING, BatchState.COMPLETE}:
            raise ValueError("A server-reserved contribution cannot be changed or rescanned.")
        generation = batch.scan_generation + 1
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM scan_issues WHERE batch_id = ?", (str(batch_id),))
            self._connection.execute(
                """
                UPDATE batches
                SET state = ?, scan_generation = ?, updated_at = ?
                WHERE id = ?
                """,
                (BatchState.SCANNING.value, generation, _now(), str(batch_id)),
            )
        return generation, batch.frozen

    def pause_scan(self, batch_id: UUID) -> None:
        self._set_batch_state(batch_id, BatchState.PAUSED)

    def finish_scan(self, batch_id: UUID, *, requires_review: bool) -> None:
        target = BatchState.NEEDS_REVIEW if requires_review else BatchState.APPROVED
        self._set_batch_state(batch_id, target)

    def _set_batch_state(self, batch_id: UUID, state: BatchState) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE batches SET state = ?, updated_at = ? WHERE id = ?",
                (state.value, _now(), str(batch_id)),
            )
        if cursor.rowcount == 0:
            raise KeyError(f"Unknown contribution batch {batch_id}.")

    def get_item_by_path(self, batch_id: UUID, normalized_path: str) -> InventoryItem | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM inventory_items
                WHERE batch_id = ? AND normalized_path = ?
                """,
                (str(batch_id), normalized_path),
            ).fetchone()
        return self._item_from_row(row) if row is not None else None

    def get_item(self, item_id: UUID) -> InventoryItem:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM inventory_items WHERE id = ?", (str(item_id),)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown inventory item {item_id}.")
        return self._item_from_row(row)

    def list_items(
        self, batch_id: UUID, *, selection_id: UUID | None = None
    ) -> list[InventoryItem]:
        query = "SELECT * FROM inventory_items WHERE batch_id = ?"
        parameters: list[str] = [str(batch_id)]
        if selection_id is not None:
            query += " AND selection_id = ?"
            parameters.append(str(selection_id))
        query += " ORDER BY normalized_path, id"
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [self._item_from_row(row) for row in rows]

    @staticmethod
    def _item_from_row(row: sqlite3.Row) -> InventoryItem:
        reason = RejectionReason(row["reason"]) if row["reason"] else None
        return InventoryItem(
            id=UUID(row["id"]),
            batch_id=UUID(row["batch_id"]),
            selection_id=UUID(row["selection_id"]),
            relative_path=Path(row["relative_path"]),
            source_path=Path(row["source_path"]),
            normalized_path=row["normalized_path"],
            basename=row["basename"],
            snapshot=FileSnapshot(
                size_bytes=row["size_bytes"],
                modified_ns=row["modified_ns"],
                changed_ns=row["changed_ns"],
            ),
            status=InventoryStatus(row["status"]),
            reason=reason,
            content_type=row["content_type"],
            sha256=row["sha256"],
            content_md5=row["content_md5"],
            width=row["width"],
            height=row["height"],
            last_seen_generation=row["last_seen_generation"],
        )

    def upsert_item(
        self,
        *,
        batch_id: UUID,
        selection_id: UUID,
        relative_path: Path,
        source_path: Path,
        snapshot: FileSnapshot,
        result: ValidationResult,
        generation: int,
    ) -> UUID:
        normalized = normalize_path(source_path)
        existing = self.get_item_by_path(batch_id, normalized)
        item_id = existing.id if existing else uuid4()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO inventory_items(
                    id, batch_id, selection_id, relative_path, source_path, normalized_path,
                    basename, size_bytes, modified_ns, changed_ns, status, reason,
                    content_type, sha256, content_md5, width, height,
                    last_seen_generation, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(batch_id, normalized_path) DO UPDATE SET
                    selection_id = excluded.selection_id,
                    relative_path = excluded.relative_path,
                    source_path = excluded.source_path,
                    basename = excluded.basename,
                    size_bytes = excluded.size_bytes,
                    modified_ns = excluded.modified_ns,
                    changed_ns = excluded.changed_ns,
                    status = excluded.status,
                    reason = excluded.reason,
                    content_type = excluded.content_type,
                    sha256 = excluded.sha256,
                    content_md5 = excluded.content_md5,
                    width = excluded.width,
                    height = excluded.height,
                    last_seen_generation = excluded.last_seen_generation,
                    updated_at = excluded.updated_at
                """,
                (
                    str(item_id),
                    str(batch_id),
                    str(selection_id),
                    os.fspath(relative_path),
                    os.path.abspath(source_path),
                    normalized,
                    source_path.name,
                    snapshot.size_bytes,
                    snapshot.modified_ns,
                    snapshot.changed_ns,
                    result.status.value,
                    result.reason.value if result.reason else None,
                    result.content_type,
                    result.sha256,
                    result.content_md5,
                    result.width,
                    result.height,
                    generation,
                    _now(),
                ),
            )
        return item_id

    def set_content_md5(self, item_id: UUID, content_md5: str) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE inventory_items SET content_md5 = ?, updated_at = ?
                WHERE id = ? AND status = ? AND sha256 IS NOT NULL
                """,
                (
                    content_md5,
                    _now(),
                    str(item_id),
                    InventoryStatus.ACCEPTED.value,
                ),
            )
        if cursor.rowcount == 0:
            raise KeyError(f"Unknown accepted inventory item {item_id}.")

    def mark_batch_reserved(self, batch_id: UUID) -> None:
        batch = self.get_batch(batch_id)
        if batch.state not in {BatchState.APPROVED, BatchState.RESERVED, BatchState.UPLOADING}:
            raise ValueError("Only an approved contribution can be reserved for upload.")
        timestamp = _now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE batches
                SET state = CASE WHEN state = ? THEN ? ELSE state END, updated_at = ?
                WHERE id = ?
                """,
                (
                    BatchState.APPROVED.value,
                    BatchState.RESERVED.value,
                    timestamp,
                    str(batch_id),
                ),
            )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO upload_checkpoints(
                    item_id, state, attempt_count, updated_at
                )
                SELECT id, ?, 0, ? FROM inventory_items
                WHERE batch_id = ? AND status = ?
                """,
                (
                    LocalUploadState.PENDING.value,
                    timestamp,
                    str(batch_id),
                    InventoryStatus.ACCEPTED.value,
                ),
            )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO derivative_checkpoints(
                    item_id, variant, state, attempt_count, updated_at
                )
                SELECT id, variants.variant, ?, 0, ? FROM inventory_items
                CROSS JOIN (
                    SELECT ? AS variant
                    UNION ALL SELECT ?
                ) AS variants
                WHERE batch_id = ? AND status = ?
                """,
                (
                    LocalUploadState.PENDING.value,
                    timestamp,
                    AssetVariant.PREVIEW.value,
                    AssetVariant.THUMBNAIL.value,
                    str(batch_id),
                    InventoryStatus.ACCEPTED.value,
                ),
            )

    def list_upload_checkpoints(self, batch_id: UUID) -> list[UploadCheckpoint]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT upload_checkpoints.* FROM upload_checkpoints
                JOIN inventory_items ON inventory_items.id = upload_checkpoints.item_id
                WHERE inventory_items.batch_id = ?
                ORDER BY inventory_items.normalized_path, inventory_items.id
                """,
                (str(batch_id),),
            ).fetchall()
        return [
            UploadCheckpoint(
                item_id=UUID(row["item_id"]),
                state=LocalUploadState(row["state"]),
                attempt_count=row["attempt_count"],
                last_error_code=row["last_error_code"],
            )
            for row in rows
        ]

    def get_upload_checkpoint(self, item_id: UUID) -> UploadCheckpoint:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM upload_checkpoints WHERE item_id = ?", (str(item_id),)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown upload checkpoint {item_id}.")
        return UploadCheckpoint(
            item_id=UUID(row["item_id"]),
            state=LocalUploadState(row["state"]),
            attempt_count=row["attempt_count"],
            last_error_code=row["last_error_code"],
        )

    def mark_upload_started(self, item_id: UUID) -> None:
        self._update_upload(
            item_id,
            state=LocalUploadState.UPLOADING,
            increment_attempt=True,
        )
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE batches SET state = ?, updated_at = ?
                WHERE id = (SELECT batch_id FROM inventory_items WHERE id = ?)
                  AND state != ?
                """,
                (
                    BatchState.UPLOADING.value,
                    _now(),
                    str(item_id),
                    BatchState.COMPLETE.value,
                ),
            )

    def mark_upload_failed(self, item_id: UUID, error_code: str) -> None:
        self._update_upload(
            item_id,
            state=LocalUploadState.FAILED,
            error_code=error_code,
        )

    def mark_upload_verified(self, item_id: UUID) -> None:
        self._update_upload(item_id, state=LocalUploadState.VERIFIED)
        self._refresh_batch_upload_state(item_id)

    def mark_upload_excluded(self, item_id: UUID) -> None:
        self._update_upload(item_id, state=LocalUploadState.EXCLUDED)
        self._refresh_batch_upload_state(item_id)

    def _refresh_batch_upload_state(self, item_id: UUID) -> None:
        with self._lock, self._connection:
            batch_row = self._connection.execute(
                """
                SELECT inventory_items.batch_id FROM inventory_items
                WHERE inventory_items.id = ?
                """,
                (str(item_id),),
            ).fetchone()
            if batch_row is None:
                raise KeyError(f"Unknown inventory item {item_id}.")
            remaining = self._connection.execute(
                """
                SELECT COUNT(*) count FROM upload_checkpoints
                JOIN inventory_items ON inventory_items.id = upload_checkpoints.item_id
                WHERE inventory_items.batch_id = ?
                  AND upload_checkpoints.state NOT IN (?, ?)
                """,
                (
                    batch_row["batch_id"],
                    LocalUploadState.VERIFIED.value,
                    LocalUploadState.EXCLUDED.value,
                ),
            ).fetchone()["count"]
            self._connection.execute(
                "UPDATE batches SET state = ?, updated_at = ? WHERE id = ?",
                (
                    BatchState.COMPLETE.value if remaining == 0 else BatchState.UPLOADING.value,
                    _now(),
                    batch_row["batch_id"],
                ),
            )

    def _update_upload(
        self,
        item_id: UUID,
        *,
        state: LocalUploadState,
        error_code: str | None = None,
        increment_attempt: bool = False,
    ) -> None:
        timestamp = _now()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE upload_checkpoints
                SET state = ?,
                    attempt_count = attempt_count + ?,
                    last_error_code = ?,
                    completed_at = CASE WHEN ? IN (?, ?) THEN ? ELSE completed_at END,
                    updated_at = ?
                WHERE item_id = ?
                """,
                (
                    state.value,
                    int(increment_attempt),
                    error_code,
                    state.value,
                    LocalUploadState.VERIFIED.value,
                    LocalUploadState.EXCLUDED.value,
                    timestamp,
                    timestamp,
                    str(item_id),
                ),
            )
        if cursor.rowcount == 0:
            raise KeyError(f"Unknown upload checkpoint {item_id}.")

    def ensure_derivative_checkpoints(self, batch_id: UUID) -> None:
        self.get_batch(batch_id)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO derivative_checkpoints(
                    item_id, variant, state, attempt_count, updated_at
                )
                SELECT id, variants.variant, ?, 0, ? FROM inventory_items
                CROSS JOIN (
                    SELECT ? AS variant
                    UNION ALL SELECT ?
                ) AS variants
                WHERE batch_id = ? AND status = ?
                """,
                (
                    LocalUploadState.PENDING.value,
                    _now(),
                    AssetVariant.PREVIEW.value,
                    AssetVariant.THUMBNAIL.value,
                    str(batch_id),
                    InventoryStatus.ACCEPTED.value,
                ),
            )

    def list_derivative_checkpoints(self, batch_id: UUID) -> list[DerivativeCheckpoint]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT derivative_checkpoints.* FROM derivative_checkpoints
                JOIN inventory_items ON inventory_items.id = derivative_checkpoints.item_id
                WHERE inventory_items.batch_id = ?
                ORDER BY inventory_items.normalized_path, derivative_checkpoints.variant
                """,
                (str(batch_id),),
            ).fetchall()
        return [self._derivative_from_row(row) for row in rows]

    def get_derivative_checkpoint(
        self, item_id: UUID, variant: AssetVariant | str
    ) -> DerivativeCheckpoint:
        parsed_variant = _derivative_variant(variant)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM derivative_checkpoints WHERE item_id = ? AND variant = ?
                """,
                (str(item_id), parsed_variant.value),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown derivative checkpoint {item_id}/{parsed_variant.value}.")
        return self._derivative_from_row(row)

    @staticmethod
    def _derivative_from_row(row: sqlite3.Row) -> DerivativeCheckpoint:
        return DerivativeCheckpoint(
            item_id=UUID(row["item_id"]),
            variant=AssetVariant(row["variant"]),
            state=LocalUploadState(row["state"]),
            attempt_count=row["attempt_count"],
            last_error_code=row["last_error_code"],
        )

    def mark_derivative_started(self, item_id: UUID, variant: AssetVariant | str) -> None:
        self._update_derivative(
            item_id,
            _derivative_variant(variant),
            state=LocalUploadState.UPLOADING,
            increment_attempt=True,
        )

    def mark_derivative_failed(
        self, item_id: UUID, variant: AssetVariant | str, error_code: str
    ) -> None:
        self._update_derivative(
            item_id,
            _derivative_variant(variant),
            state=LocalUploadState.FAILED,
            error_code=error_code,
        )

    def mark_derivative_verified(self, item_id: UUID, variant: AssetVariant | str) -> None:
        self._update_derivative(
            item_id,
            _derivative_variant(variant),
            state=LocalUploadState.VERIFIED,
        )

    def mark_derivatives_excluded(self, item_id: UUID) -> None:
        for variant in DERIVATIVE_VARIANTS:
            self._update_derivative(item_id, variant, state=LocalUploadState.EXCLUDED)

    def _update_derivative(
        self,
        item_id: UUID,
        variant: AssetVariant,
        *,
        state: LocalUploadState,
        error_code: str | None = None,
        increment_attempt: bool = False,
    ) -> None:
        if variant not in DERIVATIVE_VARIANTS:
            raise ValueError("Derivative variants are previews or thumbnails.")
        timestamp = _now()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE derivative_checkpoints
                SET state = ?, attempt_count = attempt_count + ?, last_error_code = ?,
                    completed_at = CASE WHEN ? IN (?, ?) THEN ? ELSE completed_at END,
                    updated_at = ?
                WHERE item_id = ? AND variant = ?
                """,
                (
                    state.value,
                    int(increment_attempt),
                    error_code,
                    state.value,
                    LocalUploadState.VERIFIED.value,
                    LocalUploadState.EXCLUDED.value,
                    timestamp,
                    timestamp,
                    str(item_id),
                    variant.value,
                ),
            )
        if cursor.rowcount == 0:
            raise KeyError(f"Unknown derivative checkpoint {item_id}/{variant.value}.")

    def derivatives_complete(self, batch_id: UUID) -> bool:
        checkpoints = self.list_derivative_checkpoints(batch_id)
        return bool(checkpoints) and all(
            checkpoint.state in {LocalUploadState.VERIFIED, LocalUploadState.EXCLUDED}
            for checkpoint in checkpoints
        )

    def derivative_cache_directory(self, batch_id: UUID) -> Path:
        self.get_batch(batch_id)
        destination = self.database_path.parent / "derivatives" / str(batch_id)
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            destination.chmod(0o700)
        return destination

    def cleanup_derivative_cache(self, batch_id: UUID) -> None:
        destination = self.database_path.parent / "derivatives" / str(batch_id)
        if destination.is_dir():
            shutil.rmtree(destination)

    def touch_item(self, item_id: UUID, generation: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE inventory_items
                SET last_seen_generation = ?, updated_at = ?
                WHERE id = ?
                """,
                (generation, _now(), str(item_id)),
            )

    def reconcile_unseen(self, batch_id: UUID, generation: int, *, frozen: bool) -> int:
        with self._lock, self._connection:
            if not frozen:
                cursor = self._connection.execute(
                    """
                    DELETE FROM inventory_items
                    WHERE batch_id = ? AND last_seen_generation != ?
                    """,
                    (str(batch_id), generation),
                )
                return cursor.rowcount

            cursor = self._connection.execute(
                """
                UPDATE inventory_items
                SET status = ?, reason = ?, updated_at = ?
                WHERE batch_id = ?
                  AND last_seen_generation != ?
                  AND status != ?
                """,
                (
                    InventoryStatus.BLOCKING.value,
                    RejectionReason.SOURCE_MISSING.value,
                    _now(),
                    str(batch_id),
                    generation,
                    InventoryStatus.REJECTED.value,
                ),
            )
            return cursor.rowcount

    def add_scan_issue(
        self,
        *,
        batch_id: UUID,
        selection_id: UUID | None,
        generation: int,
        reason: ScanIssueReason,
        source_path: Path,
        blocking: bool,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO scan_issues(
                    id, batch_id, selection_id, generation, reason,
                    source_path, blocking, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    str(batch_id),
                    str(selection_id) if selection_id else None,
                    generation,
                    reason.value,
                    os.path.abspath(source_path),
                    int(blocking),
                    _now(),
                ),
            )

    def list_scan_issues(self, batch_id: UUID) -> list[ScanIssue]:
        batch = self.get_batch(batch_id)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT reason, source_path, blocking FROM scan_issues
                WHERE batch_id = ? AND generation = ?
                ORDER BY source_path, id
                """,
                (str(batch_id), batch.scan_generation),
            ).fetchall()
        return [
            ScanIssue(
                reason=ScanIssueReason(row["reason"]),
                source_path=Path(row["source_path"]),
                blocking=bool(row["blocking"]),
            )
            for row in rows
        ]

    def summary(
        self,
        batch_id: UUID,
        *,
        changed_count: int = 0,
        validated_count: int = 0,
        reused_count: int = 0,
    ) -> ScanSummary:
        with self._lock:
            counts = self._connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'accepted' THEN 1 ELSE 0 END) accepted_count,
                    SUM(CASE WHEN status = 'accepted' THEN size_bytes ELSE 0 END) accepted_bytes,
                    SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) rejected_count,
                    SUM(CASE WHEN status = 'rejected' THEN size_bytes ELSE 0 END) rejected_bytes,
                    SUM(CASE WHEN status = 'blocking' THEN 1 ELSE 0 END) blocking_item_count
                FROM inventory_items WHERE batch_id = ?
                """,
                (str(batch_id),),
            ).fetchone()
            issue_counts = self._connection.execute(
                """
                SELECT
                    SUM(CASE WHEN blocking = 1 THEN 1 ELSE 0 END) blocking_count,
                    SUM(CASE WHEN blocking = 0 THEN 1 ELSE 0 END) warning_count
                FROM scan_issues
                WHERE batch_id = ? AND generation = (
                    SELECT scan_generation FROM batches WHERE id = ?
                )
                """,
                (str(batch_id), str(batch_id)),
            ).fetchone()
        return ScanSummary(
            batch_id=batch_id,
            state=self.get_batch(batch_id).state,
            accepted_count=counts["accepted_count"] or 0,
            accepted_bytes=counts["accepted_bytes"] or 0,
            rejected_count=counts["rejected_count"] or 0,
            rejected_bytes=counts["rejected_bytes"] or 0,
            blocking_item_count=counts["blocking_item_count"] or 0,
            blocking_issue_count=issue_counts["blocking_count"] or 0,
            warning_count=issue_counts["warning_count"] or 0,
            changed_count=changed_count,
            validated_count=validated_count,
            reused_count=reused_count,
        )

    def approve_batch(self, batch_id: UUID, *, supported_profile_id: str) -> None:
        batch = self.get_batch(batch_id)
        if batch.state is not BatchState.NEEDS_REVIEW:
            raise ValueError("The contribution batch must finish scanning before approval.")
        summary = self.summary(batch_id)
        if summary.blocking_item_count or summary.blocking_issue_count:
            raise ValueError("Resolve every blocking scan issue before approval.")
        if summary.accepted_count == 0:
            raise ValueError("A contribution batch must contain at least one accepted JPEG.")
        event = self.get_event(batch.event_id)
        if event.processing_profile_id != supported_profile_id:
            raise ValueError("The desktop processing profile does not match the event.")
        if summary.accepted_bytes > event.storage_limit_bytes:
            raise ValueError("The contribution exceeds the cached event storage allowance.")
        timestamp = _now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE batches
                SET state = ?, frozen_at = COALESCE(frozen_at, ?),
                    approved_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    BatchState.APPROVED.value,
                    timestamp,
                    timestamp,
                    timestamp,
                    str(batch_id),
                ),
            )

    def update_relocated_selection(
        self,
        selection_id: UUID,
        new_source_path: Path,
        relocated_items: Iterable[tuple[InventoryItem, Path, FileSnapshot]],
    ) -> None:
        normalized_selection = normalize_path(new_source_path)
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE selections SET source_path = ?, normalized_path = ? WHERE id = ?
                """,
                (os.path.abspath(new_source_path), normalized_selection, str(selection_id)),
            )
            for item, relocated_path, snapshot in relocated_items:
                self._connection.execute(
                    """
                    UPDATE inventory_items
                    SET source_path = ?, normalized_path = ?, basename = ?, size_bytes = ?,
                        modified_ns = ?, changed_ns = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        os.path.abspath(relocated_path),
                        normalize_path(relocated_path),
                        relocated_path.name,
                        snapshot.size_bytes,
                        snapshot.modified_ns,
                        snapshot.changed_ns,
                        _now(),
                        str(item.id),
                    ),
                )
