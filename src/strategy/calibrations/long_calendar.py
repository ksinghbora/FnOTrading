"""Long Calendar calibration — params + regime confidence hook.

Edit ONLY this file to recalibrate long_calendar.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.strategy.params import BaseStrategyParams

if TYPE_CHECKING:
    from src.strategy.regime import RegimeDetector


# ─── Pydantic params ────────────────────────────────────────────────


class LongCalendarParams(BaseStrategyParams):
    """Parameters for Long Calendar strategy — long vega, theta differential.

    Phase 3b candidate (Apr 27): the only positive-vega strategy in the
    roster. Sells front-week ATM option, buys back-week (or back-month)
    same-strike option.
    """

    # VIX entry band — calendar wants moderate vol with room to expand.
    vix_entry_min: float = 14.0
    vix_entry_max: float = 25.0
    vix_reduce_above: float = 22.0

    # Calendar structure
    leg_type: str = "CE"                     # "CE" | "PE" | "BOTH"
    strike_offset_pct: float = 0.0           # 0 = ATM

    # Back-expiry selection. v3: back must be at least min_back_days
    # after front (typically picks the next monthly, ~21-35 days).
    min_back_days: int = 21

    # Risk management
    profit_target_pct: float = 30.0
    stop_loss_pct: float = 50.0
    max_underlying_move_pct: float = 1.5     # Hard stop if spot moves >X% from strike

    # Timing
    front_close_buffer_minutes: int = 90     # Close N min before front-week expiry
    pcr_filter_enabled: bool = False
    max_pain_filter_enabled: bool = False

    # V5: long calendar is a debit spread — typical NIFTY ATM ~₹0.6L/lot
    expected_margin_per_lot_lakhs: float = 0.6

    # May 6 2026 — long-vol regime gate.
    # CI ≥ 61.8 AND VRP < 0 (range AND IV cheap).
    require_long_vol_regime_v2: bool = False

    # May 6 2026 — LC v2b: drop CI condition, pure VRP < 0.
    require_long_vol_regime_v2b: bool = False


# ─── Regime confidence hook ─────────────────────────────────────────

REGIME_FAMILY = "long_vol"


def compute_regime_confidence(detector: "RegimeDetector", underlying: str) -> float:
    """0.0-1.0 confidence for LC entry.

    Dispatches to long-vol family. LC differs from LS in that LC wants
    spot to STAY near strike (range-bound preference). The family-level
    long-vol confidence already factors CI mid-range; override here if
    LC needs stricter range preference than LS.
    """
    return float(detector.regime_confidence_for_long_vol(underlying))
