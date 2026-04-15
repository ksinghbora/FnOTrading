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
        """Main recording loop — snapshot every interval."""
        while self._running:
            try:
                await asyncio.sleep(self._interval)
                if not self._running:
                    break

                now = self._clock.now()
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

    def _take_snapshot(self, now: datetime) -> None:
        """Snapshot all active chains to CSV.

        Merges WebSocket data (greeks, LTP) with Breeze data (bid/ask, OI)
        when available. Falls back to WebSocket-only if Breeze is unavailable.
        """
        rows = []

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

                        rows.append({
                            "time": now.isoformat(),
                            "underlying": underlying,
                            "expiry": expiry.isoformat(),
                            "strike": strike,
                            "option_type": opt_type,
                            "ltp": ltp,
                            "iv": g.iv if g else 0,
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
            return

        # Write to daily CSV (append mode)
        today = now.date()
        filepath = self._output_dir / f"chain_{today.isoformat()}.csv"
        file_exists = filepath.exists()

        with open(filepath, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            if not file_exists:
                writer.writeheader()
            writer.writerows(rows)

        self._snapshots_today += 1
        if self._snapshots_today % 10 == 0:
            logger.info(
                f"[CHAIN_RECORDER] Snapshot #{self._snapshots_today} — "
                f"{len(rows)} rows written to {filepath.name}"
            )
