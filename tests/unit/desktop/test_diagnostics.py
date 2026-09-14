import json
from pathlib import Path
from uuid import uuid4

from PIL import Image

from openfotos_desktop.diagnostics import RedactedDiagnosticExporter
from openfotos_desktop.ingestion import (
    CheckpointStore,
    EventCache,
    InventoryScanner,
    SubEventCache,
)


def test_diagnostic_export_omits_paths_filenames_and_checksums(tmp_path: Path) -> None:
    source = tmp_path / "private-client-name" / "person-name.jpg"
    source.parent.mkdir()
    Image.new("RGB", (4, 4), color="blue").save(source, format="JPEG")
    report_path = tmp_path / "report.json"

    with CheckpointStore(tmp_path / "checkpoint.sqlite3") as store:
        event_id = uuid4()
        sub_event = SubEventCache(id=uuid4(), name="Reception", position=1)
        store.cache_event(
            EventCache(
                id=event_id,
                name="Private event name",
                storage_limit_bytes=25_000_000_000,
                processing_profile_id="pilot-profile-v1",
                sub_events=(sub_event,),
            )
        )
        batch_id = store.create_batch(event_id, sub_event.id)
        store.add_files(batch_id, [source])
        InventoryScanner(store).scan(batch_id)
        checksum = store.list_items(batch_id)[0].sha256

        RedactedDiagnosticExporter(store).export(batch_id, report_path)

    rendered = report_path.read_text(encoding="utf-8")
    report = json.loads(rendered)
    assert report["format"] == "openfotos-redacted-diagnostic-v1"
    assert "person-name.jpg" not in rendered
    assert "private-client-name" not in rendered
    assert "Private event name" not in rendered
    assert checksum not in rendered
