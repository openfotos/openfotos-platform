"""Structured logging that deliberately excludes URLs, client addresses, and request bodies."""

import json
import logging
from datetime import UTC, datetime


class PrivacySafeJsonFormatter(logging.Formatter):
    _application_events = {
        "readiness_check_failed",
        "request_completed",
    }
    _optional_fields = (
        "request_id",
        "request_method",
        "response_status",
        "host_scope",
        "duration_ms",
        "dependency",
    )

    def format(self, record: logging.LogRecord) -> str:
        event = (
            record.msg
            if record.name.startswith("openfotos.") and record.msg in self._application_events
            else "application_log"
        )
        document = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": event,
        }
        for field in self._optional_fields:
            value = getattr(record, field, None)
            if value is not None:
                document[field] = value
        if record.exc_info:
            document["exception_type"] = record.exc_info[0].__name__
        return json.dumps(document, separators=(",", ":"), sort_keys=True)
