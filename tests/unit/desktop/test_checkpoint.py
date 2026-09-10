import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from openfotos_desktop.ingestion import CheckpointStore, EventCache, InventoryScanner


def test_newer_checkpoint_schema_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "checkpoint.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 999")

    with pytest.raises(ValueError, match="newer OpenFotos"):
        CheckpointStore(database)


def test_approved_batch_rejects_new_selections(tmp_path: Path) -> None:
    photo = tmp_path / "photo.jpg"
    Image.new("RGB", (4, 4), color="blue").save(photo, format="JPEG")

    with CheckpointStore(tmp_path / "checkpoint.sqlite3") as store:
        event_id = uuid4()
        store.cache_event(
            EventCache(
                id=event_id,
                name="Reception",
                storage_limit_bytes=25_000_000_000,
                processing_profile_id="pilot-profile-v1",
            )
        )
        batch_id = store.create_batch(event_id)
        selection_id = store.add_files(batch_id, [photo])[0]
        InventoryScanner(store).scan(batch_id)
        store.approve_batch(batch_id, supported_profile_id="pilot-profile-v1")

        with pytest.raises(ValueError, match="frozen"):
            store.add_files(batch_id, [tmp_path / "another.jpg"])
        with pytest.raises(ValueError, match="frozen"):
            store.remove_selection(selection_id)


def test_draft_selection_can_be_removed_without_touching_source(tmp_path: Path) -> None:
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"source stays")

    with CheckpointStore(tmp_path / "checkpoint.sqlite3") as store:
        event_id = uuid4()
        store.cache_event(
            EventCache(
                id=event_id,
                name="Reception",
                storage_limit_bytes=25_000_000_000,
                processing_profile_id="pilot-profile-v1",
            )
        )
        batch_id = store.create_batch(event_id)
        selection_id = store.add_files(batch_id, [photo])[0]

        store.remove_selection(selection_id)

        assert store.list_selections(batch_id) == []
        assert photo.read_bytes() == b"source stays"


def test_deleting_local_event_removes_checkpoints_but_not_sources(tmp_path: Path) -> None:
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"source bytes stay on disk")

    with CheckpointStore(tmp_path / "checkpoint.sqlite3") as store:
        event_id = uuid4()
        store.cache_event(
            EventCache(
                id=event_id,
                name="Reception",
                storage_limit_bytes=25_000_000_000,
                processing_profile_id="pilot-profile-v1",
            )
        )
        batch_id = store.create_batch(event_id)
        store.add_files(batch_id, [photo])

        store.delete_local_event(event_id)

        assert store.list_events() == []
        with pytest.raises(KeyError):
            store.get_batch(batch_id)
        assert photo.read_bytes() == b"source bytes stay on disk"


def test_cached_event_allowance_blocks_oversized_batch_approval(tmp_path: Path) -> None:
    photo = tmp_path / "photo.jpg"
    Image.new("RGB", (4, 4), color="blue").save(photo, format="JPEG")

    with CheckpointStore(tmp_path / "checkpoint.sqlite3") as store:
        event_id = uuid4()
        store.cache_event(
            EventCache(
                id=event_id,
                name="Reception",
                storage_limit_bytes=photo.stat().st_size - 1,
                processing_profile_id="pilot-profile-v1",
            )
        )
        batch_id = store.create_batch(event_id)
        store.add_files(batch_id, [photo])
        InventoryScanner(store).scan(batch_id)

        with pytest.raises(ValueError, match="storage allowance"):
            store.approve_batch(batch_id, supported_profile_id="pilot-profile-v1")
