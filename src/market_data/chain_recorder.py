"""Option chain snapshot recorder — captures live chain data for future replay.

Records option chain state to CSV files during live/paper trading.
After collecting 2-3 weeks of data, the ReplayBacktestEngine can replay
real option prices instead of synthetic Black-Scholes pricing.

Optionally enriches snapshots with Breeze API data (bid/ask, OI) when configured.
Falls back gracefully to WebSocket-only data if Breeze is unavailable.

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


class BreezeEnricher:
    """Optional Breeze API integration for richer chain snapshots.

    Fetches real-time option chain quotes with bid/ask and OI.
    If not configured or any error occurs, returns empty — caller uses WebSocket data.
    """

    def __init__(self):
        self._breeze = None
        self._available = False
        self._last_error_time = 0
        self._error_cooldown = 300  # 5 min cooldown after errors

    def connect(self) -> bool:
        """Try to connect to Breeze API. Returns True if successful."""
        try:
            api_key = os.environ.get("BREEZE_API_KEY", "")
            api_secret = os.environ.get("BREEZE_API_SECRET", "")
            session_token = os.environ.get("BREEZE_SESSION_TOKEN", "")

            if not (api_key and api_secret and session_token):
                return False

            from breeze_connect import BreezeConnect
            self._breeze = BreezeConnect(api_key=api_key)
            self._breeze.generate_session(api_secret=api_secret, session_token=session_token)
            self._available = True
            logger.info("[CHAIN_RECORDER] Breeze API connected — enriched snapshots enabled")
            return True
        except Exception as e:
            logger.info(f"[CHAIN_RECORDER] Breeze API not available ({e}) — using WebSocket only")
            self._available = False
            return False

    def get_chain_quotes(self, underlying: str, expiry: date) -> dict[tuple[float, str], dict]:
        """Fetch full option chain from Breeze.

        Returns: {(strike, option_type): {ltp, bid, ask, oi, volume}} or empty dict on failure.
        """
        import time as _time

        if not self._available or not self._breeze:
            return {}

        # Cooldown after errors
        now = _time.time()
        if now - self._last_error_time < self._error_cooldown:
            return {}

        try:
            result: dict[tuple[float, str], dict] = {}

            for right, opt_type in [("call", "CE"), ("put", "PE")]:
                resp = self._breeze.get_option_chain_quotes(
                    stock_code=underlying,
                    exchange_code="NFO",
                    product_type="options",
                    expiry_date=f"{expiry.isoformat()}T07:00:00.000Z",
                    right=right,
                    strike_price="",  # all strikes
                )

                if resp and resp.get("Success"):
                    for entry in resp["Success"]:
                        strike = float(entry.get("strike_price", 0))
                        ltp = float(entry.get("ltp", 0) or 0)
                        if strike <= 0:
                            continue

                        result[(strike, opt_type)] = {
                            "ltp": ltp,
                            "bid": float(entry.get("best_bid_price", 0) or 0),
                            "ask": float(entry.get("best_offer_price", 0) or 0),
                            "oi": int(float(entry.get("open_interest", 0) or 0)),
                            "volume": int(entry.get("total_quantity_traded", 0) or 0),
                        }

            return result

        except Exception as e:
            logger.warning(f"[CHAIN_RECORDER] Breeze quote fetch failed: {e}")
            self._last_error_time = _time.time()
            return {}


class ChainSnapshotRecorder:
    """Periodically snapshots option chain to CSV for future backtesting.

    Captures ATM ± num_strikes for all active expiries every interval_seconds.
    One CSV file per trading day, append mode (survives restarts).

    If Breeze API is configured, enriches snapshots with real bid/ask and OI.
    Falls back to WebSocket-only data if Breeze is unavailable.
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
        self._breeze = BreezeEnricher()
        self._heartbeat = heartbeat

    async def start(self) -> None:
        """Start periodic snapshot recording."""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._running = True
        self._breeze.connect()  # Best-effort — falls back to WebSocket if unavailable
        self._task = asyncio.create_task(self._record_loop())
        source = "WebSocket + Breeze" if self._breeze._available else "WebSocket"
        logger.info(
            f"[CHAIN_RECORDER] Started — interval={self._interval}s "
            f"output={self._output_dir} strikes=±{self._num_strikes} source={source}"
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
        """Snapshot all active chains to CSV.

        Merges WebSocket data (greeks, LTP) with Breeze data (bid/ask, OI)
        when available. Falls back to WebSocket-only if Breeze is unavailable.

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
                # Fetch Breeze quotes for this expiry (best-effort)
                breeze_quotes = self._breeze.get_chain_quotes(underlying, expiry)

                for entry in chain.strikes:
                    strike = float(entry.strike)
                    # Only record strikes near ATM
                    if spot > 0 and abs(strike - spot) / spot > 0.10:
                        continue

                    for opt_type, opt_data in [("CE", entry.ce), ("PE", entry.pe)]:
                        if not opt_data:
                            continue

                        g = opt_data.greeks

                        # Start with WebSocket data
                        ltp = float(opt_data.ltp)
                        oi = opt_data.oi
                        volume = opt_data.volume
                        bid = float(opt_data.bid_price)
                        ask = float(opt_data.ask_price)

                        # Enrich with Breeze data if available
                        bq = breeze_quotes.get((strike, opt_type))
                        if bq:
                            # Breeze has better bid/ask and OI
                            if bq["bid"] > 0:
                                bid = bq["bid"]
                            if bq["ask"] > 0:
                                ask = bq["ask"]
                            if bq["oi"] > 0:
                                oi = bq["oi"]
                            if bq["volume"] > 0:
                                volume = bq["volume"]
                            # Use Breeze LTP if WebSocket LTP is zero
                            if ltp <= 0 and bq["ltp"] > 0:
                                ltp = bq["ltp"]

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
