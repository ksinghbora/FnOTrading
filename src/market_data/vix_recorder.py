"""Live India VIX recorder — captures the real NSE VIX index minute-by-minute.

Why this exists (Apr 17 audit):
  The chain-replay backtest needs a VIX value for every minute the chain has
  data. Until now we reconstructed VIX from ATM IV (`extract_spot_vix_from_chain.py`)
  which is biased ~50-70% high on weekly options near expiry. The bias led
  the replay engine to mis-classify regimes (calling normal days "high vol"
  and skewing every VIX-gated entry).

  Kite's WebSocket already streams the INDIA VIX index (token=264969) — the
  main loop subscribes to it for the paper broker's slippage model. This
  recorder taps the same TICK event stream and writes VIX to a daily CSV
  with the *exact* schema as `data/india_vix_minute.csv` so it can be
  concatenated with the historical feed without column gymnastics.

Output:
  data/india_vix_recorded/india_vix_YYYY-MM-DD.csv
  Columns: date,open,high,low,close,volume,oi  (volume + oi always 0)
  One row per minute (last LTP wins within the minute).

Behavior:
  - Refuses to record on weekends + NSE holidays (same gate as chain recorder).
  - Records 09:00 → 15:35 IST so we catch pre-open + post-close ticks.
  - Atomic per-minute flush: collects ticks in memory, writes to a temp file,
    fsyncs, then renames over the day file. Survives crash mid-write.
  - Uses MarketClock for time so backtests can pin a deterministic clock.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
from datetime import datetime
from pathlib import Path

from src.core.clock import MarketClock
from src.core.constants import INDIA_VIX_TOKEN
from src.core.events import Event, EventBus, EventType
from src.observability.heartbeat import Heartbeat

logger = logging.getLogger(__name__)


class IndiaVixRecorder:
    """Subscribes to TICK events and writes minute-bar VIX to a daily CSV.

    Designed to run alongside ChainSnapshotRecorder so every chain minute
    has a matching real-VIX row.
    """

    def __init__(
        self,
        event_bus: EventBus,
        clock: MarketClock,
        output_dir: str | Path = "data/india_vix_recorded",
        heartbeat: Heartbeat | None = None,
    ):
        self._event_bus = event_bus
        self._clock = clock
        self._output_dir = Path(output_dir)

        # In-memory minute aggregator: {minute_iso: {open, high, low, close}}
        # Wiped at start of each new minute when we flush.
        self._minute_bars: dict[str, dict[str, float]] = {}
        self._current_minute: str | None = None
        self._current_date: str | None = None

        self._task: asyncio.Task | None = None
        self._running = False
        self._minutes_written_today = 0
        self._last_value: float = 0.0
        self._heartbeat = heartbeat

    async def start(self) -> None:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._running = True
        self._event_bus.subscribe(EventType.TICK, self._on_tick)
        self._task = asyncio.create_task(self._flush_loop())
        logger.info(
            f"[VIX_RECORDER] Started — output={self._output_dir} "
            f"(token={INDIA_VIX_TOKEN}, every-minute flush)"
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
        # Final flush before exit so we don't lose the in-flight minute.
        self._flush_completed_minutes(force=True)
        logger.info(
            f"[VIX_RECORDER] Stopped — {self._minutes_written_today} minute bars today"
        )

    async def _on_tick(self, event: Event) -> None:
        tick = event.payload.get("tick")
        if not tick:
            return
        if tick.get("instrument_token", 0) != INDIA_VIX_TOKEN:
            return
        ltp = tick.get("ltp")
        if not ltp:
            return
        try:
            v = float(ltp)
        except (TypeError, ValueError):
            return
        if v <= 0:
            return
        # Track last seen value for heartbeat regardless of whether it
        # falls inside the recording window (a stale tick during pre-open
        # is still a useful "WS is alive" signal for the watchdog).
        self._last_value = v

        now = self._clock.now()
        if self._clock.is_trading_holiday(now.date()):
            return
        # Trade window plus a margin so we keep the closing print
        if not (9 <= now.hour < 16):
            return

        minute_iso = now.strftime("%Y-%m-%dT%H:%M")
        bar = self._minute_bars.get(minute_iso)
        if bar is None:
            self._minute_bars[minute_iso] = {
                "open": v, "high": v, "low": v, "close": v,
                "tz": now.strftime("%z"),
            }
        else:
            bar["high"] = max(bar["high"], v)
            bar["low"] = min(bar["low"], v)
            bar["close"] = v

    async def _flush_loop(self) -> None:
        """Every 30 s, write any minutes whose window has closed."""
        while self._running:
            try:
                await asyncio.sleep(30)
                if not self._running:
                    break
                self._flush_completed_minutes(force=False)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[VIX_RECORDER] Error in flush loop")

    def _flush_completed_minutes(self, force: bool) -> None:
        """Write minutes whose timestamp is strictly before the current minute.

        force=True flushes the in-flight minute too (used on shutdown).
        """
        if not self._minute_bars:
            return
        now = self._clock.now()
        current_minute = now.strftime("%Y-%m-%dT%H:%M")

        ready = []
        for minute_iso in sorted(self._minute_bars.keys()):
            if force or minute_iso < current_minute:
                ready.append(minute_iso)

        if not ready:
            return

        # Group ready minutes by date so each day's CSV gets a single rewrite
        by_day: dict[str, list[tuple[str, dict[str, float]]]] = {}
        for m in ready:
            day = m[:10]
            by_day.setdefault(day, []).append((m, self._minute_bars.pop(m)))

        for day, entries in by_day.items():
            self._append_atomic(day, entries)
            self._minutes_written_today += len(entries)

        # Heartbeat after a successful flush. The watchdog cares about both
        # last_write (did anything happen recently?) and last_value (does
        # the value look sane vs intraday norms?).
        if self._heartbeat is not None and ready:
            self._heartbeat.update(
                "vix_recorder",
                minutes_today=self._minutes_written_today,
                last_value=round(self._last_value, 2),
                last_write=ready[-1] + ":00",
            )

    def _append_atomic(self, day: str, entries: list[tuple[str, dict[str, float]]]) -> None:
        """Append ``entries`` to the day's CSV using temp-file + rename.

        We always rewrite the entire day file rather than appending in place
        because the cost is trivial (max 375 rows/day) and rewrite gives us
        crash-safety: a torn write produces a corrupt temp file that the
        rename never reaches, and the previous day file stays intact.
        """
        path = self._output_dir / f"india_vix_{day}.csv"
        tmp_path = path.with_suffix(".csv.tmp")

        # Load existing rows (if any) so we don't drop earlier minutes
        existing: list[dict] = []
        if path.exists():
            with open(path) as f:
                reader = csv.DictReader(f)
                existing.extend(reader)

        # Build new row set, deduped by minute (existing wins so we don't
        # clobber a closed minute with a late tick that snuck in)
        seen_minutes = {r["date"][:16] for r in existing}
        new_rows: list[dict] = []
        for minute_iso, bar in entries:
            if minute_iso in seen_minutes:
                continue
            tz = bar.get("tz") or "+0530"
            # Format tz as "+05:30" rather than "+0530"
            if tz and len(tz) == 5 and ":" not in tz:
                tz = f"{tz[:3]}:{tz[3:]}"
            ts = f"{minute_iso}:00{tz}"
            new_rows.append({
                "date": ts,
                "open": round(bar["open"], 2),
                "high": round(bar["high"], 2),
                "low": round(bar["low"], 2),
                "close": round(bar["close"], 2),
                "volume": 0,
                "oi": 0,
            })
            seen_minutes.add(minute_iso)

        if not new_rows and existing:
            return  # nothing changed

        all_rows = sorted(existing + new_rows, key=lambda r: r["date"])

        # Atomic write: temp + fsync + rename
        with open(tmp_path, "w", newline="") as f:
            writer = csv.DictWriter(
                f, fieldnames=["date", "open", "high", "low", "close", "volume", "oi"]
            )
            writer.writeheader()
            writer.writerows(all_rows)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, path)
