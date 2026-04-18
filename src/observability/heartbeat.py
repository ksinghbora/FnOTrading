"""Heartbeat writer — lets external watchdogs know recorders are alive.

Why this exists (DATA_RELIABILITY_PLAN §8.5):
  When the WebSocket dies silently or the recorder process gets stuck on a
  blocking call, we used to find out hours later when nightly_audit.py
  noticed the day's CSV was empty. By then the broker session is gone and
  the trading day is unrecoverable.

  This module writes a tiny JSON file every N seconds describing what each
  recorder has done so far today. A separate watchdog (cron, systemd timer,
  or just nightly_audit.py) reads the file and Telegrams if the timestamp
  is stale during market hours.

Files:
  data/heartbeat/<process_name>.json

Schema:
  {
    "process": "recorder",
    "pid": 12345,
    "ts": "2026-04-17T11:43:01+05:30",
    "started_at": "2026-04-17T09:14:58+05:30",
    "streams": {
      "chain_recorder": {
        "snapshots_today": 145,
        "last_write": "2026-04-17T11:42:58+05:30",
        "priceable_pct_last": 96.2,
        "iv_pct_last": 91.4
      },
      "vix_recorder": {
        "minutes_today": 148,
        "last_value": 13.82,
        "last_write": "2026-04-17T11:42:00+05:30"
      },
      "tick_recorder": {
        "ticks_today": 2_134_119,
        "instruments_active": 42,
        "last_flush": "2026-04-17T11:42:30+05:30"
      }
    },
    "kite_ws_state": "connected"
  }

Atomic writes (temp + fsync + rename) so a crash mid-write never produces a
torn JSON that the watchdog would parse as "recorder dead".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.core.clock import IST

logger = logging.getLogger(__name__)


class Heartbeat:
    """Process-wide heartbeat collector.

    One instance per process (recorder OR trader). Recorders register their
    stream-level stats via ``update(stream, **fields)``; the writer task
    serializes the latest snapshot to ``output_path`` every ``interval_s``
    seconds.

    Thread-safe via a stdlib lock — recorders may be in different async
    tasks but the lock is uncontended so latency is negligible.
    """

    def __init__(
        self,
        process_name: str,
        output_path: str | Path = "data/heartbeat/recorder.json",
        interval_s: float = 30.0,
    ):
        self._process_name = process_name
        self._output_path = Path(output_path)
        self._interval_s = float(interval_s)
        self._lock = threading.Lock()
        self._streams: dict[str, dict[str, Any]] = {}
        self._fields: dict[str, Any] = {}
        self._started_at = self._now_iso()
        self._task: asyncio.Task | None = None
        self._running = False
        self._date_str = date.today().isoformat()

    # ── Public API ────────────────────────────────────────────────

    def update(self, stream: str, **fields: Any) -> None:
        """Merge ``fields`` into the stream's heartbeat block.

        Each call replaces overlapping keys but preserves keys not mentioned
        in this call — recorders can issue partial updates without first
        reading the current state.
        """
        with self._lock:
            block = self._streams.setdefault(stream, {})
            block.update(fields)
            block["last_update_ts"] = self._now_iso()

    def set_field(self, key: str, value: Any) -> None:
        """Set a process-level field (e.g. kite_ws_state)."""
        with self._lock:
            self._fields[key] = value

    async def start(self) -> None:
        """Begin periodic writes. Idempotent."""
        if self._running:
            return
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        self._running = True
        self._task = asyncio.create_task(self._write_loop())
        # Write once immediately so a watchdog polling between start and the
        # first interval doesn't see a stale file from a previous run.
        self._write_now()
        logger.info(
            "[HEARTBEAT] Started — process=%s file=%s interval=%.0fs",
            self._process_name, self._output_path, self._interval_s,
        )

    async def stop(self) -> None:
        """Stop the write loop and emit a final state."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # Final snapshot so the watchdog can see "stopped cleanly" rather
        # than "stale because writer died".
        self.set_field("stopped_at", self._now_iso())
        self._write_now()
        logger.info("[HEARTBEAT] Stopped — final snapshot written to %s", self._output_path)

    # ── Internals ─────────────────────────────────────────────────

    async def _write_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._interval_s)
                if not self._running:
                    break
                # Daily counter reset: caller may want to reset
                # snapshots_today etc. when the date rolls over. We don't
                # touch caller-owned fields, but we expose the date so the
                # watchdog can detect cross-day staleness.
                today_str = date.today().isoformat()
                if today_str != self._date_str:
                    self._date_str = today_str
                self._write_now()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[HEARTBEAT] write loop error")

    def _write_now(self) -> None:
        with self._lock:
            payload = {
                "process": self._process_name,
                "pid": os.getpid(),
                "ts": self._now_iso(),
                "started_at": self._started_at,
                "date": self._date_str,
                "streams": dict(self._streams),
                **self._fields,
            }
        self._atomic_write(payload)

    def _atomic_write(self, payload: dict) -> None:
        tmp = self._output_path.with_suffix(self._output_path.suffix + ".tmp")
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, self._output_path)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(IST).isoformat(timespec="seconds")


# ────────────────────────────────────────────────────────────────────
# Watchdog read-side helper — used by nightly audit + verify_system
# ────────────────────────────────────────────────────────────────────

def read_heartbeat(path: str | Path = "data/heartbeat/recorder.json") -> dict | None:
    """Read the latest heartbeat. Returns None if missing or corrupt.

    Designed to be tolerant: a corrupt heartbeat file means we just got
    interrupted mid-write. Treat as "no heartbeat" and let the caller
    decide how loud to be about it.
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def heartbeat_age_seconds(
    path: str | Path = "data/heartbeat/recorder.json",
    *,
    now: datetime | None = None,
) -> float | None:
    """Seconds since the heartbeat was last written, or None if unreadable."""
    hb = read_heartbeat(path)
    if not hb or "ts" not in hb:
        return None
    try:
        ts = datetime.fromisoformat(hb["ts"])
    except ValueError:
        return None
    if ts.tzinfo is None:
        # Treat naive as IST; recorder writes IST, so mismatch is a bug.
        # NOTE: pytz timezones MUST be attached via .localize(), not
        # .replace(tzinfo=...), otherwise pytz uses LMT (+05:53) and the
        # computed age is off by ~23 minutes.
        ts = IST.localize(ts)
    n = now or datetime.now(IST)
    return (n - ts).total_seconds()
