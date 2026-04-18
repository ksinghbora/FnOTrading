"""Tests for src.observability.heartbeat — the recorder watchdog file.

The heartbeat is the only signal an external watchdog has between recorder
crashes and the nightly audit. Its contract:
  1. Writes are atomic (temp + fsync + rename) — no torn JSON.
  2. update() merges fields without clobbering keys it didn't mention.
  3. read_heartbeat() tolerates a missing file gracefully.
  4. heartbeat_age_seconds() handles both naive and TZ-aware timestamps.
  5. start()/stop() are idempotent and produce a final snapshot.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytz

from src.observability.heartbeat import (
    Heartbeat,
    heartbeat_age_seconds,
    read_heartbeat,
)

IST = pytz.timezone("Asia/Kolkata")


@pytest.fixture
def hb_path(tmp_path: Path) -> Path:
    return tmp_path / "hb" / "recorder.json"


# ── Atomicity & schema ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_write_creates_directory_and_file(hb_path: Path):
    hb = Heartbeat("trader", output_path=hb_path, interval_s=60.0)
    await hb.start()
    try:
        assert hb_path.exists(), "start() should immediately write a heartbeat"
        payload = json.loads(hb_path.read_text())
        assert payload["process"] == "trader"
        assert payload["pid"] > 0
        assert "ts" in payload
        assert "started_at" in payload
        assert payload["streams"] == {}
    finally:
        await hb.stop()


@pytest.mark.asyncio
async def test_update_merges_partial_fields(hb_path: Path):
    hb = Heartbeat("recorder", output_path=hb_path, interval_s=60.0)
    await hb.start()
    try:
        hb.update("chain_recorder", snapshots_today=5, priceable_pct_last=92.1)
        hb.update("chain_recorder", snapshots_today=6)  # only one field
        # Force a write
        hb._write_now()  # type: ignore[attr-defined]

        payload = json.loads(hb_path.read_text())
        chain = payload["streams"]["chain_recorder"]
        # snapshots_today was overwritten, priceable_pct_last preserved
        assert chain["snapshots_today"] == 6
        assert chain["priceable_pct_last"] == 92.1
        assert "last_update_ts" in chain
    finally:
        await hb.stop()


@pytest.mark.asyncio
async def test_set_field_attaches_process_level_metadata(hb_path: Path):
    hb = Heartbeat("recorder", output_path=hb_path, interval_s=60.0)
    await hb.start()
    try:
        hb.set_field("kite_ws_state", "connected")
        hb._write_now()  # type: ignore[attr-defined]
        payload = json.loads(hb_path.read_text())
        assert payload["kite_ws_state"] == "connected"
    finally:
        await hb.stop()


@pytest.mark.asyncio
async def test_no_partial_files_left_behind(hb_path: Path):
    """The .tmp file used during atomic write must not survive normal flow."""
    hb = Heartbeat("trader", output_path=hb_path, interval_s=60.0)
    await hb.start()
    try:
        hb._write_now()  # type: ignore[attr-defined]
        # `.suffix + ".tmp"` is the convention used by the writer
        leftover = hb_path.with_suffix(hb_path.suffix + ".tmp")
        assert not leftover.exists(), "atomic write should rename, not leak .tmp"
    finally:
        await hb.stop()


@pytest.mark.asyncio
async def test_stop_writes_final_snapshot_with_stopped_at(hb_path: Path):
    hb = Heartbeat("trader", output_path=hb_path, interval_s=60.0)
    await hb.start()
    await hb.stop()
    payload = json.loads(hb_path.read_text())
    assert "stopped_at" in payload, "stop() should annotate the final heartbeat"


@pytest.mark.asyncio
async def test_start_is_idempotent(hb_path: Path):
    hb = Heartbeat("trader", output_path=hb_path, interval_s=60.0)
    await hb.start()
    # Calling start() twice should not double-launch the writer task
    await hb.start()
    assert hb._task is not None  # type: ignore[attr-defined]
    await hb.stop()


# ── Read-side helpers ───────────────────────────────────────────────


def test_read_heartbeat_returns_none_when_missing(tmp_path: Path):
    assert read_heartbeat(tmp_path / "nope.json") is None


def test_read_heartbeat_returns_none_for_corrupt_json(tmp_path: Path):
    p = tmp_path / "broken.json"
    p.write_text("{ not valid json")
    assert read_heartbeat(p) is None


def test_heartbeat_age_seconds_handles_aware_timestamp(tmp_path: Path):
    p = tmp_path / "hb.json"
    five_min_ago = datetime.now(IST) - timedelta(minutes=5)
    p.write_text(json.dumps({
        "process": "trader",
        "pid": 1,
        "ts": five_min_ago.isoformat(timespec="seconds"),
        "streams": {},
    }))
    age = heartbeat_age_seconds(p)
    assert age is not None
    # Allow ±5s slack for clock drift between write and assert
    assert 295 <= age <= 305


def test_heartbeat_age_seconds_handles_naive_timestamp(tmp_path: Path):
    """A naive ISO string in the heartbeat must be assumed to be IST.

    If we accidentally treated it as UTC the age would jump by 5h30m and
    the watchdog would page incorrectly during pre-open.
    """
    p = tmp_path / "hb.json"
    naive = datetime.now(IST).replace(tzinfo=None) - timedelta(minutes=2)
    p.write_text(json.dumps({
        "process": "trader",
        "pid": 1,
        "ts": naive.isoformat(timespec="seconds"),
        "streams": {},
    }))
    age = heartbeat_age_seconds(p)
    assert age is not None
    # If misinterpreted as UTC, age would be ~19,800s. We expect ~120s.
    assert 110 <= age <= 130, (
        f"naive ts must be treated as IST; got {age}s (expected ~120s)"
    )


def test_heartbeat_age_seconds_returns_none_when_no_ts(tmp_path: Path):
    p = tmp_path / "hb.json"
    p.write_text(json.dumps({"process": "trader", "pid": 1, "streams": {}}))
    assert heartbeat_age_seconds(p) is None
