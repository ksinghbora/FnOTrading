"""Trend Debit Spread calibration — params + scoring + regime confidence hook.

Edit ONLY this file to recalibrate trend_debit_spread.
"""

from __future__ import annotations

from datetime import time
from typing import TYPE_CHECKING

from src.strategy.params import BaseStrategyParams
from src.strategy.scoring import ScoreConfig

if TYPE_CHECKING:
    from src.strategy.regime import RegimeDetector


# ─── Pydantic params ────────────────────────────────────────────────


class TrendDebitSpreadParams(BaseStrategyParams):
    """Parameters for Trend Debit Spread strategy.

    Buys debit spreads (bull call or bear put) on morning range
    breakouts. Profits from trending markets that hurt premium sellers.
    """

    # Trend benefits from elevated vol — debit spreads cheaper as IV rises.
    # Override base 22 cap because trend works through stressed regimes.
    vix_entry_min: float = 12.0
    vix_entry_max: float = 25.0
    entry_time: time = time(10, 0)            # Wait for opening volatility to settle
    exit_time: time = time(15, 0)
    breakout_confirmation_pct: float = 0.7
    spread_width_strikes: int = 2
    stop_loss_pct: float = 50.0               # Disaster cap (trail is the active exit)
    profit_target_pct: float = 50.0
    trailing_stop_pct: float = 15.0           # Active exit
    max_trades_per_day: int = 1               # Avoid whipsaw re-entries
    oi_confirm: bool = True                   # Require OI level breach to confirm
    log_only: bool = False

    # V5: bull-call / bear-put debit spread on NIFTY weekly ≈ ₹0.4L/lot
    expected_margin_per_lot_lakhs: float = 0.4


# ─── Legacy 0-100 scoring config ────────────────────────────────────

TREND_DEBIT_SPREAD_CONFIG = ScoreConfig(
    name="trend_debit_spread",
    # Trend needs VIX for option premium to be worth buying
    vix_bands=[
        (16, 25, 25, "good for trend"),
        (12, 16, 15, "adequate"),
        (25, 35, 10, "volatile(cheap spreads)"),
        (0, 12, 0, "too calm"),
        (35, 50, 5, "extreme"),
    ],
    # Trend wants WIDE range (breakout)
    range_invert=True,
    range_tight_max=0.5,
    range_tight_pts=25,
    range_moderate_max=0.3,
    range_moderate_pts=12,
    range_wide_max=0.8,
    range_wide_pts=5,
    # Trend wants directional move
    move_invert=True,
    move_flat_max=0.3,
    move_flat_pts=25,
    move_mild_max=0.15,
    move_mild_pts=12,
    move_drift_max=0.5,
    move_drift_pts=5,
    # DTE matters less for trend
    dte_safe_pts=5,
    dte_ok_pts=3,
    expiry_penalty=-20,
)


# ─── Regime confidence hook ─────────────────────────────────────────

REGIME_FAMILY = "directional_trend"


def compute_regime_confidence(detector: "RegimeDetector", underlying: str) -> float:
    """0.0-1.0 confidence for TDS entry."""
    return float(detector.regime_confidence_for_directional_trend(underlying))
