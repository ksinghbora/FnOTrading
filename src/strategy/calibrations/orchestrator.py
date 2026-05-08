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

    # ─── V6 (May 8 2026) multi-slot orchestration ─────────────────
    # Default 1 = V4/V5 single-slot behaviour (backward compatible).
    # Set to 2+ to allow multiple children to hold positions
    # concurrently. With max_concurrent_slots=4 (the canonical roster
    # size), the orchestrator can run IC + IB + SS + TD in parallel
    # when their gates pass on the same day. Each child manages its
    # own exits independently; the orchestrator routes ticks to ALL
    # active children plus tries to enter new ones up to the slot cap.
    max_concurrent_slots: int = 1

    # Total margin budget across active children, in lakhs of rupees.
    # When > 0 and ``margin_aware_selection=True``, new entries are
    # blocked once sum(active.expected_margin_per_lot_lakhs) ≥ this
    # cap. 0 disables the budget gate (no cap).
    max_total_margin_lakhs: float = 0.0

    # ─── V6.1 (May 8 2026) AI advisor bias integration ────────────
    # When True, orchestrator reads the morning DayBias file at first
    # tick of each day and adjusts each child's legacy_score by the
    # family-appropriate adj before ranking:
    #   premium_selling family → bias.premium_score_adj  (±15)
    #   directional_trend family → bias.trend_score_adj   (±15)
    # Adjustment only applies when bias.<family>_confidence ≥
    # ``advisor_bias_min_confidence``. Default 0.5 mirrors the
    # confluence engine's gate.
    # Default False preserves V6_RAW behaviour exactly. When enabled
    # in backtest, set BACKFILL_DAY_BIAS_DIR env var to point at the
    # synthetic-backfill directory; in live, the advisor cron writes
    # to data/day_bias.json which load_day_bias() reads automatically.
    use_advisor_bias: bool = False
    advisor_bias_min_confidence: float = 0.5
