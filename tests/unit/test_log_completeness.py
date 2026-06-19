"""
tests/unit/test_log_completeness.py

Property-based tests for structured log entry completeness and correlation ID
propagation.

# Feature: prism, Property 21: Structured Log Entry Completeness
For any error, warning, or info event produced by any Prism component, the
resulting log entry SHALL always contain all six required fields: UTC timestamp,
severity level, component name, error type, correlation_id, and
human-readable message.

# Feature: prism, Property 22: Correlation ID Propagation
For any user-initiated request with an assigned correlation ID, every log
entry produced during that request's lifecycle SHALL include that same
correlation ID.

Validates: Requirements 12.1, 12.4, 12.5, 12.6, 12.7
"""

import json
import logging
import uuid
from io import StringIO

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backend.logging_config import (
    PrismJsonFormatter,
    correlation_id_var,
    configure_logging,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = {
    "timestamp",
    "level",
    "component",
    "error_type",
    "correlation_id",
    "message",
}


def _capture_log_record(
    message: str,
    level: int = logging.INFO,
    logger_name: str = "backend.test",
    exc_info: bool = False,
    correlation_id: str = "",
) -> dict:
    """Emit a log record through PrismJsonFormatter and return the parsed JSON dict."""
    formatter = PrismJsonFormatter()
    record = logging.LogRecord(
        name=logger_name,
        level=level,
        pathname="test.py",
        lineno=1,
        msg=message,
        args=(),
        exc_info=(ValueError, ValueError("test error"), None) if exc_info else None,
    )
    token = correlation_id_var.set(correlation_id)
    try:
        formatted = formatter.format(record)
    finally:
        correlation_id_var.reset(token)
    return json.loads(formatted)


# ---------------------------------------------------------------------------
# Property 21: Structured Log Entry Completeness
# Validates: Requirements 12.1, 12.4, 12.5, 12.6
# ---------------------------------------------------------------------------


@given(
    message=st.text(min_size=1, max_size=200),
    level=st.sampled_from([logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL]),
    logger_name=st.from_regex(r"backend\.[a-z][a-z0-9._]{1,30}", fullmatch=True),
)
@settings(max_examples=100)
def test_property_21_log_entry_has_all_required_fields(
    message: str,
    level: int,
    logger_name: str,
) -> None:
    """**Property 21: Structured Log Entry Completeness**

    Every log entry SHALL always contain all six required fields.

    **Validates: Requirements 12.1, 12.4, 12.5, 12.6**
    """
    entry = _capture_log_record(message, level, logger_name)

    # --- Invariant: all six required fields present ---
    for field in _REQUIRED_FIELDS:
        assert field in entry, (
            f"Required log field '{field}' missing from entry: {entry}"
        )

    # --- Invariant: correct types ---
    assert isinstance(entry["timestamp"], str), "timestamp must be a string"
    assert isinstance(entry["level"], str), "level must be a string"
    assert isinstance(entry["component"], str), "component must be a string"
    assert isinstance(entry["error_type"], str), "error_type must be a string"
    assert isinstance(entry["correlation_id"], str), "correlation_id must be a string"
    assert isinstance(entry["message"], str), "message must be a string"

    # --- Invariant: level is valid ---
    assert entry["level"] in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}, (
        f"Invalid level value: {entry['level']!r}"
    )

    # --- Invariant: component matches logger name ---
    assert entry["component"] == logger_name, (
        f"component should be {logger_name!r}, got {entry['component']!r}"
    )

    # --- Invariant: message matches ---
    assert message in entry["message"] or entry["message"] == message, (
        f"message field should contain {message!r}"
    )


# ---------------------------------------------------------------------------
# Property 22: Correlation ID Propagation
# Validates: Requirements 12.7
# ---------------------------------------------------------------------------


@given(
    correlation_id=st.uuids().map(str),
    message=st.text(min_size=1, max_size=100),
)
@settings(max_examples=100)
def test_property_22_correlation_id_propagated(
    correlation_id: str,
    message: str,
) -> None:
    """**Property 22: Correlation ID Propagation**

    Every log entry produced during a request SHALL include the request's
    correlation ID set via the ContextVar.

    **Validates: Requirements 12.7**
    """
    entry = _capture_log_record(message, correlation_id=correlation_id)

    assert entry["correlation_id"] == correlation_id, (
        f"Expected correlation_id={correlation_id!r}, got {entry['correlation_id']!r}"
    )


@given(
    cids=st.lists(
        st.uuids().map(str),
        min_size=2,
        max_size=5,
        unique=True,
    ),
)
@settings(max_examples=50)
def test_property_22_different_requests_have_different_correlation_ids(
    cids: list[str],
) -> None:
    """**Property 22 isolation**: Each request's logs use that request's CID only."""
    entries = []
    for cid in cids:
        entry = _capture_log_record("test", correlation_id=cid)
        entries.append((cid, entry["correlation_id"]))

    for expected, actual in entries:
        assert expected == actual, (
            f"CID mismatch: expected {expected!r}, got {actual!r}"
        )


# ---------------------------------------------------------------------------
# Unit tests — concrete examples
# ---------------------------------------------------------------------------


class TestLogCompletenessExamples:

    def test_error_type_populated_for_exceptions(self):
        entry = _capture_log_record("Something failed", exc_info=True)
        assert entry["error_type"] == "ValueError"

    def test_error_type_empty_for_non_exceptions(self):
        entry = _capture_log_record("Normal info")
        assert entry["error_type"] == ""

    def test_timestamp_is_iso8601_utc(self):
        entry = _capture_log_record("test")
        ts = entry["timestamp"]
        assert "T" in ts, "Timestamp should be ISO 8601"
        assert ts.endswith("+00:00") or ts.endswith("Z") or "+00:00" in ts, (
            "Timestamp should be UTC"
        )

    def test_default_correlation_id_is_empty_string(self):
        # No CID set → defaults to ""
        entry = _capture_log_record("test", correlation_id="")
        assert entry["correlation_id"] == ""

    def test_configure_logging_does_not_raise(self, capsys):
        configure_logging(level="WARNING")
        logger = logging.getLogger("backend.test.config")
        logger.info("This should NOT appear (below WARNING)")
        # Re-configure to INFO for other tests
        configure_logging(level="INFO")

    def test_multiple_log_entries_same_cid(self):
        cid = str(uuid.uuid4())
        entries = [
            _capture_log_record(f"msg {i}", correlation_id=cid)
            for i in range(5)
        ]
        for entry in entries:
            assert entry["correlation_id"] == cid
