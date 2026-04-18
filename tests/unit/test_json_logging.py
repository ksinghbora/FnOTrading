"""Tests for the structured JSON log pipeline (DATA_RELIABILITY_PLAN §8.2).

What we're protecting:
  1. JSON shape — ts/level/process/pid/module/msg are always present;
     ``tag`` is promoted to a top-level field when supplied via extras.
  2. Extras passed through ``logger.x(..., extra={...})`` survive into the
     record exactly. Dashboard queries and alert SQL will rely on this.
  3. Decimal / Enum / pydantic / arbitrary objects don't crash the
     formatter — they degrade to a string instead.
  4. RedactFilter scrubs secret-looking keys (token / password / api_key)
     from extras before they're written to disk.
  5. setup_logging() with ``log_file=`` writes valid JSONL to the file
     while ALSO keeping stderr output for journal/tail.
  6. The Tag enum is closed (StrEnum) — mistyped tags fail at import
     time instead of writing a typo'd value to disk.

Important: every test that calls ``setup_logging()`` re-runs it in a
``finally`` block to restore root handler state, otherwise a failure
here would poison subsequent tests in the suite.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path

import pytest

from src.utils.log_tags import Tag
from src.utils.logging import (
    JsonFormatter,
    RedactFilter,
    _is_secret_key,
    setup_logging,
)


# ── JsonFormatter shape & passthrough ──────────────────────────────


def _make_record(
    msg: str = "hello",
    level: int = logging.INFO,
    extras: dict | None = None,
    name: str = "src.example",
) -> logging.LogRecord:
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname="example.py",
        lineno=10,
        msg=msg,
        args=(),
        exc_info=None,
    )
    if extras:
        for k, v in extras.items():
            setattr(record, k, v)
    return record


def test_json_formatter_emits_required_fields():
    """Every record must carry ts/level/process/pid/module/msg."""
    fmt = JsonFormatter(process_name="recorder")
    out = json.loads(fmt.format(_make_record()))
    assert set(out.keys()) >= {"ts", "level", "process", "pid", "module", "msg"}
    assert out["level"] == "INFO"
    assert out["process"] == "recorder"
    assert out["module"] == "src.example"
    assert out["msg"] == "hello"
    # ISO 8601 with ms precision and Z suffix
    assert out["ts"].endswith("Z")
    assert "T" in out["ts"]


def test_json_formatter_promotes_tag_to_top_level():
    """tag from extras must appear at the top level as a string."""
    fmt = JsonFormatter()
    out = json.loads(fmt.format(_make_record(extras={"tag": Tag.ENTRY_QUALITY})))
    assert out["tag"] == "ENTRY_QUALITY"


def test_json_formatter_passes_through_extras():
    """All extras (other than reserved + tag) land at the top level."""
    fmt = JsonFormatter()
    out = json.loads(
        fmt.format(
            _make_record(
                extras={
                    "tag": Tag.FILTER,
                    "score": 72,
                    "underlying": "NIFTY",
                    "rejected_by": ["pcr", "max_pain"],
                }
            )
        )
    )
    assert out["score"] == 72
    assert out["underlying"] == "NIFTY"
    assert out["rejected_by"] == ["pcr", "max_pain"]


def test_json_formatter_handles_decimal_and_enum():
    """Non-JSON-native values must degrade gracefully (no crash)."""
    fmt = JsonFormatter()
    out = json.loads(
        fmt.format(
            _make_record(
                extras={
                    "premium": Decimal("123.45"),
                    "side": Tag.ORDER_LIFECYCLE,  # StrEnum used as a value
                }
            )
        )
    )
    # Decimal becomes its string form — preserves precision for jq.
    assert out["premium"] == "123.45"
    # Enum becomes its .value
    assert out["side"] == "ORDER_LIFECYCLE"


def test_json_formatter_includes_exception_info():
    """exc_info on a record gets a formatted ``exc`` field."""
    fmt = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = logging.LogRecord(
            name="src.example",
            level=logging.ERROR,
            pathname="example.py",
            lineno=10,
            msg="caught it",
            args=(),
            exc_info=sys.exc_info(),
        )
    out = json.loads(fmt.format(record))
    assert "exc" in out
    assert "ValueError: boom" in out["exc"]


def test_json_formatter_output_is_single_line():
    """Each record must be one JSONL line — no embedded newlines outside ``exc``."""
    fmt = JsonFormatter()
    line = fmt.format(_make_record(msg="line one\nline two"))
    # The msg field will contain the newline (json-escaped), but the line
    # itself must still be a single line of JSONL on disk.
    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["msg"] == "line one\nline two"


# ── Redaction ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "token", "access_token", "kite_access_token", "API_KEY",
        "api_key", "password", "secret", "auth", "Authorization",
    ],
)
def test_secret_keys_match(key: str):
    assert _is_secret_key(key), f"{key!r} should be flagged as secret"


@pytest.mark.parametrize("key", ["order_id", "underlying", "score", "ts"])
def test_non_secret_keys_pass(key: str):
    """Don't false-positive normal field names."""
    assert not _is_secret_key(key), f"{key!r} should NOT be flagged as secret"


def test_redact_filter_replaces_secret_values():
    """RedactFilter must blank out secret-looking extras before format."""
    redactor = RedactFilter()
    record = _make_record(
        extras={
            "kite_access_token": "abc123",
            "api_key": "live-key",
            "order_id": "X1",  # should NOT be redacted
        }
    )
    redactor.filter(record)
    assert record.kite_access_token == "[REDACTED]"
    assert record.api_key == "[REDACTED]"
    assert record.order_id == "X1"


def test_redaction_pipeline_through_setup_logging(tmp_path: Path):
    """End-to-end: a token in extras must NOT appear in the JSONL file."""
    log_path = tmp_path / "logs" / "test.jsonl"
    setup_logging(log_level="INFO", process_name="test", log_file=log_path)
    try:
        logger = logging.getLogger("src.test_redact")
        logger.info("auth ok", extra={"access_token": "super-secret-12345"})
        # Force flush
        for h in logging.getLogger().handlers:
            h.flush()

        contents = log_path.read_text()
        assert "super-secret-12345" not in contents, (
            "redactor failed — secret leaked into JSONL"
        )
        # Find the redact-test line specifically (setup_logging itself emits a line first).
        for raw in contents.splitlines():
            obj = json.loads(raw)
            if obj.get("module") == "src.test_redact":
                assert obj["access_token"] == "[REDACTED]"
                break
        else:
            pytest.fail("test record never reached the JSONL sink")
    finally:
        # Reset root handlers so we don't poison the next test.
        setup_logging(log_level="INFO")


# ── setup_logging() integration ────────────────────────────────────


def test_setup_logging_writes_jsonl_to_disk(tmp_path: Path):
    """A normal logger.info() must produce one JSON line on disk."""
    log_path = tmp_path / "logs" / "trader.jsonl"
    setup_logging(log_level="INFO", process_name="trader", log_file=log_path)
    try:
        logger = logging.getLogger("src.test_jsonl_write")
        logger.info("startup ok", extra={"tag": Tag.STARTUP, "step": "db"})
        for h in logging.getLogger().handlers:
            h.flush()

        assert log_path.exists()
        lines = [json.loads(l) for l in log_path.read_text().splitlines() if l]
        ours = [l for l in lines if l.get("module") == "src.test_jsonl_write"]
        assert len(ours) == 1
        assert ours[0]["msg"] == "startup ok"
        assert ours[0]["tag"] == "STARTUP"
        assert ours[0]["step"] == "db"
        assert ours[0]["process"] == "trader"
    finally:
        setup_logging(log_level="INFO")


def test_setup_logging_is_idempotent(tmp_path: Path):
    """Calling setup_logging twice must NOT double the handlers.

    Without this property, running tests in the same process (which we do)
    would multiply log output on each call and inflate the JSONL file with
    duplicate lines.
    """
    log_path = tmp_path / "logs" / "test.jsonl"
    setup_logging(log_level="INFO", process_name="test", log_file=log_path)
    handlers_after_first = len(logging.getLogger().handlers)
    setup_logging(log_level="INFO", process_name="test", log_file=log_path)
    handlers_after_second = len(logging.getLogger().handlers)
    try:
        assert handlers_after_first == handlers_after_second, (
            f"handlers piled up: {handlers_after_first} → {handlers_after_second}"
        )
    finally:
        setup_logging(log_level="INFO")


def test_setup_logging_without_log_file_skips_jsonl():
    """Default invocation (no log_file) must NOT touch disk."""
    setup_logging(log_level="INFO")
    try:
        # Only the stderr handler should be present — no FileHandler.
        from logging import FileHandler
        file_handlers = [
            h for h in logging.getLogger().handlers if isinstance(h, FileHandler)
        ]
        assert file_handlers == []
    finally:
        setup_logging(log_level="INFO")


# ── Tag enum closed-set guarantee ──────────────────────────────────


def test_tag_enum_is_string_valued():
    """StrEnum: each Tag.X must have str value equal to its name.

    This is what lets ``extra={"tag": Tag.X}`` round-trip through the JSON
    formatter as the bare uppercase string the dashboard queries expect.
    """
    for tag in Tag:
        assert tag.value == tag.name, (
            f"{tag.name} value {tag.value!r} drifted from name — JSONL queries will break"
        )


def test_tag_enum_lookup_fails_on_typo():
    """Mistyped tag at a call site must error, not silently write garbage."""
    with pytest.raises(ValueError):
        Tag("ENTRY_QUALTIY")  # deliberate typo
