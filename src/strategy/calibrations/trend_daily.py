"""Trend Daily calibration — params + regime confidence hook.

Edit ONLY this file to recalibrate trend_daily.
"""

from __future__ import annotations

from datetime import time
from typing import TYPE_CHECKING

from src.strategy.params import BaseStrategyParams

if TYPE_CHECKING:
    from src.strategy.regime import RegimeDetector


# ─── Pydantic params ────────────────────────────────────────────────


class TrendDailyParams(BaseStrategyParams):
    """Parameters for Trend Daily strategy — multi-day Donchian on NIFTY.

    May 7 2026 — first trend variant in the post-SEBI research arc to
    show positive net PnL on a 360-day train+val smoke (commit b34aefe).
    Daily timeframe escapes intraday microstructure noise and amortizes
    round-trip cost across multi-day holds.

    Cost basis: targets futures cost structure (1 bp round-trip slippage).
    Strategy is intended to trade NIFTY current-month futures, not spot.
    """

    # Donchian
    donchian_lookback: int = 20

    # ATR
    atr_period: int = 14
    atr_floor_pct: float = 0.5            # daily ATR/spot floor
    atr_stop_mult: float = 2.0            # trailing stop = peak ± mult × ATR

    # Risk gates
    vix_entry_min: float = 12.0
    vix_entry_max: float = 22.0
    vix_reduce_above: float = 22.0

    # Multi-day hold
    max_hold_days: int = 30

    # Decision timing — 15:25 IST so MOC orders can be placed before close
    decision_time: time = time(15, 25)

    # Legacy heuristic filters don't apply to directional trend
    pcr_filter_enabled: bool = False
    max_pain_filter_enabled: bool = False

    # V5: NIFTY current-month futures ~₹1L/lot SPAN+ELM (initial margin)
    expected_margin_per_lot_lakhs: float = 1.0


# ─── Regime confidence hook ─────────────────────────────────────────

REGIME_FAMILY = "directional_trend"


def compute_regime_confidence(detector: "RegimeDetector", underlying: str) -> float:
    """0.0-1.0 confidence for TD entry.

    Dispatches to directional-trend family. TD is daily-timeframe so
    the 5-min ADX from the family hook may understate trend strength
    on TD's actual decision timeframe. Override here if the daily-vs-
    intraday mismatch starts showing in the smoke.
    """
    return float(detector.regime_confidence_for_directional_trend(underlying))
