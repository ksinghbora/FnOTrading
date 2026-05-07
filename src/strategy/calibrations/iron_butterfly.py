"""Iron Butterfly calibration — params + regime confidence hook.

IB inherits structure from IC (the user-confirmed inheritance contract).
The IB params class extends IronCondorParams with ATM-body overrides.
The regime confidence hook ALSO defaults to IC's, but can be overridden
here for IB-specific weighting without touching any other strategy.

Edit ONLY this file to recalibrate iron_butterfly. The single dependency
on `iron_condor.py` is documented and intentional — IB *is* IC with
ATM body and tighter wings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Allowed cross-module dependency (IB inherits from IC by design):
from src.strategy.calibrations.iron_condor import (
    IronCondorParams,
    compute_regime_confidence as _ic_compute_regime_confidence,
)

if TYPE_CHECKING:
    from src.strategy.regime import RegimeDetector


# ─── Pydantic params ────────────────────────────────────────────────


class IronButterflyParams(IronCondorParams):
    """Parameters for Iron Butterfly strategy — IC with ATM body.

    May 7 2026 — Iron Butterfly is the post-SEBI capital-efficient
    cousin of IC. Same defined-risk 4-leg structure (sell short body,
    buy wings) but the short legs are sold AT-THE-MONEY (delta ~0.5)
    instead of OTM. Larger credit, tighter break-even zone, higher
    gamma/vega exposure.

    Why IB beats IC on capital efficiency post-SEBI:
      - Same SPAN-margin defined-risk treatment
      - ATM body = ~3× the credit of 0.15-Δ wings on same wing-width
      - Tighter wings (default 2 strikes vs IC's 8) → margin drops ~60%
        per OptionX/Bajaj Broking analysis (~₹2.5L → ₹1.5L per lot)
      - Avoids the 2% ELM hit on naked-short-straddle expiry day

    Same regime gate (require_premium_selling_regime_v2) and same
    calendar-aware filter inherited from IronCondorParams.
    """

    # ATM short body (delta ~0.5)
    short_call_delta: float = 0.5
    short_put_delta: float = -0.5

    # Tighter default wings — 2 strikes vs IC's 8
    wing_width_strikes: int = 2

    # Tighter SL — ATM gamma exposure is much higher than IC
    stop_loss_pct: float = 30.0

    # V5: tighter wings cut margin to ~₹1.5L/lot
    expected_margin_per_lot_lakhs: float = 1.5


# ─── Regime confidence hook ─────────────────────────────────────────

REGIME_FAMILY = "premium_selling"


def compute_regime_confidence(detector: "RegimeDetector", underlying: str) -> float:
    """0.0-1.0 confidence for IB entry.

    Defaults to IC's hook (premium-selling family). Override here if
    IB-specific weights are needed — e.g., IB has higher gamma exposure
    so might want stricter VIX-band scoring than IC. Doing so leaves
    IC's confidence math unchanged.
    """
    return _ic_compute_regime_confidence(detector, underlying)
