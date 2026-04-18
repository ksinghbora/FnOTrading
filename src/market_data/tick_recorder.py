"""1-second tick recorder — stores raw broker ticks for ML / regime detection.

Why this exists:
  Beyond the 60-second option-chain snapshots (chain_recorder.py), we want
  the raw 1-sec tick stream for two future workloads:
    1. ML-based intraday regime detector (planned, ~120 days of data needed)
    2. High-resolution slippage / fill simulation for realistic backtests
  Kite's WebSocket already streams MODE_FULL ticks at sub-second cadence;
  this recorder simply persists them.

Storage strategy (per the Apr 17 storage decision):
  - Format: Parquet, one file per (date, instrument_token) shard.
  - Compression: ZSTD (~10x smaller than CSV for repetitive tick data).
  - Flush cadence: 1-minute in-memory buffer → Parquet append.
  - Year-1 estimate (NIFTY + BANKNIFTY index + ATM±20 options @ 1s):
      ~2.5 MB / day / instrument × 60 instruments × 250 days ≈ 38 GB raw,
      ~4 GB after Parquet/ZSTD. Fits a Backblaze B2 ₹25/month tier.

Behavior:
  - Refuses to record on weekends + NSE holidays (same gate as chain_recorder).
  - Subscribes to TICK events on the EventBus.
  - Buckets ticks by (date, instrument_token); flushes each bucket once
    per minute via a Parquet append.
  - Atomic writes: temp file + fsync + rename, identical pattern to the
    other recorders so a crash mid-flush leaves the previous file intact.
  - Best-effort: if pyarrow ever fails to import, the recorder logs a
    warning and becomes a no-op rather than crashing the trading loop.

Output layout:
  data/ticks/YYYY-MM-DD/ticks_<instrument_token>.parquet
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.core.clock import MarketClock
from src.core.events import Event, EventBus, EventType
from src.observability.heartbeat import Heartbeat

logger = logging.getLogger(__name__)

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    _PARQUET_AVAILABLE = True
except ImportError:
    _PARQUET_AVAILABLE = False


# Columns we persist for every tick. Float types are explicit so a missing
# field doesn't poison the schema across days.
_TICK_SCHEMA: dict[str, type] = {
    "timestamp": str,        # ISO-8601 with TZ
    "instrument_token": int,
    "tradingsymbol": str,
    "ltp": float,
    "volume": int,
    "oi": int,
    "bid_price": float,
    "ask_price": float,
    "bid_qty": int,
    "ask_qty": int,
}


class TickRecorder:
    """Persists raw broker ticks to Parquet for offline ML / replay use.

    Args:
        event_bus: TICK events come from here.
        clock: MarketClock for the weekday / holiday gate.
        output_dir: Root directory; per-day subdirectories are created.
        flush_interval_seconds: How often to flush in-memory buckets to disk.
        instrument_filter: Optional set of instrument_tokens to capture.
            Defaults to None (capture every TICK that arrives). Pass an
            allowlist when you want to bound disk usage to a known list.
    """

    def __init__(
        self,
        event_bus: EventBus,
        clock: MarketClock,
        output_dir: str | Path = "data/ticks",
        flush_interval_seconds: int = 60,
        instrument_filter: set[int] | None = None,
        heartbeat: Heartbeat | None = None,
    ):
        self._event_bus = event_bus
        self._clock = clock
        self._output_dir = Path(output_dir)
        self._flush_interval = flush_interval_seconds
        self._filter = instrument_filter

        # Buckets: {(date, token): [tick_dict, ...]}
        self._buckets: dict[tuple[date, int], list[dict[str, Any]]] = defaultdict(list)

        self._task: asyncio.Task | None = None
        self._running = False
        self._ticks_written_today = 0
        self._instruments_seen_today: set[int] = set()
        self._heartbeat = heartbeat

    async def start(self) -> None:
        if not _PARQUET_AVAILABLE:
            logger.warning(
                "[TICK_RECORDER] pyarrow not available — tick recording disabled. "
                "Install pyarrow or run `uv sync`."
            )
            return
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._running = True
        self._event_bus.subscribe(EventType.TICK, self._on_tick)
        self._task = asyncio.create_task(self._flush_loop())
        filt = f"filter={len(self._filter)} tokens" if self._filter else "filter=ALL"
        logger.info(
            f"[TICK_RECORDER] Started — output={self._output_dir} "
            f"flush={self._flush_interval}s {filt}"
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # Final drain so we don't lose the in-flight minute
        if _PARQUET_AVAILABLE:
            self._flush_all()
        logger.info(
            f"[TICK_RECORDER] Stopped — {self._ticks_written_today} ticks written today"
        )

    async def _on_tick(self, event: Event) -> None:
        tick = event.payload.get("tick")
        if not tick:
            return
        token = tick.get("instrument_token", 0)
        if not token:
            return
        if self._filter is not None and token not in self._filter:
            return

        # Drop ticks on closed-market days. The kite simulator can produce
        # ticks on weekends, and we don't want them in the ML corpus.
        now = self._clock.now()
        if self._clock.is_trading_holiday(now.date()):
            return

        ts = tick.get("timestamp") or now
        if isinstance(ts, datetime):
            ts_iso = ts.isoformat()
        else:
            ts_iso = str(ts)

        self._buckets[(now.date(), token)].append({
            "timestamp": ts_iso,
            "instrument_token": int(token),
            "tradingsymbol": str(tick.get("tradingsymbol") or ""),
            "ltp": float(tick.get("ltp") or 0),
            "volume": int(tick.get("volume") or 0),
            "oi": int(tick.get("oi") or 0),
            "bid_price": float(tick.get("bid_price") or 0),
            "ask_price": float(tick.get("ask_price") or 0),
            "bid_qty": int(tick.get("bid_qty") or 0),
            "ask_qty": int(tick.get("ask_qty") or 0),
        })

    async def _flush_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._flush_interval)
                if not self._running:
                    break
                self._flush_all()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[TICK_RECORDER] Error in flush loop")

    def _flush_all(self) -> None:
        if not self._buckets:
            return
        # Snapshot + clear so new ticks accumulate while we write
        pending = self._buckets
        self._buckets = defaultdict(list)

        flushed_at: datetime | None = None
        for (day, token), rows in pending.items():
            if not rows:
                continue
            try:
                self._append_parquet(day, token, rows)
                self._ticks_written_today += len(rows)
                self._instruments_seen_today.add(token)
                flushed_at = self._clock.now()
            except Exception:
                logger.exception(
                    f"[TICK_RECORDER] Failed to flush {len(rows)} ticks for "
                    f"day={day} token={token}"
                )
                # Put rows back so we retry next flush
                self._buckets[(day, token)].extend(rows)

        # Heartbeat after every flush attempt that produced at least one
        # successful write — the watchdog uses last_flush to detect WS death.
        if self._heartbeat is not None and flushed_at is not None:
            self._heartbeat.update(
                "tick_recorder",
                ticks_today=self._ticks_written_today,
                instruments_active=len(self._instruments_seen_today),
                last_flush=flushed_at.isoformat(timespec="seconds"),
            )

    def _append_parquet(self, day: date, token: int, rows: list[dict[str, Any]]) -> None:
        """Append `rows` to the day/token's Parquet shard atomically.

        Read-modify-write: load the existing shard (if any), concatenate the
        new rows, write to a temp file, fsync, rename. Cost is O(rows-so-far)
        per flush, but with ~3600 rows/hour/instrument and ZSTD compression
        the rewrite is well under 100 ms even by EOD.
        """
        day_dir = self._output_dir / day.isoformat()
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / f"ticks_{token}.parquet"
        tmp_path = path.with_suffix(".parquet.tmp")

        new_table = pa.Table.from_pylist(rows)

        if path.exists():
            existing = pq.read_table(path)
            combined = pa.concat_tables([existing, new_table], promote_options="default")
        else:
            combined = new_table

        # Write to temp + fsync + rename for crash-safety
        pq.write_table(
            combined, tmp_path,
            compression="zstd",
            compression_level=3,  # 3 ≈ best speed / ratio tradeoff for tick data
        )
        # pyarrow doesn't expose fsync directly; do it manually
        with open(tmp_path, "rb") as f:
            os.fsync(f.fileno())
        os.rename(tmp_path, path)
