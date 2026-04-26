"""GDFL-driven market data source for BacktestEngine.

Replaces the synthetic Black-Scholes `_update_market()` path in
`src/backtest/engine.py` with real tick-aggregated data loaded from
per-day parquet files produced by `src/backtest/gdfl_loader.py`.

Strategies see the exact same `OptionChain` / `Tick` / broker-LTP caches
they see in the synthetic path — only the values change (real bid/ask/OI/LTP
instead of BS prices).
"""

import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from src.core.constants import INDIA_VIX_TOKEN, RISK_FREE_RATE
from src.core.models import Greeks, OptionData, Tick
from src.core.types import OptionType
from src.options.greeks import compute_greeks_vec
from src.options.iv import compute_iv_vec

logger = logging.getLogger(__name__)

_SPREAD_MIN = Decimal("0.05")


class GDFLMarketSource:
    """Feeds GDFL parquet data into BacktestEngine's runtime caches.

    Usage inside the engine:
        source = GDFLMarketSource("data/gdfl_snapshots", "NIFTY", spot_token)
        source.prepare_day(day, chain_builder, alloc_token)
        for minute in ...:
            source.apply(now, feed, broker, chain_builder, portfolio, option_tokens)
    """

    def __init__(
        self,
        parquet_dir: Path | str,
        underlying: str,
        spot_token: int,
    ):
        self.parquet_dir = Path(parquet_dir)
        self.underlying = underlying
        self.spot_token = spot_token
        self._day_df: pd.DataFrame | None = None
        self._day_date: date | None = None
        # Per-minute dict: ts -> row_df (subset of contracts active at that time)
        self._minute_slices: dict[pd.Timestamp, pd.DataFrame] = {}
        self._spot_by_minute: dict[pd.Timestamp, float] = {}
        self._vix_by_minute: dict[pd.Timestamp, float] = {}

    # ─── Day lifecycle ─────────────────────────────────────────────────

    def available_days(self) -> list[date]:
        """Return sorted list of trading days with parquet available."""
        prefix = f"gdfl_{self.underlying.lower()}_"
        days = []
        for p in self.parquet_dir.glob(f"{prefix}*.parquet"):
            stem = p.stem.removeprefix(prefix)
            try:
                days.append(date.fromisoformat(stem))
            except ValueError:
                continue
        return sorted(days)

    def load_day(self, day: date) -> bool:
        """Load parquet for `day` into memory. Returns False if missing."""
        fname = f"gdfl_{self.underlying.lower()}_{day.isoformat()}.parquet"
        path = self.parquet_dir / fname
        if not path.exists():
            logger.warning(f"[GDFL] Missing parquet: {path}")
            return False

        df = pq.read_table(path).to_pandas()
        # Ensure time is tz-aware (parquet preserves tz but pandas sometimes normalizes)
        if df["time"].dt.tz is None:
            df["time"] = df["time"].dt.tz_localize("Asia/Kolkata")

        self._day_df = df
        self._day_date = day
        # Pre-group by minute for O(1) lookup in apply()
        self._minute_slices = {ts: sub for ts, sub in df.groupby("time", sort=False)}
        # Spot/vix are denormalized — take first row per minute
        per_minute = df.drop_duplicates("time")[["time", "spot", "vix"]]
        self._spot_by_minute = dict(zip(per_minute["time"], per_minute["spot"]))
        self._vix_by_minute = dict(zip(per_minute["time"], per_minute["vix"]))

        expiries = sorted(df["expiry"].unique())
        logger.info(
            f"[GDFL] Loaded {fname}: {len(df):,} rows, {len(self._minute_slices)} minutes, "
            f"expiries={expiries}"
        )
        return True

    def expiries_for_day(self) -> list[date]:
        if self._day_df is None:
            return []
        return sorted(self._day_df["expiry"].unique())

    def strikes_for_day(self) -> list[float]:
        if self._day_df is None:
            return []
        return sorted(self._day_df["strike"].unique())

    # ─── Pre-register options with chain_builder ───────────────────────

    def register_options(self, chain_builder, alloc_token) -> dict[tuple[str, float, str], int]:
        """Pre-register every (expiry, strike, side) in the day with chain_builder.

        Mirrors _register_options() in engine.py so strategies find their
        expected instruments on first tick.
        """
        if self._day_df is None:
            return {}

        option_tokens: dict[tuple[str, float, str], int] = {}
        for expiry in sorted(self._day_df["expiry"].unique()):
            exp_str = pd.Timestamp(expiry).strftime("%y%b").upper()
            for strike in sorted(self._day_df[self._day_df["expiry"] == expiry]["strike"].unique()):
                for ot in ("CE", "PE"):
                    token = alloc_token(self.underlying, float(strike), ot)
                    option_tokens[(self.underlying, float(strike), ot)] = token
                    symbol = f"{self.underlying}{exp_str}{int(strike)}{ot}"
                    strike_dec = Decimal(str(int(strike))) if strike == int(strike) else Decimal(str(strike))
                    opt_type_enum = OptionType.CE if ot == "CE" else OptionType.PE
                    chain_builder.register_option(
                        token, self.underlying, expiry, strike_dec, opt_type_enum, symbol,
                    )
        return option_tokens

    # ─── Per-minute application ────────────────────────────────────────

    def apply(
        self,
        now: datetime,
        feed,
        broker,
        chain_builder,
        portfolio,
        option_tokens: dict[tuple[str, float, str], int],
    ) -> tuple[float | None, float | None]:
        """Write the current minute's snapshot into all runtime caches.

        Returns (spot, vix) for the caller to use in strategy signal paths.
        If the minute has no data, returns (None, None) and makes no writes.
        """
        # Our time keys are tz-aware pd.Timestamps; convert now to match
        ts_key = pd.Timestamp(now)
        if ts_key.tz is None:
            ts_key = ts_key.tz_localize("Asia/Kolkata")

        slice_df = self._minute_slices.get(ts_key)
        spot = self._spot_by_minute.get(ts_key)
        vix = self._vix_by_minute.get(ts_key)

        if slice_df is None or spot is None or not np.isfinite(spot):
            return None, None

        # ─── Spot ─────────────────────────────────────────────────
        spot_dec = Decimal(str(round(float(spot), 2)))
        chain_builder._spot_prices[self.underlying] = spot_dec
        feed._latest_ticks[self.spot_token] = Tick.model_construct(
            instrument_token=self.spot_token,
            tradingsymbol=self.underlying,
            timestamp=now,
            ltp=spot_dec,
            volume=0, oi=0,
            bid_price=Decimal("0"), ask_price=Decimal("0"),
            bid_qty=0, ask_qty=0,
            high=Decimal("0"), low=Decimal("0"),
            open=Decimal("0"), close=Decimal("0"),
        )
        broker.set_ltp(self.underlying, round(float(spot), 2))

        # ─── VIX ──────────────────────────────────────────────────
        vix_val = float(vix) if vix is not None and np.isfinite(vix) else 15.0
        vix_dec = Decimal(str(round(vix_val, 2)))
        feed._latest_ticks[INDIA_VIX_TOKEN] = Tick.model_construct(
            instrument_token=INDIA_VIX_TOKEN,
            tradingsymbol="INDIA VIX",
            timestamp=now,
            ltp=vix_dec,
            volume=0, oi=0,
            bid_price=Decimal("0"), ask_price=Decimal("0"),
            bid_qty=0, ask_qty=0,
            high=Decimal("0"), low=Decimal("0"),
            open=Decimal("0"), close=Decimal("0"),
        )

        # ─── Option chain: batch compute IV + Greeks, write to chain ──
        # Filter out rows with no trade yet
        valid = slice_df.dropna(subset=["ltp", "bid", "ask"])
        if valid.empty:
            return float(spot), vix_val

        # Group per expiry for T computation
        for expiry, exp_group in valid.groupby("expiry", sort=False):
            exp_date = pd.Timestamp(expiry).date()
            T = max(
                1 / (365 * 24),
                (exp_date - now.date()).days / 365
                - (now.hour * 60 + now.minute - 9 * 60 - 15) / (375 * 365),
            )

            strikes = exp_group["strike"].values
            ltps = exp_group["ltp"].values
            bids = exp_group["bid"].values
            asks = exp_group["ask"].values
            ois = exp_group["oi"].values
            vols = exp_group["volume"].values
            ots = exp_group["option_type"].values

            # Vectorized IV solve (shared S, T, r across rows). NaN for rows
            # that failed NR + brentq fallback are clamped to 0.01 below so
            # downstream Greeks don't explode.
            K_arr = strikes.astype(float)
            ltps_arr = ltps.astype(float)
            is_call = (ots == "CE")
            ivs = compute_iv_vec(
                ltps_arr, float(spot), K_arr, T, RISK_FREE_RATE, is_call,
            )
            ivs = np.where(np.isfinite(ivs) & (ivs > 0), ivs, 0.01)

            # Vectorized Greeks
            S_arr = np.full(len(strikes), float(spot))
            T_arr = np.full(len(strikes), T)
            greeks_dict = compute_greeks_vec(S_arr, K_arr, T_arr, RISK_FREE_RATE, ivs, is_call)

            chain = chain_builder.get_chain(self.underlying, exp_date)
            if chain is None:
                continue
            chain.spot_price = spot_dec
            # ATM = nearest listed strike
            step = 50.0 if self.underlying == "NIFTY" else 100.0
            atm = round(float(spot) / step) * step
            chain.atm_strike = Decimal(str(int(atm)))

            exp_str = pd.Timestamp(expiry).strftime("%y%b").upper()

            for i in range(len(strikes)):
                strike = float(strikes[i])
                ot = ots[i]
                strike_dec = Decimal(str(int(strike))) if strike == int(strike) else Decimal(str(strike))
                opt_type_enum = OptionType.CE if ot == "CE" else OptionType.PE

                symbol = f"{self.underlying}{exp_str}{int(strike)}{ot}"
                token = option_tokens.get((self.underlying, strike, ot))
                if token is None:
                    continue

                # round() for Decimal(str()) conversion: necessary to avoid
                # floating-point representation noise (e.g., 100.00000000001)
                # appearing in the Decimal string from raw float market data.
                ltp_f = float(ltps[i])
                bid_f = float(bids[i])
                ask_f = float(asks[i])
                ltp_dec = Decimal(str(round(ltp_f, 2)))
                bid_dec = max(_SPREAD_MIN, Decimal(str(round(bid_f, 2))))
                ask_dec = max(_SPREAD_MIN, Decimal(str(round(ask_f, 2))))

                # Greeks round() calls removed from inner loop — rounding was
                # for display only (4 decimal places). The values from
                # compute_greeks_vec are already numerically stable; strategy
                # logic uses comparisons (e.g., delta > 0.3) where extra
                # decimal precision is irrelevant. This saves ~6 builtin.round
                # calls × N_strikes × 750 ticks/day = ~450K–900K calls/day.
                greeks = Greeks(
                    delta=float(greeks_dict["delta"][i]),
                    gamma=float(greeks_dict["gamma"][i]),
                    theta=float(greeks_dict["theta"][i]),
                    vega=float(greeks_dict["vega"][i]),
                    rho=float(greeks_dict["rho"][i]),
                    iv=float(ivs[i]),
                )

                opt_data = OptionData.model_construct(
                    tradingsymbol=symbol,
                    instrument_token=token,
                    strike=strike_dec,
                    option_type=opt_type_enum,
                    expiry=exp_date,
                    ltp=ltp_dec,
                    bid_price=bid_dec,
                    ask_price=ask_dec,
                    volume=int(vols[i]),
                    oi=int(ois[i]),
                    greeks=greeks,
                )

                entry = chain_builder._find_or_create_entry(chain, strike_dec)
                if ot == "CE":
                    entry.ce = opt_data
                else:
                    entry.pe = opt_data

                feed._latest_ticks[token] = Tick.model_construct(
                    instrument_token=token,
                    tradingsymbol=symbol,
                    timestamp=now,
                    ltp=ltp_dec,
                    bid_price=bid_dec,
                    ask_price=ask_dec,
                    volume=int(vols[i]),
                    oi=int(ois[i]),
                    bid_qty=100, ask_qty=100,
                    high=ltp_dec, low=ltp_dec,
                    open=ltp_dec, close=ltp_dec,
                )
                # round() removed from broker.set_ltp: broker stores floats
                # for fill simulation; sub-cent precision has no effect on
                # order execution logic but saved ~750 × N_strikes calls/day.
                broker.set_ltp(symbol, ltp_f)

            # Chain aggregates
            chain.total_ce_oi = sum(e.ce.oi for e in chain.strikes if e.ce)
            chain.total_pe_oi = sum(e.pe.oi for e in chain.strikes if e.pe)
            if chain.total_ce_oi > 0:
                chain.pcr_oi = chain.total_pe_oi / chain.total_ce_oi
            from src.options.chain_analyzer import compute_max_pain
            chain.max_pain = compute_max_pain(chain)
            chain.updated_at = now

        # Refresh LTPs on open positions
        for pos in portfolio._positions._positions.values():
            cached = feed._latest_ticks.get(pos.instrument_token)
            if cached:
                pos.ltp = cached.ltp

        return float(spot), vix_val
