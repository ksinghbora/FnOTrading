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
    """Parameters for Short Strangle strategy.

    Phase 1.5 risk-management ablation winner (May 7 2026):
    asymmetric exits with NO trail stop. The 173-day post-SEBI smoke
    of 5 PT/SL/trail variants found V1 (PT=25, SL=30, NO trail) wins
    at +₹43.8/trade, 69.4% WR, Sharpe +0.57 — well above the +₹8/trade
    deploy threshold. The binding constraint was the trail stop, NOT
    the SL/PT ratio: V3 (PT=25, SL=20, no trail) lost at -₹42/trade
    because SL=20 trips on routine 1-2σ noise that SL=30 absorbs.

    Reference variants from the ablation:
      Baseline (PT=15, SL=30, trail=15):    -₹52/trade, Sharpe -0.69
      V1 (PT=25, SL=30, no trail):          +₹44/trade, Sharpe +0.57 ✓ DEPLOYED
      V2 (PT=15, SL=20, trail=15):          -₹34/trade
      V3 (PT=25, SL=20, no trail):          -₹42/trade  ← SL=20 too tight
      V4 (PT=50, SL=30, no trail) tasty:    +₹42/trade, Sharpe +0.36
    """

    # Strangle ideal band on Indian VIX: 13-16 only
    vix_entry_min: float = 13.0
    vix_entry_max: float = 16.0
    call_delta: float = 0.15
    put_delta: float = -0.15
    adjustment_delta_threshold: float = 0.25

    # ─── Phase 1.5 V1 winner (May 7 2026) ─────────────────────────
    # PT=25, SL=30, NO trail. Asymmetric profit capture with wide
    # noise absorption. SL=30 keeps the strategy in trades through
    # routine intraday whips that a tighter SL=20 would clip.
    profit_target_pct: float = 25.0          # was 15.0; capture more theta per trip
    stop_loss_pct: float = 30.0              # unchanged; absorbs 1-2σ noise
    trail_stop_pct: float = 0.0              # was 15.0; trail was the binding loser
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
