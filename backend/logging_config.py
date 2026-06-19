"""
backend/logging_config.py

Structured JSON logging configuration for Prism.

Every log entry emitted by any Prism component contains six required fields
(Requirement 12.1, 12.4–12.6):
  - timestamp   UTC ISO-8601 timestamp
  - level       Severity (DEBUG / INFO / WARNING / ERROR / CRITICAL)
  - component   Python logger name (e.g. "backend.discovery.gitlab_fetcher")
  - error_type  Exception class name, or "" for non-exception records
  - correlation_id  Per-request UUID from the FastAPI middleware (via ContextVar)
  - message     Human-readable log message

Secret masking (Requirement 11.3, 11.4):
  ``mask_secret()`` and ``display_token()`` are imported from ``backend.config``
  and must be applied at every call site that references a secret value.
  The logging formatter itself does NOT mask values — callers are responsible
  for masking before formatting.

Correlation ID (Requirement 12.7):
  ``correlation_id_var`` is a module-level ``ContextVar[str]``.  The FastAPI
  middleware sets it once per request.  All log records produced during that
  request automatically include the same ID because ``PrismJsonFormatter``
  reads it at record-format time.

Requirements: 11.3, 11.4, 12.1, 12.4, 12.5, 12.6, 12.7
"""

from __future__ import annotations

import json
import logging
import logging.config
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Correlation ID context variable
# ---------------------------------------------------------------------------

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")
"""Per-request correlation ID injected by the FastAPI middleware.

Set via ``correlation_id_var.set(cid)`` in the middleware; read by
``PrismJsonFormatter`` to include in every log record produced during the
request lifecycle (Requirement 12.7).
"""


# ---------------------------------------------------------------------------
# JSON log formatter
# ---------------------------------------------------------------------------


class PrismJsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects.

    Each output line contains exactly the six required fields:
    ``timestamp``, ``level``, ``component``, ``error_type``,
    ``correlation_id``, and ``message``.

    Extra keyword arguments passed to the logger are appended as additional
    top-level JSON keys for richer context (e.g. ``model_count=5``).
    """

    def format(self, record: logging.LogRecord) -> str:
        # --- Six required fields ---
        entry: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "component": record.name,
            "error_type": (
                record.exc_info[1].__class__.__name__
                if record.exc_info and record.exc_info[1] is not None
                else ""
            ),
            "correlation_id": correlation_id_var.get(""),
            "message": record.getMessage(),
        }

        # --- Optional: attach exception traceback as a separate field ---
        if record.exc_info:
            entry["traceback"] = self.formatException(record.exc_info)

        # --- Any extra fields the caller attached via `extra={}` ---
        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_ATTRS and not key.startswith("_"):
                entry[key] = value

        return json.dumps(entry, default=str)


# Fields that are part of the standard LogRecord — we skip these when
# collecting "extra" fields so we don't duplicate them in the output.
_STANDARD_LOG_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName",
    "taskName",
})


# ---------------------------------------------------------------------------
# Setup function
# ---------------------------------------------------------------------------


def configure_logging(level: str = "INFO") -> None:
    """Install the Prism structured JSON log formatter on the root logger.

    Call this once at application startup (before ``uvicorn`` starts).

    Parameters
    ----------
    level:
        Minimum log level to emit; defaults to ``"INFO"``.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(PrismJsonFormatter())

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove any existing handlers to avoid duplicate output
    root_logger.handlers.clear()
    root_logger.addHandler(handler)

    # Suppress noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("sentence_transformers").setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "Prism structured JSON logging configured; level=%s", level
    )
