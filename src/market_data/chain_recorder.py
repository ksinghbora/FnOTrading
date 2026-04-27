"""Option chain snapshot recorder — captures live chain data for future replay.

Records option chain state to CSV files during live/paper trading.
After collecting 2-3 weeks of data, the ReplayBacktestEngine can replay
real option prices instead of synthetic Black-Scholes pricing.

Snapshots come from the Kite WebSocket feed (greeks, LTP, OI, bid/ask).

Output: data/chain_snapshots/chain_YYYY-MM-DD.csv
Columns: time,underlying,expiry,strike,option_type,ltp,iv,delta,gamma,theta,vega,oi,volume,bid_price,ask_price
"""

import asyncio
import csv
import logging
import os
from datetime import date, datetime
from pathlib import Path

from src.core.clock import MarketClock
from src.market_data.option_chain import OptionChainBuilder
from src.observability.heartbeat import Heartbeat

logger = logging.getLogger(__name__)


class ChainSnapshotRecorder:
    """Periodically snapshots option chain to CSV for future backtesting.

    Captures ATM ± num_strikes for all active expiries every interval_seconds.
    One CSV file per trading day, atomic-rewrite mode (survives restarts).
    Source: Kite WebSocket feed only.
    """

    def __init__(
        self,
        chain_builder: OptionChainBuilder,
        clock: MarketClock,
        output_dir: str | Path = "data/chain_snapshots",
        interval_seconds: int = 60,
        num_strikes: int = 20,
        heartbeat: Heartbeat | None = None,
    ):
        self._chain_builder = chain_builder
        self._clock = clock
        self._output_dir = Path(output_dir)
        self._interval = interval_seconds
        self._num_strikes = num_strikes
        self._task: asyncio.Task | None = None
        self._running = False
        self._snapshots_today = 0
        self._heartbeat = heartbeat

    async def start(self) -> None:
        """Start periodic snapshot recording."""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._running = True
        self._task = asyncio.create_task(self._record_loop())
        logger.info(
            f"[CHAIN_RECORDER] Started — interval={self._interval}s "
            f"output={self._output_dir} strikes=±{self._num_strikes} source=WebSocket"
        )

    async def stop(self) -> None:
        """Stop recording and flush."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info(
            f"[CHAIN_RECORDER] Stopped — {self._snapshots_today} snapshots recorded today"
        )

    async def _record_loop(self) -> None:
        """Main recording loop — snapshot every interval.

        Apr 2026 fix: refuse to record on weekends + NSE holidays. The 23-day
        replay was contaminated by 6 weekend CSVs (Mar 28/29, Apr 4/5, 11/12)
        because the recorder ran any time the process was up — including
        Saturdays when only the simulator was producing chain data. The
        replay engine then treated those simulator rows as "real" market data.
        """
        while self._running:
            try:
                await asyncio.sleep(self._interval)
                if not self._running:
                    break

                now = self._clock.now()

                # Hard gate: never record on weekends or NSE holidays.
                # is_trading_holiday() returns True for Sat/Sun too.
                if self._clock.is_trading_holiday(now.date()):
                    if self._snapshots_today != 0:
                        # We rolled past midnight without a stop() call — reset counter
                        self._snapshots_today = 0
                    continue

                # Only record during market hours (9:15 - 15:30)
                if now.hour < 9 or (now.hour == 9 and now.minute < 15):
                    continue
                if now.hour > 15 or (now.hour == 15 and now.minute > 30):
                    continue

                self._take_snapshot(now)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[CHAIN_RECORDER] Error in recording loop")

    # ── Snapshot construction ────────────────────────────────────
    def _take_snapshot(self, now: datetime) -> None:
        """Snapshot all active chains to CSV from the WebSocket feed.

        Apr 2026 audit fixes:
          1. Skip-or-flag for degraded snapshots: if every CE+PE in this
             snapshot has ltp == 0 AND bid == 0 AND ask == 0, the broker
             feed is dead — log a clear DEGRADED warning and refuse to
             write the snapshot. Writing junk rows once polluted Mar 26
             with 30,586 all-zero quote rows.
          2. IV fallback when BS inversion fails: keep `iv=0` only when
             greeks are missing entirely; if greeks exist with iv=0 but
             ltp>0, leave iv=0 in the row (downstream knows to skip),
             but log the quote count so we can audit IV-coverage trends.
          3. Atomic write: temp + fsync + rename instead of append-mode.
             A power loss mid-append used to leave a torn final row.
        """
        # Defense-in-depth gate. The loop in `_record_loop` already filters
        # weekends / NSE holidays before calling us, but a recorder process
        # that was started before commit 921235b (which added the loop gate)
        # produced chain_2026-04-18.csv on Saturday morning — the live code
        # in memory was still pre-fix. Checking here means a stale process,
        # a direct test call, or a future refactor can't silently write a
        # weekend file.
        if self._clock.is_trading_holiday(now.date()):
            logger.warning(
                "[CHAIN_RECORDER] Refusing snapshot for non-trading day %s "
                "(weekday=%d) — loop gate should have caught this; "
                "check whether the process was started on pre-fix code.",
                now.date().isoformat(), now.weekday(),
            )
            return

        rows = []
        zero_quote_count = 0
        zero_iv_count = 0
        priceable_count = 0

        for underlying, expiry_chains in self._chain_builder._chains.items():
            spot = float(self._chain_builder._spot_prices.get(underlying, 0))
            if spot <= 0:
                continue

            for expiry, chain in expiry_chains.items():
                for entry in chain.strikes:
                    strike = float(entry.strike)
                    # Only record strikes near ATM
                    if spot > 0 and abs(strike - spot) / spot > 0.10:
                        continue

                    for opt_type, opt_data in [("CE", entry.ce), ("PE", entry.pe)]:
                        if not opt_data:
                            continue

                        g = opt_data.greeks

                        # WebSocket data (single source of truth)
                        ltp = float(opt_data.ltp)
                        oi = opt_data.oi
                        volume = opt_data.volume
                        bid = float(opt_data.bid_price)
                        ask = float(opt_data.ask_price)

                        # Quality counters
                        has_quote = ltp > 0 or bid > 0 or ask > 0
                        if not has_quote:
                            zero_quote_count += 1
                        else:
                            priceable_count += 1
                        iv = g.iv if g else 0
                        if iv <= 0:
                            zero_iv_count += 1

                        rows.append({
                            "time": now.isoformat(),
                            "underlying": underlying,
                            "expiry": expiry.isoformat(),
                            "strike": strike,
                            "option_type": opt_type,
                            "ltp": ltp,
                            "iv": iv,
                            "delta": g.delta if g else 0,
                            "gamma": g.gamma if g else 0,
                            "theta": g.theta if g else 0,
                            "vega": g.vega if g else 0,
                            "oi": oi,
                            "volume": volume,
                            "bid_price": bid,
                            "ask_price": ask,
                        })

        if not rows:
            logger.warning(
                "[CHAIN_RECORDER] DEGRADED: no chain rows produced at %s "
                "(chain builder empty?)",
                now.isoformat(),
            )
            return

        # Skip-or-flag: refuse to write if every leg is unpriceable.
        # When the WebSocket is dead but the recorder is still ticking,
        # the loop produces hundreds of all-zero rows. Skipping keeps the
        # day's CSV honest at the cost of a hole the auditor can see.
        total_legs = len(rows)
        if priceable_count == 0:
            logger.warning(
                "[CHAIN_RECORDER] DEGRADED: snapshot at %s has 0 priceable "
                "legs out of %d — refusing to write (broker feed likely down)",
                now.isoformat(), total_legs,
            )
            return

        # Soft warning when most legs are unpriceable but at least one is.
        # Keeps the snapshot but flags it for downstream.
        priceable_pct = 100.0 * priceable_count / total_legs
        if priceable_pct < 50.0:
            logger.warning(
                "[CHAIN_RECORDER] PARTIAL: snapshot at %s has only %d/%d "
                "priceable legs (%.0f%%)",
                now.isoformat(), priceable_count, total_legs, priceable_pct,
            )

        # IV-coverage trend audit (info only)
        iv_pct = 100.0 * (total_legs - zero_iv_count) / total_legs
        if iv_pct < 80.0 and self._snapshots_today % 10 == 0:
            logger.info(
                "[CHAIN_RECORDER] IV coverage %.0f%% at %s (BS inversion fails for some legs)",
                iv_pct, now.isoformat(),
            )

        # Atomic rewrite of the day's CSV (temp + fsync + rename).
        # Append mode is unsafe under crash: a torn write leaves a mangled
        # final row that breaks downstream parsers.
        self._atomic_append(now.date(), rows)

        self._snapshots_today += 1
        if self._snapshots_today % 10 == 0:
            logger.info(
                "[CHAIN_RECORDER] Snapshot #%d — %d rows (priceable=%d, iv_pct=%.0f%%)",
                self._snapshots_today, len(rows), priceable_count, iv_pct,
            )

        # Heartbeat for the external watchdog. The recorder owns these fields
        # and the watchdog reads them — see src/observability/heartbeat.py.
        if self._heartbeat is not None:
            self._heartbeat.update(
                "chain_recorder",
                snapshots_today=self._snapshots_today,
                rows_in_last_snapshot=len(rows),
                priceable_pct_last=round(priceable_pct, 1),
                iv_pct_last=round(iv_pct, 1),
                last_write=now.isoformat(timespec="seconds"),
            )

    def _atomic_append(self, day: date, new_rows: list[dict]) -> None:
        """Append `new_rows` to the day's CSV atomically.

        Reads the existing file, concatenates, writes to a temp file, fsyncs,
        and renames over the day file. Cost is O(rows-so-far) per snapshot
        — at 60s interval and ~600 legs/snapshot, the day file maxes out at
        ~225K rows / 22 MB, which rewrites in well under a second on SSD.
        """
        filepath = self._output_dir / f"chain_{day.isoformat()}.csv"
        tmp_path = filepath.with_suffix(".csv.tmp")

        existing: list[dict] = []
        if filepath.exists():
            with open(filepath) as f:
                reader = csv.DictReader(f)
                existing.extend(reader)

        all_rows = existing + new_rows
        fieldnames = list(new_rows[0].keys())

        with open(tmp_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_rows)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, filepath)
