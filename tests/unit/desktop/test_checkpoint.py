import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from openfotos_contracts import AssetVariant, WatermarkLogoKind, WatermarkTemplate
from openfotos_desktop.ingestion import (
    BatchState,
    CheckpointStore,
    EventCache,
    InventoryScanner,
    LocalUploadState,
    PreviewPolicyCache,
    SubEventCache,
)

_SUB_EVENT = SubEventCache(id=uuid4(), name="Reception", position=1)


def test_newer_checkpoint_schema_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "checkpoint.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 999")

    with pytest.raises(ValueError, match="newer OpenFotos"):
        CheckpointStore(database)


def test_session4_checkpoint_migrates_event_metadata_to_sub_event_schema(tmp_path: Path) -> None:
    database = tmp_path / "checkpoint.sqlite3"
    event_id = uuid4()
    with CheckpointStore(database) as store:
        store.cache_event(
            EventCache(
                id=event_id,
                name="Reception",
                storage_limit_bytes=25_000_000_000,
                processing_profile_id="pilot-profile-v1",
                sub_events=(_SUB_EVENT,),
            )
        )
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE events ADD COLUMN role TEXT NOT NULL DEFAULT 'primary'")
        connection.execute("ALTER TABLE batches RENAME COLUMN installation_id TO device_id")
        connection.execute("ALTER TABLE batches DROP COLUMN sub_event_id")
        connection.execute("DROP TABLE sub_events")
        connection.execute("PRAGMA user_version = 4")

    with CheckpointStore(database) as migrated:
        event = migrated.get_event(event_id)
        assert event.name == "Reception"
        assert event.intake_generation == 1
        assert event.device_label == ""
        assert event.sub_events == ()
        assert migrated.installation_id


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
                sub_events=(_SUB_EVENT,),
            )
        )
        batch_id = store.create_batch(event_id, _SUB_EVENT.id)
        selection_id = store.add_files(batch_id, [photo])[0]
        InventoryScanner(store).scan(batch_id)
        store.approve_batch(batch_id, supported_profile_id="pilot-profile-v1")

        with pytest.raises(ValueError, match="frozen"):
            store.add_files(batch_id, [tmp_path / "another.jpg"])
        with pytest.raises(ValueError, match="frozen"):
            store.remove_selection(selection_id)


def test_preview_policy_and_derivative_boundaries_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "checkpoint.sqlite3"
    photo = tmp_path / "photo.jpg"
    Image.new("RGB", (20, 12), color="navy").save(photo, format="JPEG")
    event_id = uuid4()
    policy = PreviewPolicyCache(
        id=uuid4(),
        enabled=True,
        template=WatermarkTemplate.BOTTOM_CENTER,
        text="OFTS Studio",
        logo_kind=WatermarkLogoKind.OFTS,
        renderer_id="watermark-raster-v1",
        derivative_profile_id="gallery-jpeg-v1",
        mark_sha256="a" * 64,
    )
    with CheckpointStore(database) as store:
        store.cache_event(
            EventCache(
                id=event_id,
                name="Reception",
                storage_limit_bytes=25_000_000_000,
                processing_profile_id="pilot-profile-v1",
                sub_events=(_SUB_EVENT,),
                preview_policy=policy,
            )
        )
        batch_id = store.create_batch(event_id, _SUB_EVENT.id)
        store.add_files(batch_id, [photo])
        InventoryScanner(store).scan(batch_id)
        store.approve_batch(batch_id, supported_profile_id="pilot-profile-v1")
        store.mark_batch_reserved(batch_id)
        item_id = store.list_items(batch_id)[0].id
        assert len(store.list_derivative_checkpoints(batch_id)) == 2
        store.mark_derivative_started(item_id, "previews")
        store.mark_derivative_verified(item_id, AssetVariant.PREVIEW)

    with CheckpointStore(database) as reopened:
        assert reopened.get_event(event_id).preview_policy == policy
        preview = reopened.get_derivative_checkpoint(item_id, AssetVariant.PREVIEW)
        thumbnail = reopened.get_derivative_checkpoint(item_id, "thumbnails")
        assert preview.variant is AssetVariant.PREVIEW
        assert thumbnail.variant is AssetVariant.THUMBNAIL
        assert preview.state is LocalUploadState.VERIFIED
        assert preview.attempt_count == 1
        assert thumbnail.state is LocalUploadState.PENDING


def test_photographer_exclusion_is_a_terminal_local_upload_state(tmp_path: Path) -> None:
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
                sub_events=(_SUB_EVENT,),
            )
        )
        batch_id = store.create_batch(event_id, _SUB_EVENT.id)
        store.add_files(batch_id, [photo])
        InventoryScanner(store).scan(batch_id)
        store.approve_batch(batch_id, supported_profile_id="pilot-profile-v1")
        store.mark_batch_reserved(batch_id)
        item = store.list_items(batch_id)[0]

        store.mark_upload_excluded(item.id)

        assert store.get_upload_checkpoint(item.id).state is LocalUploadState.EXCLUDED
        assert store.get_batch(batch_id).state is BatchState.COMPLETE


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
                sub_events=(_SUB_EVENT,),
            )
        )
        batch_id = store.create_batch(event_id, _SUB_EVENT.id)
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
                sub_events=(_SUB_EVENT,),
            )
        )
        batch_id = store.create_batch(event_id, _SUB_EVENT.id)
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
                sub_events=(_SUB_EVENT,),
            )
        )
        batch_id = store.create_batch(event_id, _SUB_EVENT.id)
        store.add_files(batch_id, [photo])
        InventoryScanner(store).scan(batch_id)

        with pytest.raises(ValueError, match="storage allowance"):
            store.approve_batch(batch_id, supported_profile_id="pilot-profile-v1")
