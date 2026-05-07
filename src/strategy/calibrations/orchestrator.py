"""Orchestrator calibration — params only.

Edit ONLY this file to recalibrate orchestrator. The orchestrator does
NOT belong to a regime family; it ROUTES across families. So no
``compute_regime_confidence`` hook is exported here — orchestrator
falls back to the default 0.5 neutral if it's ever nested inside
another orchestrator (rare).
"""

from __future__ import annotations

from pydantic import Field

from src.strategy.params import BaseStrategyParams


REGIME_FAMILY = "unknown"


class OrchestratorParams(BaseStrategyParams):
    """Parameters for the OrchestratorStrategy meta-controller.

    Phase 3c (Apr 27 2026): registry-discovered, fully-decoupled meta
    strategy. Adding a strategy = put its registry name in ``children``.

    V5 (May 7 2026) coordinator extensions: regime-aware scoring, cash
    floor, margin-aware ranking, correlation guard, drawdown breaker.
    All default-OFF for V4 backward compatibility.
    """

    # Names from the registry. Strategy must register itself via
    # @register_strategy(name, ParamsCls) to be discoverable here.
    children: list[str] = Field(default_factory=lambda: [
        "iron_condor",
        "iron_butterfly",
        "short_strangle",
        "short_straddle",
        "long_calendar",
    ])

    # Per-child param overrides. Key = child registry name, value = dict
    # of param overrides for that child's params class.
    children_params: dict = Field(default_factory=dict)

    # Selection thresholds
    min_score_to_trade: int = 60
    score_margin_to_switch: int = 10

    # Lifecycle
    propagate_exits_to_children: bool = True

    # ─── V5 (May 7 2026) coordinator extensions ─────────────────────
    # All default-OFF: setting any to default reproduces V4 behaviour.

    # 1. Regime-aware scoring: effective = legacy × regime_confidence
    regime_aware_scoring: bool = False

    # 2. Cash floor: skip when no family clears confidence
    cash_floor_confidence: float = 0.0

    # 3. Margin-aware ranking: rank by score / margin_lakhs
    margin_aware_selection: bool = False

    # 4. Correlation guard: block same-family stacking
    block_correlated_families: bool = False

    # 5. Daily-PnL circuit breaker: stop new entries below floor
    daily_max_drawdown_inr: float = 0.0

    # 6. Per-strategy weight cap (multi-slot future)
    max_strategy_weight: float = 1.0
