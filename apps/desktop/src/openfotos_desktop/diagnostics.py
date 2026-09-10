"""Redacted desktop diagnostics safe to attach to a support request."""

import json
from pathlib import Path
from uuid import UUID

from .ingestion import CheckpointStore


class RedactedDiagnosticExporter:
    def __init__(self, store: CheckpointStore) -> None:
        self.store = store

    def export(self, batch_id: UUID, destination: Path) -> None:
        batch = self.store.get_batch(batch_id)
        summary = self.store.summary(batch_id)
        items = self.store.list_items(batch_id)
        report = {
            "format": "openfotos-redacted-diagnostic-v1",
            "installation_id": str(self.store.installation_id),
            "event_id": str(batch.event_id),
            "batch_id": str(batch.id),
            "batch_state": batch.state.value,
            "summary": {
                "accepted_count": summary.accepted_count,
                "accepted_bytes": summary.accepted_bytes,
                "rejected_count": summary.rejected_count,
                "rejected_bytes": summary.rejected_bytes,
                "blocking_item_count": summary.blocking_item_count,
                "blocking_issue_count": summary.blocking_issue_count,
                "warning_count": summary.warning_count,
            },
            "items": [
                {
                    "asset_id": str(item.id),
                    "status": item.status.value,
                    "reason": item.reason.value if item.reason else None,
                    "size_bytes": item.snapshot.size_bytes,
                }
                for item in items
            ],
        }
        destination.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
