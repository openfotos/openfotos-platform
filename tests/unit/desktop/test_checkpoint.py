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
    LocalFaceState,
    LocalUploadState,
    PreviewPolicyCache,
    SubEventCache,
)

_SUB_EVENT = SubEventCache(id=uuid4(), name="Reception", position=1)


def test_newer_checkpoint_schema_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "checkpoint.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 999")

    with pytest.raises(ValueError, match="newer OneNodeAI Studio"):
        CheckpointStore(database)


def test_session4_checkpoint_migrates_event_metadata_to_sub_event_schema(tmp_path: Path) -> None:
    database = tmp_path / "checkpoint.sqlite3"
    event_id = uuid4()
    with CheckpointStore(database) as store:
        store.cache_event(
            EventCache(
                id=event_id,
                name="Reception",
                storage_limit_bytes=50_000_000_000,
                processing_profile_id="pilot-profile-v1",
                sub_events=(_SUB_EVENT,),
            )
        )
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE events ADD COLUMN role TEXT NOT NULL DEFAULT 'primary'")
        connection.execute("ALTER TABLE batches RENAME COLUMN installation_id TO device_id")
        connection.execute("ALTER TABLE batches DROP COLUMN sub_event_id")
        connection.execute("ALTER TABLE events DROP COLUMN face_model_id")
        connection.execute("ALTER TABLE events DROP COLUMN face_index_ready")
        connection.execute("DROP TABLE face_analysis_checkpoints")
        connection.execute("DROP TABLE sub_events")
        connection.execute("PRAGMA user_version = 4")

    with CheckpointStore(database) as migrated:
        event = migrated.get_event(event_id)
        assert event.name == "Reception"
        assert event.intake_generation == 1
        assert event.device_label == ""
        assert event.sub_events == ()
        assert migrated.installation_id


def test_checkpoint_migrates_the_legacy_ofts_watermark_logo_kind(tmp_path: Path) -> None:
    database = tmp_path / "checkpoint.sqlite3"
    event_id = uuid4()
    policy = PreviewPolicyCache(
        id=uuid4(),
        enabled=True,
        template=WatermarkTemplate.COMPACT_BOTTOM_RIGHT,
        text="",
        logo_kind=WatermarkLogoKind.ONENODEAI,
        renderer_id="watermark-raster-v1",
        derivative_profile_id="gallery-jpeg-v1",
        mark_sha256="b" * 64,
    )
    with CheckpointStore(database) as store:
        store.cache_event(
            EventCache(
                id=event_id,
                name="Reception",
                storage_limit_bytes=50_000_000_000,
                processing_profile_id="pilot-profile-v1",
                sub_events=(_SUB_EVENT,),
                preview_policy=policy,
            )
        )
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE events SET preview_logo_kind = 'ofts'")
        connection.execute("PRAGMA user_version = 6")

    with CheckpointStore(database) as migrated:
        assert migrated.get_event(event_id).preview_policy == policy


def test_workstation_label_persists_and_recovers_from_a_single_cached_label(tmp_path: Path) -> None:
    database = tmp_path / "checkpoint.sqlite3"
    with CheckpointStore(database) as store:
        assert store.workstation_label() == ""
        store.cache_event(
            EventCache(
                id=uuid4(),
                name="Reception",
                storage_limit_bytes=50_000_000_000,
                processing_profile_id="pilot-profile-v1",
                sub_events=(_SUB_EVENT,),
                device_label="Yashas",
            )
        )

        assert store.workstation_label() == "Yashas"
        store.set_workstation_label("Renamed workstation")

    with CheckpointStore(database) as reopened:
        assert reopened.workstation_label() == "Renamed workstation"


def test_workstation_label_recovery_does_not_guess_between_cached_labels(tmp_path: Path) -> None:
    database = tmp_path / "checkpoint.sqlite3"
    with CheckpointStore(database) as store:
        for name, label in (("Reception", "Workstation one"), ("Ceremony", "Workstation two")):
            store.cache_event(
                EventCache(
                    id=uuid4(),
                    name=name,
                    storage_limit_bytes=50_000_000_000,
                    processing_profile_id="pilot-profile-v1",
                    sub_events=(_SUB_EVENT,),
                    device_label=label,
                )
            )

        assert store.workstation_label() == ""


def test_approved_batch_rejects_new_selections(tmp_path: Path) -> None:
    photo = tmp_path / "photo.jpg"
    Image.new("RGB", (4, 4), color="blue").save(photo, format="JPEG")

    with CheckpointStore(tmp_path / "checkpoint.sqlite3") as store:
        event_id = uuid4()
        store.cache_event(
            EventCache(
                id=event_id,
                name="Reception",
                storage_limit_bytes=50_000_000_000,
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
        text="OneNodeAI Studio",
        logo_kind=WatermarkLogoKind.ONENODEAI,
        renderer_id="watermark-raster-v1",
        derivative_profile_id="gallery-jpeg-v1",
        mark_sha256="a" * 64,
    )
    with CheckpointStore(database) as store:
        store.cache_event(
            EventCache(
                id=event_id,
                name="Reception",
                storage_limit_bytes=50_000_000_000,
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
        store.mark_face_analysis_started(item_id)
        store.mark_face_analysis_complete(
            item_id,
            state=LocalFaceState.NO_USABLE_FACE,
            detected_face_count=0,
            usable_face_count=0,
        )

    with CheckpointStore(database) as reopened:
        assert reopened.get_event(event_id).preview_policy == policy
        preview = reopened.get_derivative_checkpoint(item_id, AssetVariant.PREVIEW)
        thumbnail = reopened.get_derivative_checkpoint(item_id, "thumbnails")
        assert preview.variant is AssetVariant.PREVIEW
        assert thumbnail.variant is AssetVariant.THUMBNAIL
        assert preview.state is LocalUploadState.VERIFIED
        assert preview.attempt_count == 1
        assert thumbnail.state is LocalUploadState.PENDING
        face = reopened.get_face_analysis_checkpoint(item_id)
        assert face.state is LocalFaceState.NO_USABLE_FACE
        assert face.attempt_count == 1
        assert reopened.face_analysis_complete(batch_id)

    with sqlite3.connect(database) as connection:
        face_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(face_analysis_checkpoints)")
        }
    assert "vector" not in face_columns
    assert "embedding" not in face_columns


def test_server_derivative_state_recovers_a_lost_failure_response(tmp_path: Path) -> None:
    photo = tmp_path / "photo.jpg"
    Image.new("RGB", (4, 4), color="blue").save(photo, format="JPEG")

    with CheckpointStore(tmp_path / "checkpoint.sqlite3") as store:
        event_id = uuid4()
        store.cache_event(
            EventCache(
                id=event_id,
                name="Reception",
                storage_limit_bytes=50_000_000_000,
                processing_profile_id="pilot-profile-v1",
                sub_events=(_SUB_EVENT,),
            )
        )
        batch_id = store.create_batch(event_id, _SUB_EVENT.id)
        store.add_files(batch_id, [photo])
        InventoryScanner(store).scan(batch_id)
        store.approve_batch(batch_id, supported_profile_id="pilot-profile-v1")
        store.mark_batch_reserved(batch_id)
        item_id = store.list_items(batch_id)[0].id
        for _ in range(5):
            store.mark_derivative_started(item_id, AssetVariant.THUMBNAIL)
            store.mark_derivative_failed(item_id, AssetVariant.THUMBNAIL, "server_unavailable")
        assert store.get_derivative_checkpoint(item_id, AssetVariant.THUMBNAIL).attempt_count == 5

        store.sync_derivative(
            item_id,
            AssetVariant.THUMBNAIL,
            state=LocalUploadState.PENDING,
            attempt_count=4,
            error_code="",
        )

        recovered = store.get_derivative_checkpoint(item_id, AssetVariant.THUMBNAIL)
        assert recovered.state is LocalUploadState.PENDING
        assert recovered.attempt_count == 4
        assert recovered.last_error_code is None


def test_photographer_exclusion_is_a_terminal_local_upload_state(tmp_path: Path) -> None:
    photo = tmp_path / "photo.jpg"
    Image.new("RGB", (4, 4), color="blue").save(photo, format="JPEG")

    with CheckpointStore(tmp_path / "checkpoint.sqlite3") as store:
        event_id = uuid4()
        store.cache_event(
            EventCache(
                id=event_id,
                name="Reception",
                storage_limit_bytes=50_000_000_000,
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
                storage_limit_bytes=50_000_000_000,
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
                storage_limit_bytes=50_000_000_000,
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
