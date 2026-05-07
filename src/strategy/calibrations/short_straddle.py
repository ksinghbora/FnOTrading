"""Short Straddle calibration — params + scoring + regime confidence hook.

Edit ONLY this file to recalibrate short_straddle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.strategy.params import BaseStrategyParams
from src.strategy.scoring import ScoreConfig

if TYPE_CHECKING:
    from src.strategy.regime import RegimeDetector


# ─── Pydantic params ────────────────────────────────────────────────


class ShortStraddleParams(BaseStrategyParams):
    """Parameters for Short Straddle strategy."""

    # ATM exposure — most gamma-fragile of all premium strategies
    vix_entry_min: float = 13.0              # Skip below complacency
    vix_entry_max: float = 15.0              # Tight upper bound — straddle blows up above
    adjustment_threshold_pct: float = 40.0
    stop_loss_pct: float = 30.0
    trail_stop_pct: float = 15.0
    profit_target_pct: float = 10.0          # Capture early theta — tighter than strangle
    add_hedge: bool = True
    hedge_offset_strikes: int = 6

    # V5: ATM short straddle (hedged) ~₹2L/lot SPAN+ELM
    expected_margin_per_lot_lakhs: float = 2.0


# ─── Legacy 0-100 scoring config ────────────────────────────────────

SHORT_STRADDLE_CONFIG = ScoreConfig(
    name="short_straddle",
    # Straddle is ATM — most gamma-fragile; tight Indian band 13-15 only
    vix_bands=[
        (13, 15, 25, "ideal(ATM)"),
        (15, 17, 12, "ok(ATM)"),
        (17, 50, 0, "dangerous(ATM)"),
        (0, 13, 0, "thin(ATM)"),
    ],
    # Straddle needs very tight range
    range_tight_max=0.2,
    range_tight_pts=30,
    range_moderate_max=0.4,
    range_moderate_pts=15,
    range_wide_max=0.6,
    range_wide_pts=5,
    # Very sensitive to directional moves
    move_flat_max=0.1,
    move_flat_pts=30,
    move_mild_max=0.2,
    move_mild_pts=15,
    move_drift_max=0.3,
    move_drift_pts=5,
)


# ─── Regime confidence hook ─────────────────────────────────────────

REGIME_FAMILY = "premium_selling"


def compute_regime_confidence(detector: "RegimeDetector", underlying: str) -> float:
    """0.0-1.0 confidence for SST entry.

    Dispatches to family-level. SST is the most VIX-sensitive of the
    premium-sellers (ATM gamma); if needed, override with stricter
    VIX caps.
    """
    return float(detector.regime_confidence_for_premium_selling(underlying))
