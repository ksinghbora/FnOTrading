"""Iron Condor calibration — params + scoring + regime confidence hook.

Edit ONLY this file to recalibrate iron_condor. No other strategy's
behaviour is affected by changes here.

Tunables exposed:

  - IronCondorParams (Pydantic class) — default params for IC
  - IRON_CONDOR_CONFIG (ScoreConfig)  — VIX/range/move/PCR/DTE scorer
  - REGIME_FAMILY                     — "premium_selling"
  - compute_regime_confidence(detector, underlying) -> float
                                      — IC's regime-fitness 0.0–1.0
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import Field

from src.strategy.params import BaseStrategyParams
from src.strategy.scoring import ScoreConfig

if TYPE_CHECKING:
    from src.strategy.regime import RegimeDetector


# ─── Pydantic params ────────────────────────────────────────────────


class IronCondorParams(BaseStrategyParams):
    """Parameters for Iron Condor strategy."""

    # IC ideal band on Indian VIX: 16-22 (16-20 ideal, 20-22 stressed but tradable, >25 no trade).
    # Defined risk via wings tolerates more VIX than naked strangle.
    vix_entry_min: float = 16.0              # Below: premium too thin — strangle wins
    vix_entry_max: float = 22.0              # Above: stressed beyond IC discipline
    vix_reduce_above: float = 20.0           # Halve lots in stressed band (20-22)
    short_call_delta: float = 0.15
    short_put_delta: float = -0.15
    wing_width_strikes: int = 8              # Distance between short and long strikes (8 = ~400pt wing)
    adjustment_threshold_pct: float = 60.0
    stop_loss_pct: float = 40.0
    profit_target_pct: float = 25.0

    # V5 margin estimate (₹L per lot) — IC with 8-strike wings on NIFTY
    # post-SEBI runs ~₹2.5L SPAN+ELM. Used by orchestrator V5 when
    # margin_aware_selection=True.
    expected_margin_per_lot_lakhs: float = 2.5

    # May 2 2026: Indian-market range-detection gate. ADX(14)<22 AND
    # BB-squeeze active AND RV/IV<0.80. Default False.
    require_premium_selling_regime: bool = False

    # Apr 30 2026 v2: orthogonal Choppiness Index + VRP gate.
    # CI ≥ 61.8 AND VRP > 0. Mutually exclusive with v1.
    require_premium_selling_regime_v2: bool = False

    # May 7 2026 — calendar-aware filter stack.
    require_calendar_filter: bool = False
    allowed_days_of_week: list[int] = Field(default_factory=lambda: [1, 2, 3])
    block_pre_event_days: int = 1
    block_friday: bool = False


# ─── Legacy 0-100 scoring config ────────────────────────────────────

IRON_CONDOR_CONFIG = ScoreConfig(
    name="iron_condor",
    # Indian VIX bands: 16-20 ideal, 20-22 stressed (wings still protect), >25 no trade
    vix_bands=[
        (16, 20, 25, "ideal(IC)"),
        (20, 22, 15, "stressed(IC-wings hold)"),
        (13, 16, 10, "thin premium(IC)"),
        (22, 25, 5, "high stress(IC)"),
        (25, 50, 0, "no_trade(event)"),
        (0, 13, 0, "complacency(IC)"),
    ],
)


# ─── Regime confidence hook ─────────────────────────────────────────

REGIME_FAMILY = "premium_selling"


def compute_regime_confidence(detector: "RegimeDetector", underlying: str) -> float:
    """0.0-1.0 confidence that conditions favour IC entry.

    Default: dispatch to the family-level confidence
    (regime_confidence_for_premium_selling). Override this function in
    place if IC needs strategy-specific weighting (different VIX peak,
    different DoW factor, etc.) — that change WILL NOT affect any
    other strategy's confidence math because each calibration module
    owns its own hook.
    """
    return float(detector.regime_confidence_for_premium_selling(underlying))
