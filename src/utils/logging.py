"""Logging setup — human-readable on stderr, optional JSONL to disk.

Architecture (DATA_RELIABILITY_PLAN §8.2):
- Every module uses ``logger = logging.getLogger(__name__)`` from stdlib.
- The root logger has TWO handlers when a process runs in production:
    1. StreamHandler on stderr with a human-readable format — for tail/journal.
    2. FileHandler on ``data/logs/<process>.jsonl`` with ``JsonFormatter`` —
       for queryable, durable, structured logs you can pipe through ``jq``
       or load into the dashboard.
- Tags are typed via ``src.utils.log_tags.Tag`` and passed through
  ``extra={"tag": Tag.X, ...}``. The JSON formatter promotes ``tag`` to a
  top-level field so JSONL queries can filter on a stable closed list
  instead of fuzzy substring matches against ``[NAME]`` prefixes.
- Secrets are stripped by ``RedactFilter`` before they hit either sink, so
  a stray ``logger.info("token=%s", access_token)`` becomes ``[REDACTED]``.

structlog is no longer used at call sites — every module imports stdlib
``logging``. Keeping the dep around as a no-op was confusing; the JSON
sink lives entirely in stdlib formatters now so there's nothing extra to
configure per-module.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from enum import Enum
from pathlib import Path
from typing import Any


# ─── Reserved LogRecord attributes ──────────────────────────────────
# Anything not in this set on a LogRecord is treated as a user-supplied
# ``extra=`` field and gets emitted at the top level of the JSON record.
# Source: logging.LogRecord.__init__ + logging.Formatter docs.
_RESERVED_LOGRECORD_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
})


# ─── Redaction patterns ─────────────────────────────────────────────
# Field names that match any of these substrings (case-insensitive) get
# their value replaced with ``[REDACTED]`` before serialisation. List is
# intentionally narrow — broad matches like "id" would clobber order_id.
_SECRET_KEY_PATTERNS = (
    re.compile(r"token", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"password", re.IGNORECASE),
    re.compile(r"api[_-]?key", re.IGNORECASE),
    re.compile(r"access[_-]?key", re.IGNORECASE),
    re.compile(r"auth(?:orization)?", re.IGNORECASE),
)


def _is_secret_key(key: str) -> bool:
    return any(p.search(key) for p in _SECRET_KEY_PATTERNS)


def _jsonable(value: Any) -> Any:
    """Coerce a value into something json.dumps can handle.

    Falls back to ``repr()`` rather than raising. Logging a Decimal or a
    pydantic model shouldn't crash the formatter.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    # Decimal, datetime, pydantic models, exceptions, …
    try:
        return str(value)
    except Exception:  # pragma: no cover — last-resort safety
        return repr(value)


class RedactFilter(logging.Filter):
    """Replace secret-looking ``extra=`` values with ``[REDACTED]``.

    Operates on the LogRecord attributes set by ``logger.x(..., extra={...})``.
    The message string itself (``record.msg`` / ``record.getMessage()``) is
    NOT scanned — secrets in f-string'd messages are the call site's
    responsibility (and the linter's). Scanning the message would either
    miss a lot or false-positive constantly.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for key in list(record.__dict__):
            if key in _RESERVED_LOGRECORD_ATTRS:
                continue
            if _is_secret_key(key):
                record.__dict__[key] = "[REDACTED]"
        return True


class JsonFormatter(logging.Formatter):
    """Format LogRecord as a single JSON line.

    Output shape:
        {
          "ts": "2026-04-17T10:30:00.123Z",     # ISO 8601 UTC, ms precision
          "level": "INFO",
          "process": "recorder",                 # set by setup_logging()
          "pid": 12345,
          "module": "src.recorder.tick_recorder",
          "msg": "subscribed to 257 tokens",
          "tag": "TICKER",                       # promoted from extras if set
          ... any other extras from extra={} ...
          "exc": "Traceback (...)\n..."          # only if exc_info present
        }

    Why not pull in a third-party JSON formatter? Stdlib gives us this in
    ~30 lines and we control exactly which keys land in the schema —
    important because dashboard queries and alert scripts will assume it.
    """

    def __init__(self, process_name: str = "trader") -> None:
        super().__init__()
        self._process_name = process_name

    def format(self, record: logging.LogRecord) -> str:
        # Use formatTime for ISO + ms precision; record.created is unix epoch.
        ts = self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S")
        ts = f"{ts}.{int(record.msecs):03d}Z"

        out: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "process": self._process_name,
            "pid": record.process,
            "module": record.name,
            "msg": record.getMessage(),
        }

        # Promote tag to a top-level field for easy jq filtering.
        tag = record.__dict__.get("tag")
        if tag is not None:
            out["tag"] = tag.value if isinstance(tag, Enum) else str(tag)

        # Carry over every other extra= field. We skip reserved LogRecord
        # attributes and the tag we just promoted.
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOGRECORD_ATTRS or key in ("tag",):
                continue
            out[key] = _jsonable(value)

        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            out["stack"] = self.formatStack(record.stack_info)

        # default=str is a final safety net for anything _jsonable missed.
        return json.dumps(out, default=str, separators=(",", ":"))


def setup_logging(
    log_level: str = "INFO",
    json_output: bool = False,  # kept for backward compat — unused
    process_name: str | None = None,
    log_file: Path | str | None = None,
) -> None:
    """Configure root logger with stderr (human) + optional JSONL file sink.

    Args:
        log_level: Root log level (DEBUG, INFO, WARNING, …).
        json_output: Legacy flag from the structlog era. Ignored — both
            sinks coexist now (human on stderr, JSON in the file).
        process_name: Tag every JSON record with this name so concatenated
            recorder+trader logs are still distinguishable. Defaults to
            ``trader`` if a log_file is set without an explicit name.
        log_file: If provided, write JSONL records to this path. Parent
            directory is created if missing. Pass ``None`` (default) to
            stick to stderr-only — useful in tests and one-off scripts.

    Idempotent: clears existing root handlers before adding ours, so
    repeated calls (e.g. reconfiguring in a test) don't pile up duplicates.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    proc = process_name or "trader"

    root = logging.getLogger()
    # Drop any prior handlers — basicConfig from a previous call, pytest
    # caplog leftovers, etc. — to keep behaviour deterministic.
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level)

    redactor = RedactFilter()

    # 1. Human-readable stderr handler (always on; this is what tail/journal sees).
    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    stderr_handler.setLevel(level)
    stderr_handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    stderr_handler.addFilter(redactor)
    root.addHandler(stderr_handler)

    # 2. JSONL file handler (only if a path was given).
    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        # mode="a" → safe across restarts; we do not rotate here. Rotation
        # is handled out-of-band (e.g. logrotate or a daily cron) so the
        # process never has to cope with a handle being yanked. If we add
        # in-process rotation later it goes here, not in the formatter.
        file_handler = logging.FileHandler(path, mode="a", encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(JsonFormatter(process_name=proc))
        file_handler.addFilter(redactor)
        root.addHandler(file_handler)

    # Silence noisy third-party loggers — same list as before.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    # Surface where logs are going — helpful in deployment when the JSON
    # sink is supposed to be on but the path env var was missing.
    if log_file is not None:
        logging.getLogger(__name__).info(
            "logging configured: process=%s level=%s jsonl=%s pid=%d",
            proc, log_level.upper(), path, os.getpid(),
        )
