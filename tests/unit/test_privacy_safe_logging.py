import json
import logging

from openfotos_server.logging import PrivacySafeJsonFormatter


def test_formatter_does_not_interpolate_potentially_sensitive_log_arguments() -> None:
    record = logging.LogRecord(
        name="django.server",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg='Not Found: /share/private-capability/ with body "%s"',
        args=("private-secret",),
        exc_info=None,
    )

    document = json.loads(PrivacySafeJsonFormatter().format(record))

    assert document["event"] == "application_log"
    assert "private-capability" not in json.dumps(document)
    assert "private-secret" not in json.dumps(document)


def test_formatter_does_not_emit_preformatted_framework_messages() -> None:
    record = logging.LogRecord(
        name="django.security.DisallowedHost",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Invalid HTTP_HOST header: 'attacker-controlled.example'",
        args=(),
        exc_info=None,
    )

    document = json.loads(PrivacySafeJsonFormatter().format(record))

    assert document["event"] == "application_log"
    assert "attacker-controlled" not in json.dumps(document)


def test_formatter_keeps_allowlisted_structured_application_event() -> None:
    record = logging.LogRecord(
        name="openfotos.request",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request_completed",
        args=(),
        exc_info=None,
    )
    record.request_id = "request-123"
    record.response_status = 200

    document = json.loads(PrivacySafeJsonFormatter().format(record))

    assert document["event"] == "request_completed"
    assert document["request_id"] == "request-123"
    assert document["response_status"] == 200
