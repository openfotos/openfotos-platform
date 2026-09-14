import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from PIL import Image

from openfotos_desktop.ingestion import (
    BatchState,
    CheckpointStore,
    EventCache,
    InventoryScanner,
    InventoryValidator,
    RejectionReason,
    ScanLimits,
    SubEventCache,
)

_SUB_EVENT_ID = UUID("00000000-0000-4000-8000-000000000103")


def write_jpeg(path: Path, *, size: tuple[int, int] = (8, 6), color: str = "navy") -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color=color).save(path, format="JPEG")
    return path.stat().st_size


def cache_event(store: CheckpointStore, event_id: UUID | None = None) -> UUID:
    selected_id = event_id or uuid4()
    store.cache_event(
        EventCache(
            id=selected_id,
            name="Synthetic reception",
            storage_limit_bytes=25_000_000_000,
            processing_profile_id="pilot-profile-v1",
            sub_events=(SubEventCache(id=_SUB_EVENT_ID, name="Reception", position=1),),
        )
    )
    return selected_id


def create_batch(store: CheckpointStore, event_id: UUID | None = None) -> UUID:
    selected_id = event_id or cache_event(store)
    return store.create_batch(selected_id, _SUB_EVENT_ID)


def test_mixed_nested_inventory_reports_exact_counts_and_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    first_bytes = write_jpeg(source / "first.JPG")
    second_bytes = write_jpeg(source / "nested" / "second.jpeg", color="green")
    corrupt = source / "nested" / "broken.jpg"
    corrupt.write_bytes(b"not a jpeg")
    unsupported = source / "notes.txt"
    unsupported.write_bytes(b"do not upload")
    individual = tmp_path / "individual.jpeg"
    individual_bytes = write_jpeg(individual, color="red")

    with CheckpointStore(tmp_path / "checkpoint.sqlite3") as store:
        batch_id = create_batch(store)
        store.add_folder(batch_id, source)
        store.add_files(batch_id, [individual, individual])

        summary = InventoryScanner(store).scan(batch_id)

        assert summary.accepted_count == 3
        assert summary.accepted_bytes == first_bytes + second_bytes + individual_bytes
        assert summary.rejected_count == 2
        assert summary.rejected_bytes == corrupt.stat().st_size + unsupported.stat().st_size
        assert summary.blocking_issue_count == 0
        assert summary.state is BatchState.NEEDS_REVIEW
        reasons = {item.reason for item in store.list_items(batch_id) if item.reason}
        assert reasons == {
            RejectionReason.CONTENT_TYPE_MISMATCH,
            RejectionReason.UNSUPPORTED_EXTENSION,
        }


def test_forced_exit_resumes_without_revalidating_unchanged_items(tmp_path: Path) -> None:
    source = tmp_path / "source"
    for index in range(3):
        write_jpeg(source / f"{index}.jpg", color=(index * 30, 0, 0))

    database = tmp_path / "checkpoint.sqlite3"
    with CheckpointStore(database) as store:
        batch_id = create_batch(store)
        store.add_folder(batch_id, source)

        class SimulatedExit(Exception):
            pass

        def exit_after_first(progress) -> None:
            if progress.processed_count == 1:
                raise SimulatedExit

        with pytest.raises(SimulatedExit):
            InventoryScanner(store).scan(batch_id, on_progress=exit_after_first)

        assert store.get_batch(batch_id).state is BatchState.SCANNING
        assert len(store.list_items(batch_id)) == 1

    with CheckpointStore(database) as reopened:
        summary = InventoryScanner(reopened).scan(batch_id)

        assert summary.accepted_count == 3
        assert summary.reused_count == 1
        assert summary.validated_count == 2


def test_process_termination_recovers_committed_wal_checkpoint(tmp_path: Path) -> None:
    source = tmp_path / "source"
    for index in range(3):
        write_jpeg(source / f"{index}.jpg", color=(index * 30, 0, 0))
    database = tmp_path / "checkpoint.sqlite3"
    with CheckpointStore(database) as store:
        batch_id = create_batch(store)
        store.add_folder(batch_id, source)

    crash_script = """
import os
import sys
from pathlib import Path
from uuid import UUID
from openfotos_desktop.ingestion import CheckpointStore, InventoryScanner

store = CheckpointStore(Path(sys.argv[1]))
def terminate(progress):
    if progress.processed_count == 1:
        os._exit(19)
InventoryScanner(store).scan(UUID(sys.argv[2]), on_progress=terminate)
"""
    crashed = subprocess.run(
        [sys.executable, "-c", crash_script, str(database), str(batch_id)],
        check=False,
    )
    assert crashed.returncode == 19

    with CheckpointStore(database) as recovered:
        summary = InventoryScanner(recovered).scan(batch_id)

        assert summary.accepted_count == 3
        assert summary.reused_count == 1
        assert summary.validated_count == 2


def test_changed_approved_file_pauses_for_review(tmp_path: Path) -> None:
    photo = tmp_path / "photo.jpg"
    write_jpeg(photo, color="blue")

    with CheckpointStore(tmp_path / "checkpoint.sqlite3") as store:
        batch_id = create_batch(store)
        store.add_files(batch_id, [photo])
        first = InventoryScanner(store).scan(batch_id)
        store.approve_batch(batch_id, supported_profile_id="pilot-profile-v1")
        original_checksum = store.list_items(batch_id)[0].sha256

        write_jpeg(photo, size=(10, 7), color="yellow")
        current = photo.stat()
        os.utime(photo, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000))
        second = InventoryScanner(store).scan(batch_id)

        assert first.accepted_count == second.accepted_count == 1
        assert second.changed_count == 1
        assert second.state is BatchState.NEEDS_REVIEW
        assert store.list_items(batch_id)[0].sha256 != original_checksum


def test_approval_requires_complete_scan_and_matching_profile(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with CheckpointStore(tmp_path / "checkpoint.sqlite3") as store:
        batch_id = create_batch(store)
        store.add_folder(batch_id, missing)
        summary = InventoryScanner(store).scan(batch_id)

        assert summary.blocking_issue_count == 1
        with pytest.raises(ValueError, match="blocking"):
            store.approve_batch(batch_id, supported_profile_id="pilot-profile-v1")

        missing.mkdir()
        write_jpeg(missing / "photo.jpg")
        InventoryScanner(store).scan(batch_id)
        with pytest.raises(ValueError, match="processing profile"):
            store.approve_batch(batch_id, supported_profile_id="other-profile")


def test_byte_and_pixel_limits_are_stable_rejection_reasons(tmp_path: Path) -> None:
    byte_limited = tmp_path / "large.jpg"
    pixel_limited = tmp_path / "wide.jpeg"
    write_jpeg(byte_limited)
    with byte_limited.open("ab") as oversized:
        oversized.write(b"padding" * 200)
    write_jpeg(pixel_limited, size=(11, 10))
    limits = ScanLimits(max_file_bytes=1_000, max_pixels=100)

    with CheckpointStore(tmp_path / "checkpoint.sqlite3") as store:
        batch_id = create_batch(store)
        store.add_files(batch_id, [byte_limited, pixel_limited])
        InventoryScanner(store, validator=InventoryValidator(limits)).scan(batch_id)

        reasons = {item.source_path.name: item.reason for item in store.list_items(batch_id)}
        assert reasons == {
            "large.jpg": RejectionReason.FILE_TOO_LARGE,
            "wide.jpeg": RejectionReason.IMAGE_TOO_LARGE,
        }


def test_relocated_root_is_verified_and_reuses_checkpoint(tmp_path: Path) -> None:
    original = tmp_path / "original"
    write_jpeg(original / "nested" / "photo.jpg")

    with CheckpointStore(tmp_path / "checkpoint.sqlite3") as store:
        batch_id = create_batch(store)
        selection_id = store.add_folder(batch_id, original)
        InventoryScanner(store).scan(batch_id)
        store.approve_batch(batch_id, supported_profile_id="pilot-profile-v1")

        relocated = tmp_path / "relocated"
        original.rename(relocated)
        scanner = InventoryScanner(store)
        scanner.relocate_selection(selection_id, relocated)
        summary = scanner.scan(batch_id)

        assert summary.accepted_count == 1
        assert summary.reused_count == 1
        assert store.list_items(batch_id)[0].source_path == relocated / "nested" / "photo.jpg"


def test_ten_installations_create_distinct_batches_and_assets(tmp_path: Path) -> None:
    source = tmp_path / "shared.jpg"
    write_jpeg(source)
    event_id = uuid4()
    installation_ids = set()
    batch_ids = set()
    asset_ids = set()

    for index in range(10):
        with CheckpointStore(tmp_path / f"device-{index}.sqlite3") as store:
            cache_event(store, event_id)
            batch_id = create_batch(store, event_id)
            store.add_files(batch_id, [source])
            InventoryScanner(store).scan(batch_id)
            installation_ids.add(store.installation_id)
            batch_ids.add(batch_id)
            asset_ids.add(store.list_items(batch_id)[0].id)

    assert len(installation_ids) == len(batch_ids) == len(asset_ids) == 10


def test_distinct_paths_with_identical_bytes_are_separate_accepted_assets(
    tmp_path: Path,
) -> None:
    first = tmp_path / "editor-one" / "photo.jpg"
    second = tmp_path / "editor-two" / "same-photo.jpg"
    write_jpeg(first)
    second.parent.mkdir()
    second.write_bytes(first.read_bytes())

    with CheckpointStore(tmp_path / "checkpoint.sqlite3") as store:
        batch_id = create_batch(store)
        store.add_files(batch_id, [first, second])

        summary = InventoryScanner(store).scan(batch_id)
        items = store.list_items(batch_id)

        assert summary.accepted_count == 2
        assert len({item.id for item in items}) == 2
        assert len({item.sha256 for item in items}) == 1


def test_new_file_after_freeze_is_warned_and_left_for_a_new_batch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_jpeg(source / "approved.jpg")

    with CheckpointStore(tmp_path / "checkpoint.sqlite3") as store:
        batch_id = create_batch(store)
        store.add_folder(batch_id, source)
        InventoryScanner(store).scan(batch_id)
        store.approve_batch(batch_id, supported_profile_id="pilot-profile-v1")

        write_jpeg(source / "later.jpg", color="yellow")
        summary = InventoryScanner(store).scan(batch_id)

        assert summary.state is BatchState.APPROVED
        assert summary.accepted_count == 1
        assert summary.warning_count == 1
        assert [item.basename for item in store.list_items(batch_id)] == ["approved.jpg"]


def test_symbolic_link_is_visible_and_never_followed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = source / "target.jpg"
    link = source / "linked.jpg"
    write_jpeg(target)
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("This test environment cannot create symbolic links.")

    with CheckpointStore(tmp_path / "checkpoint.sqlite3") as store:
        batch_id = create_batch(store)
        store.add_folder(batch_id, source)
        summary = InventoryScanner(store).scan(batch_id)

        assert summary.accepted_count == 1
        assert summary.rejected_count == 1
        linked_item = next(
            item for item in store.list_items(batch_id) if item.basename == link.name
        )
        assert linked_item.reason is RejectionReason.SYMBOLIC_LINK


def test_jpeg_signature_with_invalid_structure_has_decode_reason(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.jpg"
    invalid.write_bytes(b"\xff\xd8\xffnot-a-decodable-jpeg")

    with CheckpointStore(tmp_path / "checkpoint.sqlite3") as store:
        batch_id = create_batch(store)
        store.add_files(batch_id, [invalid])
        InventoryScanner(store).scan(batch_id)

        assert store.list_items(batch_id)[0].reason is RejectionReason.INVALID_JPEG
