"""Recursive discovery orchestration with restart-safe per-file checkpoints."""

import os
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from .checkpoint import CheckpointStore, normalize_path
from .models import (
    FileSnapshot,
    InventoryItem,
    InventoryStatus,
    RejectionReason,
    ScanIssueReason,
    ScanProgress,
    ScanSummary,
    SelectionKind,
    SourceSelection,
    ValidationResult,
)
from .validation import InventoryValidator, sha256_file


class ScanCancelled(Exception):
    pass


class RelocationError(ValueError):
    pass


@dataclass(frozen=True)
class _Candidate:
    selection: SourceSelection
    source_path: Path
    relative_path: Path
    snapshot: FileSnapshot
    linked: bool
    regular: bool


def _snapshot(path: Path) -> tuple[os.stat_result, FileSnapshot]:
    details = path.stat(follow_symlinks=False)
    return details, FileSnapshot(
        size_bytes=details.st_size,
        modified_ns=details.st_mtime_ns,
        changed_ns=details.st_ctime_ns,
    )


def _unchanged(item: InventoryItem, snapshot: FileSnapshot) -> bool:
    return item.snapshot == snapshot and item.status in {
        InventoryStatus.ACCEPTED,
        InventoryStatus.REJECTED,
    }


def _is_junction(path: Path) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction and is_junction(path))


class InventoryScanner:
    def __init__(
        self,
        store: CheckpointStore,
        *,
        validator: InventoryValidator | None = None,
    ) -> None:
        self.store = store
        self.validator = validator or InventoryValidator()

    def scan(
        self,
        batch_id: UUID,
        *,
        on_progress: Callable[[ScanProgress], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ScanSummary:
        generation, frozen = self.store.start_scan(batch_id)
        seen: set[str] = set()
        accepted_count = 0
        rejected_count = 0
        changed_count = 0
        validated_count = 0
        reused_count = 0
        processed_count = 0

        try:
            for selection in self.store.list_selections(batch_id):
                for candidate in self._discover(selection, batch_id, generation):
                    if is_cancelled and is_cancelled():
                        raise ScanCancelled
                    normalized = normalize_path(candidate.source_path)
                    if normalized in seen:
                        continue
                    seen.add(normalized)
                    existing = self.store.get_item_by_path(batch_id, normalized)

                    if frozen and existing is None:
                        self.store.add_scan_issue(
                            batch_id=batch_id,
                            selection_id=selection.id,
                            generation=generation,
                            reason=ScanIssueReason.NEW_FILE_AFTER_FREEZE,
                            source_path=candidate.source_path,
                            blocking=False,
                        )
                        continue

                    if existing and _unchanged(existing, candidate.snapshot):
                        self.store.touch_item(existing.id, generation)
                        reused_count += 1
                        result_status = existing.status
                    else:
                        result = self._validate_candidate(candidate)
                        try:
                            _, after = _snapshot(candidate.source_path)
                        except OSError:
                            after = None
                        if after is None or after != candidate.snapshot:
                            result = ValidationResult(
                                status=InventoryStatus.BLOCKING,
                                reason=RejectionReason.FILE_CHANGED_DURING_SCAN,
                                content_type=None,
                                sha256=None,
                                content_md5=None,
                                width=None,
                                height=None,
                            )
                        self.store.upsert_item(
                            batch_id=batch_id,
                            selection_id=selection.id,
                            relative_path=candidate.relative_path,
                            source_path=candidate.source_path,
                            snapshot=after or candidate.snapshot,
                            result=result,
                            generation=generation,
                        )
                        validated_count += 1
                        if existing is not None:
                            changed_count += 1
                        result_status = result.status

                    accepted_count += result_status is InventoryStatus.ACCEPTED
                    rejected_count += result_status is InventoryStatus.REJECTED
                    processed_count += 1
                    if on_progress:
                        on_progress(
                            ScanProgress(
                                processed_count=processed_count,
                                accepted_count=accepted_count,
                                rejected_count=rejected_count,
                                current_path=candidate.source_path,
                            )
                        )

            changed_count += self.store.reconcile_unseen(batch_id, generation, frozen=frozen)
            provisional = self.store.summary(
                batch_id,
                changed_count=changed_count,
                validated_count=validated_count,
                reused_count=reused_count,
            )
            requires_review = (
                not frozen
                or changed_count > 0
                or provisional.blocking_item_count > 0
                or provisional.blocking_issue_count > 0
            )
            self.store.finish_scan(batch_id, requires_review=requires_review)
            return self.store.summary(
                batch_id,
                changed_count=changed_count,
                validated_count=validated_count,
                reused_count=reused_count,
            )
        except ScanCancelled:
            self.store.pause_scan(batch_id)
            raise

    def relocate_selection(self, selection_id: UUID, new_source_path: Path) -> None:
        selection = self.store.get_selection(selection_id)
        new_root = Path(new_source_path)
        self._verify_selection_kind(selection.kind, new_root)
        relocated: list[tuple[InventoryItem, Path, FileSnapshot]] = []

        for item in self.store.list_items(selection.batch_id, selection_id=selection.id):
            relocated_path = (
                new_root if selection.kind is SelectionKind.FILE else new_root / item.relative_path
            )
            try:
                details, snapshot = _snapshot(relocated_path)
            except OSError as exc:
                raise RelocationError(
                    f"The relocated source is incomplete: {item.relative_path}"
                ) from exc
            if not stat.S_ISREG(details.st_mode) or relocated_path.is_symlink():
                raise RelocationError(f"The relocated source type changed: {item.relative_path}")
            if item.status is InventoryStatus.ACCEPTED:
                try:
                    relocated_checksum = sha256_file(relocated_path)
                except OSError as exc:
                    raise RelocationError(
                        f"The relocated source cannot be verified: {item.relative_path}"
                    ) from exc
                if relocated_checksum != item.sha256:
                    raise RelocationError(
                        f"The relocated source bytes changed: {item.relative_path}"
                    )
            relocated.append((item, relocated_path, snapshot))

        self.store.update_relocated_selection(selection_id, new_root, relocated)

    def _discover(
        self,
        selection: SourceSelection,
        batch_id: UUID,
        generation: int,
    ) -> Iterator[_Candidate]:
        source = selection.source_path
        try:
            details, snapshot = _snapshot(source)
        except OSError:
            self._add_issue(
                batch_id,
                selection,
                generation,
                ScanIssueReason.SELECTION_MISSING,
                source,
            )
            return

        if source.is_symlink() or _is_junction(source):
            yield _Candidate(selection, source, Path("."), snapshot, linked=True, regular=False)
            return

        expected_mode = stat.S_ISREG if selection.kind is SelectionKind.FILE else stat.S_ISDIR
        if not expected_mode(details.st_mode):
            self._add_issue(
                batch_id,
                selection,
                generation,
                ScanIssueReason.SELECTION_TYPE_CHANGED,
                source,
            )
            return

        if selection.kind is SelectionKind.FILE:
            yield _Candidate(selection, source, Path("."), snapshot, linked=False, regular=True)
            return

        yield from self._walk_directory(selection, batch_id, generation)

    def _walk_directory(
        self,
        selection: SourceSelection,
        batch_id: UUID,
        generation: int,
    ) -> Iterator[_Candidate]:
        pending = [selection.source_path]
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as scan:
                    entries = sorted(
                        scan, key=lambda entry: os.path.normcase(entry.name), reverse=True
                    )
            except OSError:
                self._add_issue(
                    batch_id,
                    selection,
                    generation,
                    ScanIssueReason.DIRECTORY_UNREADABLE,
                    directory,
                )
                continue

            for entry in entries:
                path = Path(entry.path)
                try:
                    details, snapshot = _snapshot(path)
                except OSError:
                    self._add_issue(
                        batch_id,
                        selection,
                        generation,
                        ScanIssueReason.FILE_UNREADABLE,
                        path,
                    )
                    continue
                relative = path.relative_to(selection.source_path)
                linked = entry.is_symlink() or _is_junction(path)
                if linked:
                    yield _Candidate(
                        selection, path, relative, snapshot, linked=True, regular=False
                    )
                elif stat.S_ISDIR(details.st_mode):
                    pending.append(path)
                else:
                    yield _Candidate(
                        selection,
                        path,
                        relative,
                        snapshot,
                        linked=False,
                        regular=stat.S_ISREG(details.st_mode),
                    )

    def _validate_candidate(self, candidate: _Candidate) -> ValidationResult:
        if candidate.linked:
            return self._rejected(RejectionReason.SYMBOLIC_LINK)
        if not candidate.regular:
            return self._rejected(RejectionReason.UNSUPPORTED_FILE_TYPE)
        try:
            return self.validator.validate(
                candidate.source_path,
                size_bytes=candidate.snapshot.size_bytes,
            )
        except OSError:
            return ValidationResult(
                status=InventoryStatus.BLOCKING,
                reason=RejectionReason.SOURCE_UNREADABLE,
                content_type=None,
                sha256=None,
                content_md5=None,
                width=None,
                height=None,
            )

    @staticmethod
    def _rejected(reason: RejectionReason) -> ValidationResult:
        return ValidationResult(
            status=InventoryStatus.REJECTED,
            reason=reason,
            content_type=None,
            sha256=None,
            content_md5=None,
            width=None,
            height=None,
        )

    @staticmethod
    def _verify_selection_kind(kind: SelectionKind, path: Path) -> None:
        if path.is_symlink() or _is_junction(path):
            raise RelocationError("Linked source roots are not supported.")
        if kind is SelectionKind.FILE and not path.is_file():
            raise RelocationError("Select the relocated source file.")
        if kind is SelectionKind.FOLDER and not path.is_dir():
            raise RelocationError("Select the relocated source folder.")

    def _add_issue(
        self,
        batch_id: UUID,
        selection: SourceSelection,
        generation: int,
        reason: ScanIssueReason,
        source_path: Path,
    ) -> None:
        self.store.add_scan_issue(
            batch_id=batch_id,
            selection_id=selection.id,
            generation=generation,
            reason=reason,
            source_path=source_path,
            blocking=True,
        )
