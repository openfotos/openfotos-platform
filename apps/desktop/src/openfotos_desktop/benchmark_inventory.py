"""Repeatable, redacted inventory benchmark for representative photographer hardware."""

import argparse
import json
import platform
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from openfotos_contracts import EVENT_ORIGINAL_BYTES_LIMIT

from .ingestion import CheckpointStore, EventCache, InventoryScanner, ScanLimits, SubEventCache


def run_benchmark(source: Path, database: Path) -> dict[str, object]:
    sub_event = SubEventCache(id=uuid4(), name="Benchmark section", position=1)
    event = EventCache(
        id=uuid4(),
        name="Redacted benchmark event",
        storage_limit_bytes=EVENT_ORIGINAL_BYTES_LIMIT,
        processing_profile_id="pilot-profile-v1",
        sub_events=(sub_event,),
    )
    with CheckpointStore(database) as store:
        store.cache_event(event)
        batch_id = store.create_batch(event.id, sub_event.id, label="benchmark")
        store.add_folder(batch_id, source)
        started = time.perf_counter()
        summary = InventoryScanner(store).scan(batch_id)
        elapsed_seconds = time.perf_counter() - started

    processed_bytes = summary.accepted_bytes + summary.rejected_bytes
    return {
        "format": "openfotos-inventory-benchmark-v1",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "limits": {
            "max_file_bytes": ScanLimits().max_file_bytes,
            "max_pixels": ScanLimits().max_pixels,
        },
        "accepted_count": summary.accepted_count,
        "accepted_bytes": summary.accepted_bytes,
        "rejected_count": summary.rejected_count,
        "rejected_bytes": summary.rejected_bytes,
        "blocking_item_count": summary.blocking_item_count,
        "blocking_issue_count": summary.blocking_issue_count,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "processed_mib_per_second": round(
            processed_bytes / (1024 * 1024) / elapsed_seconds if elapsed_seconds else 0,
            3,
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Representative local or USB source folder")
    parser.add_argument("--output", type=Path, help="Optional redacted JSON report path")
    arguments = parser.parse_args()
    if not arguments.source.is_dir():
        parser.error("source must be a readable directory")

    with tempfile.TemporaryDirectory(prefix="openfotos-benchmark-") as temporary:
        report = run_benchmark(arguments.source, Path(temporary) / "checkpoint.sqlite3")
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if arguments.output:
        arguments.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 1 if report["blocking_item_count"] or report["blocking_issue_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
