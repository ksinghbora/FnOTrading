"""Trend ITM calibration — params + regime confidence hook.

Edit ONLY this file to recalibrate trend_itm.
"""

from __future__ import annotations

from datetime import time
from typing import TYPE_CHECKING

from src.strategy.params import BaseStrategyParams

if TYPE_CHECKING:
    from src.strategy.regime import RegimeDetector


# ─── Pydantic params ────────────────────────────────────────────────


class TrendITMParams(BaseStrategyParams):
    """Parameters for Trend ITM strategy — Donchian breakout, deep-ITM single-leg.

    May 1 2026 pivot from premium-selling. GDFL corpus has no futures
    ticks — using deep-ITM single-leg options as a futures proxy.
    Delta ~0.95 mimics futures price action; theta is small relative
    to the intrinsic value.

    Signal:
      - 20-bar Donchian channel breakout on 1-min spot bars
      - ATR(14) floor (require minimum tradeable range)
      - VIX 12-22 band
      - 09:30 → 14:30 IST entry window

    Execution:
      - Long bias → BUY a CE strike `itm_offset_pts` BELOW spot
      - Short bias → BUY a PE strike `itm_offset_pts` ABOVE spot
      - Single leg, BUY at ask (debit position)

    Exit:
      - 2× ATR trailing stop on spot
      - 14:45 IST hard time stop (intraday only — no overnight gap)
      - Reverse on opposite-side Donchian breakout
    """

    # ─── VIX band ─────────────────────────────────────────────────
    vix_entry_min: float = 10.0
    vix_entry_max: float = 22.0
    vix_reduce_above: float = 20.0

    # ─── Time gates (IST) ────────────────────────────────────────
    entry_time: time = time(9, 30)            # Skip auction-imbalance noise
    exit_time: time = time(14, 45)            # Hard square-off
    last_entry_time: time = time(14, 30)

    # ─── Donchian breakout ───────────────────────────────────────
    donchian_lookback: int = 20
    breakout_confirmation_pts: float = 5.0
    breakout_atr_mult: float = 1.0            # 0.0 = disabled (v1)

    # ─── ATR(14) Wilder smoothing ────────────────────────────────
    atr_period: int = 14
    atr_floor_pct_of_spot: float = 0.025      # ~6 pts on NIFTY 24K
    atr_stop_mult: float = 3.5                # Trailing stop multiplier
    min_hold_minutes: int = 5

    # Choppy window skip (11:30-13:00 IST is lowest-realised-vol window)
    skip_chop_window_start: time = time(11, 30)
    skip_chop_window_end: time = time(13, 0)

    # ─── ITM strike selection ────────────────────────────────────
    itm_offset_pts: int = 500                 # 500pts ITM at NIFTY 22500 = ~2.2% intrinsic
    itm_max_strike_search_pts: int = 100

    # ─── Risk management ─────────────────────────────────────────
    profit_target_pct: float = 100.0          # Exit when premium doubled
    stop_loss_pct: float = 40.0               # Disaster cap
    max_trades_per_day: int = 3

    # Legacy heuristic filters disabled for directional ITM longs
    pcr_filter_enabled: bool = False
    max_pain_filter_enabled: bool = False

    # V5: deep-ITM single-leg long ~₹0.5L per lot
    expected_margin_per_lot_lakhs: float = 0.5


# ─── Regime confidence hook ─────────────────────────────────────────

REGIME_FAMILY = "directional_trend"


def compute_regime_confidence(detector: "RegimeDetector", underlying: str) -> float:
    """0.0-1.0 confidence for TITM entry.

    Dispatches to directional-trend family. TITM operates on 1-min spot
    bars, matching the family hook's 5-min ADX more closely than TD's
    daily timeframe — so the family default is a better fit here.
    """
    return float(detector.regime_confidence_for_directional_trend(underlying))
