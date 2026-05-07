"""Short Strangle calibration — params + scoring + regime confidence hook.

Edit ONLY this file to recalibrate short_strangle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import Field

from src.strategy.params import BaseStrategyParams
from src.strategy.scoring import ScoreConfig

if TYPE_CHECKING:
    from src.strategy.regime import RegimeDetector


# ─── Pydantic params ────────────────────────────────────────────────


class ShortStrangleParams(BaseStrategyParams):
    """Parameters for Short Strangle strategy."""

    # Strangle ideal band on Indian VIX: 13-16 only
    vix_entry_min: float = 13.0
    vix_entry_max: float = 16.0
    call_delta: float = 0.15
    put_delta: float = -0.15
    adjustment_delta_threshold: float = 0.25
    stop_loss_pct: float = 30.0
    trail_stop_pct: float = 15.0
    profit_target_pct: float = 15.0
    add_hedge: bool = True
    hedge_offset_strikes: int = 5

    # V5: hedged short strangle on NIFTY ~₹1.5L SPAN+ELM per lot.
    expected_margin_per_lot_lakhs: float = 1.5

    # May 7 2026 (Phase 1) — Indian-validated regime + calendar gates.
    require_premium_selling_regime_v2: bool = False
    require_calendar_filter: bool = False
    # 0=Mon 1=Tue 2=Wed 3=Thu 4=Fri. Default Tue/Wed/Thu matches Anurag
    # Goel's NIFTY short-strangle Sharpe-1.96 research.
    allowed_days_of_week: list[int] = Field(default_factory=lambda: [1, 2, 3])
    block_pre_event_days: int = 1
    block_friday: bool = False


# ─── Legacy 0-100 scoring config ────────────────────────────────────

SHORT_STRANGLE_CONFIG = ScoreConfig(
    name="short_strangle",
    # Indian VIX bands: 13-16 ideal, 16-18 marginal, >18 naked premium too dangerous
    vix_bands=[
        (13, 16, 25, "ideal"),
        (16, 18, 8, "marginal(strangle)"),
        (18, 50, 0, "too_high(naked)"),
        (0, 13, 0, "complacency(thin)"),
    ],
)


# ─── Regime confidence hook ─────────────────────────────────────────

REGIME_FAMILY = "premium_selling"


def compute_regime_confidence(detector: "RegimeDetector", underlying: str) -> float:
    """0.0-1.0 confidence that conditions favour SS entry.

    Default: dispatch to family-level. Strangle is naked-short premium
    so it's MORE sensitive to VIX excursions than IC; if/when this
    matters, override here with a tighter VIX peak (e.g. only 13-16
    counts as fully favourable).
    """
    return float(detector.regime_confidence_for_premium_selling(underlying))
