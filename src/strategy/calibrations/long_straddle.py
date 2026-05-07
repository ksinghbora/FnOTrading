"""Long Straddle calibration — params + regime confidence hook.

Edit ONLY this file to recalibrate long_straddle.
"""

from __future__ import annotations

from datetime import time
from typing import TYPE_CHECKING

from src.strategy.params import BaseStrategyParams

if TYPE_CHECKING:
    from src.strategy.regime import RegimeDetector


# ─── Pydantic params ────────────────────────────────────────────────


class LongStraddleParams(BaseStrategyParams):
    """Parameters for Long Straddle strategy — long-vol via long ATM CE+PE.

    Long straddle is the "anti-LC" for the same gate: where LC needs
    spot to stay near strike, LS needs spot to leave it. Same VRP<0
    gate (IV cheap, room to expand).
    """

    # VIX entry band — straddle wants moderate-to-high vol with room to expand
    vix_entry_min: float = 13.0
    vix_entry_max: float = 25.0
    vix_reduce_above: float = 22.0

    # Strike selection
    strike_offset_pct: float = 0.0           # 0 = ATM
    delta_target: float = 0.5                # ATM = ~0.5 delta

    use_weekly_expiry: bool = True

    # Risk management — long-debit, max loss = net debit paid
    profit_target_pct: float = 50.0
    stop_loss_pct: float = 50.0

    # Timing — must be `time` types (Pydantic does NOT auto-convert
    # str→time in subclass overrides).
    entry_time: time = time(9, 30)
    exit_time: time = time(15, 0)
    skip_entry_on_expiry_day: bool = True
    expiry_day_force_exit_at: time = time(14, 30)

    # Defaults — long-vol structure, default OFF for legacy filters
    pcr_filter_enabled: bool = False
    max_pain_filter_enabled: bool = False

    # V5: long straddle debit ≈ ₹0.4L/lot on NIFTY 24K with 1-lot=75
    expected_margin_per_lot_lakhs: float = 0.4

    # May 6 2026 — v2b regime gate (single-condition VRP < 0).
    # LS uses ONLY v2b (no CI requirement), since LS profits from
    # MOVEMENT, opposite of LC's "spot stays near strike" preference.
    require_long_vol_regime_v2b: bool = False


# ─── Regime confidence hook ─────────────────────────────────────────

REGIME_FAMILY = "long_vol"


def compute_regime_confidence(detector: "RegimeDetector", underlying: str) -> float:
    """0.0-1.0 confidence for LS entry.

    Dispatches to long-vol family. Note: family-level long_vol penalises
    extreme CI (both very-high AND very-low) because vol mean-reverts
    cleanly in mid-range. LS could plausibly want a different shape
    (LS wants MOVEMENT, so high-CI / range-bound is BAD). If/when
    smoke confirms, override here without affecting LC's confidence.
    """
    return float(detector.regime_confidence_for_long_vol(underlying))
