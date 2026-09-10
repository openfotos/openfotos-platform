"""Durable, event-scoped SQLite checkpoints for desktop inventory."""

import os
import sqlite3
from collections.abc import Iterable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from uuid import UUID, uuid4

from .models import (
    BatchState,
    ContributionBatch,
    EventCache,
    FileSnapshot,
    InventoryItem,
    InventoryStatus,
    RejectionReason,
    ScanIssue,
    ScanIssueReason,
    ScanSummary,
    SelectionKind,
    SourceSelection,
    ValidationResult,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    storage_limit_bytes INTEGER NOT NULL CHECK (storage_limit_bytes > 0),
    processing_profile_id TEXT NOT NULL,
    cached_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    device_id TEXT NOT NULL,
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
"""
_SCHEMA_VERSION = 1


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
        except Exception:
            self._connection.close()
            raise
        if os.name != "nt":
            self.database_path.chmod(0o600)
        self._installation_id = self._load_or_create_installation_id()

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
                INSERT INTO events(id, name, storage_limit_bytes, processing_profile_id, cached_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    storage_limit_bytes = excluded.storage_limit_bytes,
                    processing_profile_id = excluded.processing_profile_id,
                    cached_at = excluded.cached_at
                """,
                (
                    str(event.id),
                    event.name.strip(),
                    event.storage_limit_bytes,
                    event.processing_profile_id.strip(),
                    _now(),
                ),
            )

    def get_event(self, event_id: UUID) -> EventCache:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM events WHERE id = ?", (str(event_id),)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown cached event {event_id}.")
        return EventCache(
            id=UUID(row["id"]),
            name=row["name"],
            storage_limit_bytes=row["storage_limit_bytes"],
            processing_profile_id=row["processing_profile_id"],
        )

    def list_events(self) -> list[EventCache]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM events ORDER BY name, id").fetchall()
        return [
            EventCache(
                id=UUID(row["id"]),
                name=row["name"],
                storage_limit_bytes=row["storage_limit_bytes"],
                processing_profile_id=row["processing_profile_id"],
            )
            for row in rows
        ]

    def delete_local_event(self, event_id: UUID) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute("DELETE FROM events WHERE id = ?", (str(event_id),))
        if cursor.rowcount == 0:
            raise KeyError(f"Unknown cached event {event_id}.")

    def create_batch(self, event_id: UUID, *, label: str = "") -> UUID:
        self.get_event(event_id)
        batch_id = uuid4()
        timestamp = _now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO batches(
                    id, event_id, device_id, state, label, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(batch_id),
                    str(event_id),
                    str(self.installation_id),
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
            device_id=UUID(row["device_id"]),
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
                    content_type, sha256, width, height, last_seen_generation, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    result.width,
                    result.height,
                    generation,
                    _now(),
                ),
            )
        return item_id

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
