"""Shared backtest infrastructure.

Hosts the non-BS plumbing reused by both the GDFL real-tick path
(`src.backtest.engine.BacktestEngine`) and the chain-snapshot replay path
(`src.backtest.replay_engine.ReplayBacktestEngine`):

- :class:`BacktestClock` — a :class:`MarketClock` that returns simulated time.
- :func:`import_strategies` — triggers `@register_strategy` side-effect imports.
- :func:`trading_days` — calendar-aware trading-day iterator.

Also hosts the historical CSV loaders (`_load_spot_csv`, `_load_vix_csv`)
and a handful of module-level constants / legacy BS helpers that the
replay engine still uses for its BS fallback path. Those are slated for
removal once the replay engine is either ported to real-tick data or
retired; for now they live here to keep the import surface stable after
Phase 1 of the BS removal.
"""

from __future__ import annotations

import csv
import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytz
from scipy.stats import norm as sp_norm

from src.core.clock import MarketClock
from src.core.constants import INDIA_VIX_TOKEN, RISK_FREE_RATE
from src.core.models import Greeks, OptionData, Tick
from src.core.types import OptionType
from src.market_data.simulator import BANKNIFTY_SPOT_TOKEN, NIFTY_SPOT_TOKEN
from src.options.skew import ParametricSkew

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# ─── Module-level constants ────────────────────────────────────────
_TOKEN_BASE = 600_000
_SPOT_TOKENS = {"NIFTY": NIFTY_SPOT_TOKEN, "BANKNIFTY": BANKNIFTY_SPOT_TOKEN}
_STRIKE_STEPS = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50}
_DEFAULT_SPOTS = {"NIFTY": 22500.0, "BANKNIFTY": 48000.0}
_NUM_STRIKES = 30  # ±30 strikes from ATM
_FUT_TOKEN = 500_000  # Fixed token for synthetic futures


class BacktestClock(MarketClock):
    """Market clock returning simulated time for backtesting."""

    def __init__(self):
        super().__init__()
        self._sim_now: datetime | None = None
        self._expiry_override: dict[str, list[date]] = {}  # underlying -> available expiries

    def set_time(self, dt: datetime) -> None:
        self._sim_now = dt

    def set_expiries(self, underlying: str, expiries: list[date]) -> None:
        """Override the calendar-based expiry lookup with real listed expiries.

        Used when GDFL (or other real data) dictates which expiries exist,
        which can differ from the default Tuesday weekly cadence (e.g. due
        to holidays or unscheduled shifts).
        """
        self._expiry_override[underlying] = sorted(expiries)

    def now(self) -> datetime:
        if self._sim_now:
            return self._sim_now
        return super().now()

    def is_market_open(self) -> bool:
        return True  # Always open during backtest

    def next_expiry(self, underlying: str) -> date:
        override = self._expiry_override.get(underlying)
        if override:
            today = self.now().date()
            future = [e for e in override if e >= today]
            if future:
                return future[0]
        return super().next_expiry(underlying)


def import_strategies() -> None:
    """Import strategy modules to trigger @register_strategy decorators."""
    import src.strategy.implementations.short_straddle  # noqa: F401
    import src.strategy.implementations.short_strangle  # noqa: F401
    try:
        import src.strategy.implementations.iron_condor  # noqa: F401
        import src.strategy.implementations.iron_butterfly  # noqa: F401
        import src.strategy.implementations.long_calendar  # noqa: F401
        import src.strategy.implementations.delta_neutral  # noqa: F401
        import src.strategy.implementations.trend_debit_spread  # noqa: F401
        import src.strategy.implementations.trend_itm  # noqa: F401
        import src.strategy.implementations.portfolio_strategy  # noqa: F401
        import src.strategy.implementations.orchestrator  # noqa: F401
    except ImportError:
        pass


def trading_days(start: date, num_days: int, clock: MarketClock) -> list[date]:
    """Generate a list of trading days starting from ``start``."""
    days: list[date] = []
    current = start
    while len(days) < num_days:
        if current.weekday() < 5 and not clock.is_trading_holiday(current):
            days.append(current)
        current += timedelta(days=1)
    return days


# ─── Historical CSV loaders (used by replay_engine) ────────────────


def _load_spot_csv(filepath: str | Path) -> dict[date, list[dict]]:
    """Load spot CSV and group by trading day.

    Returns: {date: [{time, open, high, low, close, volume}, ...]}
    """
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"Spot data file not found: {filepath}")

    day_candles: dict[date, list[dict]] = defaultdict(list)

    with open(filepath) as f:
        reader = csv.DictReader(f)
        for row in reader:
            dt = datetime.fromisoformat(row["date"].replace("+05:30", "+05:30"))
            if dt.tzinfo is None:
                dt = IST.localize(dt)
            day_candles[dt.date()].append({
                "datetime": dt,
                "time": dt.time(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row.get("volume", 0)),
            })

    # Sort candles within each day
    for day in day_candles:
        day_candles[day].sort(key=lambda c: c["datetime"])

    return dict(day_candles)


def _load_vix_csv(filepath: str | Path) -> dict[date, list[dict]]:
    """Load VIX CSV and group by trading day."""
    filepath = Path(filepath)
    if not filepath.exists():
        logger.warning(f"VIX data file not found: {filepath}, will use default VIX=14.5")
        return {}

    day_candles: dict[date, list[dict]] = defaultdict(list)

    with open(filepath) as f:
        reader = csv.DictReader(f)
        for row in reader:
            dt = datetime.fromisoformat(row["date"].replace("+05:30", "+05:30"))
            if dt.tzinfo is None:
                dt = IST.localize(dt)
            day_candles[dt.date()].append({
                "datetime": dt,
                "close": float(row["close"]),
            })

    for day in day_candles:
        day_candles[day].sort(key=lambda c: c["datetime"])

    return dict(day_candles)


# ─── Legacy BS helpers ─────────────────────────────────────────────
# Used only by src.backtest.replay_engine's BS fallback path. Do not
# call from new code. Targeted for removal once replay_engine either
# drops the BS fallback or is replaced by a real-tick source.


def _register_options(chain_builder, underlying, spot, step, num_strikes, expiry, alloc_token):
    """Register option instruments in the chain builder."""
    atm = round(spot / step) * step
    for i in range(-num_strikes, num_strikes + 1):
        strike = atm + i * step
        strike_dec = Decimal(str(strike))
        for opt_type in (OptionType.CE, OptionType.PE):
            token = alloc_token(underlying, strike, opt_type.value)
            expiry_str = expiry.strftime("%y%b").upper()
            symbol = f"{underlying}{expiry_str}{int(strike)}{opt_type.value}"
            chain_builder.register_option(
                token, underlying, expiry, strike_dec, opt_type, symbol,
            )


def _fit_daily_skew(
    chain_builder,
    underlying: str,
    expiry: date,
    spot: float,
    fallback: "ParametricSkew",
) -> "ParametricSkew":
    """Fit ``ParametricSkew`` from the current chain's observed IVs.

    Falls back to ``fallback`` (or ``nifty_typical``) if the chain has
    fewer than 5 valid OTM strike IVs.
    """
    try:
        chain = chain_builder.get_chain(underlying, expiry)
    except Exception:
        return fallback or ParametricSkew.nifty_typical()
    if chain is None:
        return fallback or ParametricSkew.nifty_typical()

    strikes: list[float] = []
    ivs: list[float] = []
    atm_iv_samples: list[float] = []
    atm_strike = float(chain.atm_strike) if chain.atm_strike else spot
    for entry in getattr(chain, "strikes", []):
        try:
            k = float(entry.strike)
        except Exception:
            continue
        for opt in (entry.ce, entry.pe):
            if opt is None or not getattr(opt, "greeks", None):
                continue
            iv = float(opt.greeks.iv or 0.0)
            if iv <= 0:
                continue
            strikes.append(k)
            ivs.append(iv)
            if abs(k - atm_strike) < 1e-6:
                atm_iv_samples.append(iv)

    if not strikes:
        return fallback or ParametricSkew.nifty_typical()
    atm_iv = (
        sum(atm_iv_samples) / len(atm_iv_samples)
        if atm_iv_samples
        else sum(ivs) / len(ivs)
    )
    return ParametricSkew.fit_from_observed(
        strikes=strikes,
        ivs=ivs,
        atm_iv=atm_iv,
        spot=spot,
    )


def _update_market(
    feed, broker, chain_builder, portfolio,
    underlying, expiry, spot, vix, now,
    spot_token, step, num_strikes, T, iv_base,
    option_tokens, alloc_token,
    *,
    skew_model: "ParametricSkew | None" = None,
):
    """Vectorized BS market update — used only by replay_engine's fallback."""
    if skew_model is None:
        skew_model = ParametricSkew.nifty_typical()
    spot_dec = Decimal(str(round(spot, 2)))
    r = RISK_FREE_RATE

    # ─── Spot price ───────────────────────────────────────────
    chain_builder._spot_prices[underlying] = spot_dec
    feed._latest_ticks[spot_token] = Tick.model_construct(
        instrument_token=spot_token,
        tradingsymbol=underlying,
        timestamp=now,
        ltp=spot_dec,
        volume=0, oi=0,
        bid_price=Decimal("0"), ask_price=Decimal("0"),
        bid_qty=0, ask_qty=0,
        high=Decimal("0"), low=Decimal("0"),
        open=Decimal("0"), close=Decimal("0"),
    )
    broker.set_ltp(underlying, round(spot, 2))

    # ─── Synthetic futures (for delta_neutral hedging) ──────
    month_map = {
        1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
        7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
    }
    yy = now.year % 100
    mmm = month_map[now.month]
    fut_symbol = f"{underlying}{yy}{mmm}FUT"
    fut_price = round(spot * (1 + RISK_FREE_RATE * T), 2)
    fut_dec = Decimal(str(fut_price))
    feed._latest_ticks[_FUT_TOKEN] = Tick.model_construct(
        instrument_token=_FUT_TOKEN,
        tradingsymbol=fut_symbol,
        timestamp=now,
        ltp=fut_dec,
        volume=0, oi=0,
        bid_price=fut_dec, ask_price=fut_dec,
        bid_qty=0, ask_qty=0,
        high=fut_dec, low=fut_dec,
        open=fut_dec, close=fut_dec,
    )
    broker.set_ltp(fut_symbol, fut_price)

    # ─── VIX ──────────────────────────────────────────────────
    vix_dec = Decimal(str(round(vix, 2)))
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

    # ─── Option chain (vectorized) ────────────────────────────
    chain = chain_builder.get_chain(underlying, expiry)
    if not chain:
        return

    chain.spot_price = spot_dec
    atm = round(spot / step) * step
    chain.atm_strike = Decimal(str(atm))

    # Build strike array
    n = 2 * num_strikes + 1
    strikes_arr = np.empty(n)
    for idx in range(n):
        strikes_arr[idx] = atm + (idx - num_strikes) * step

    # Vectorized BS pricing for all strikes at once.
    # Per-strike IV from parametric skew (iv_atm anchor = iv_base).
    S_arr = np.full(n, spot)
    T_safe = max(T, 1e-10)
    atm_iv = max(iv_base, 1e-10)
    sigma_arr = np.maximum(skew_model.apply_vec(atm_iv, strikes_arr, spot), 1e-10)
    sqrt_T = math.sqrt(T_safe)
    exp_rT = math.exp(-r * T_safe)

    d1_arr = (np.log(S_arr / strikes_arr) + (r + 0.5 * sigma_arr ** 2) * T_safe) / (
        sigma_arr * sqrt_T
    )
    d2_arr = d1_arr - sigma_arr * sqrt_T
    N_d1 = sp_norm.cdf(d1_arr)
    N_d2 = sp_norm.cdf(d2_arr)
    N_neg_d1 = 1.0 - N_d1
    N_neg_d2 = 1.0 - N_d2
    n_d1_pdf = sp_norm.pdf(d1_arr)

    ce_prices_arr = np.maximum(0.05, S_arr * N_d1 - strikes_arr * exp_rT * N_d2)
    pe_prices_arr = np.maximum(0.05, strikes_arr * exp_rT * N_neg_d2 - S_arr * N_neg_d1)

    # Vectorized Greeks — no gamma cap (see src/options/greeks.py).
    gamma_denom = S_arr * sigma_arr * sqrt_T
    gamma_arr = np.where(
        gamma_denom < 1e-8, np.inf, n_d1_pdf / np.where(gamma_denom < 1e-8, 1.0, gamma_denom)
    )
    common_theta = -(S_arr * n_d1_pdf * sigma_arr) / (2 * sqrt_T)
    ce_theta_arr = (common_theta - r * strikes_arr * exp_rT * N_d2) / 365
    pe_theta_arr = (common_theta + r * strikes_arr * exp_rT * N_neg_d2) / 365
    vega_arr = S_arr * n_d1_pdf * sqrt_T / 100
    ce_rho_arr = strikes_arr * T_safe * exp_rT * N_d2 / 100
    pe_rho_arr = -strikes_arr * T_safe * exp_rT * N_neg_d2 / 100

    # Synthetic OI (vectorized)
    distance_arr = np.abs(strikes_arr - spot) / spot if spot > 0 else np.zeros(n)
    oi_arr = np.maximum(1000, 50000 * np.exp(-distance_arr * 30)).astype(int)

    # Expiry string (computed once)
    exp_str = expiry.strftime("%y%b").upper()
    _SPREAD_MIN = Decimal("0.05")
    _SPREAD_FACTOR = Decimal("0.01")

    # Populate chain entries from vectorized results
    for idx in range(n):
        strike = float(strikes_arr[idx])
        strike_dec = Decimal(str(int(strike))) if strike == int(strike) else Decimal(str(strike))

        entry = chain_builder._find_or_create_entry(chain, strike_dec)
        oi_val = int(oi_arr[idx])

        for opt_str, price_val, delta_val, theta_val, rho_val in (
            ("CE", float(ce_prices_arr[idx]), float(N_d1[idx]), float(ce_theta_arr[idx]), float(ce_rho_arr[idx])),
            ("PE", float(pe_prices_arr[idx]), float(N_d1[idx] - 1), float(pe_theta_arr[idx]), float(pe_rho_arr[idx])),
        ):
            key = (underlying, strike, opt_str)
            if key not in option_tokens:
                token = alloc_token(underlying, strike, opt_str)
                opt_type_enum = OptionType.CE if opt_str == "CE" else OptionType.PE
                sym = f"{underlying}{exp_str}{int(strike)}{opt_str}"
                chain_builder.register_option(
                    token, underlying, expiry, strike_dec, opt_type_enum, sym,
                )
            else:
                token = option_tokens[key]

            symbol = f"{underlying}{exp_str}{int(strike)}{opt_str}"
            price_dec = Decimal(str(round(price_val, 2)))
            spread = max(_SPREAD_MIN, price_dec * _SPREAD_FACTOR)
            bid = max(_SPREAD_MIN, price_dec - spread)
            ask = price_dec + spread
            opt_type_enum = OptionType.CE if opt_str == "CE" else OptionType.PE

            # round() calls removed from Greeks construction in the inner loop.
            # Rounding was for display only (4/6 decimal places). Strategy logic
            # uses comparisons (delta > 0.3) where sub-0.0001 precision is
            # irrelevant. Saves ~6 builtins.round × 2 sides × N_strikes × 750
            # ticks/day ≈ 270K–540K calls per backtest day.
            # gamma special case: inf is preserved for 0DTE near-expiry; finite
            # values no longer rounded since they feed only into budget checks
            # that compare against broad thresholds (not exact equality).
            gamma_val = float(gamma_arr[idx])
            greeks = Greeks(
                delta=delta_val,
                gamma=gamma_val,
                theta=theta_val,
                vega=float(vega_arr[idx]),
                rho=rho_val,
                iv=float(sigma_arr[idx]),
            )

            opt_data = OptionData.model_construct(
                tradingsymbol=symbol,
                instrument_token=token,
                strike=strike_dec,
                option_type=opt_type_enum,
                expiry=expiry,
                ltp=price_dec,
                bid_price=bid,
                ask_price=ask,
                volume=oi_val // 3,
                oi=oi_val,
                greeks=greeks,
            )

            if opt_str == "CE":
                entry.ce = opt_data
            else:
                entry.pe = opt_data

            feed._latest_ticks[token] = Tick.model_construct(
                instrument_token=token,
                tradingsymbol=symbol,
                timestamp=now,
                ltp=price_dec,
                bid_price=bid,
                ask_price=ask,
                volume=oi_val // 3,
                oi=oi_val,
                bid_qty=100, ask_qty=100,
                high=price_dec, low=price_dec,
                open=price_dec, close=price_dec,
            )
            # round() removed: broker stores price as float for fill simulation;
            # sub-cent precision doesn't affect execution logic.
            broker.set_ltp(symbol, price_val)

    # Chain aggregates
    chain.total_ce_oi = sum(e.ce.oi for e in chain.strikes if e.ce)
    chain.total_pe_oi = sum(e.pe.oi for e in chain.strikes if e.pe)
    if chain.total_ce_oi > 0:
        chain.pcr_oi = chain.total_pe_oi / chain.total_ce_oi
    from src.options.chain_analyzer import compute_max_pain
    chain.max_pain = compute_max_pain(chain)
    chain.updated_at = now

    # Update portfolio LTPs for open positions
    for key, pos in portfolio._positions._positions.items():
        cached = feed._latest_ticks.get(pos.instrument_token)
        if cached:
            pos.ltp = cached.ltp
