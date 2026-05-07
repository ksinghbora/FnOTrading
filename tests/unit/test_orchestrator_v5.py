"""Tests for OrchestratorStrategy V5 coordinator extensions.

V5 (May 7 2026) adds five independently-flagged coordinator behaviours
on top of the V4 contract:
  1. regime_aware_scoring   — multiply legacy score by regime confidence
  2. cash_floor_confidence  — refuse to allocate when no family clears the floor
  3. margin_aware_selection — rank by score-per-lakh-of-margin
  4. block_correlated_families — exclude same-family children from re-entry
  5. daily_max_drawdown_inr — block new entries after day-PnL crosses floor

These tests cover each knob in isolation. The pure ranking math also
gets direct unit tests via synthetic ChildCandidate objects (no need
to spin up real strategies). V4-contract behaviour is covered in
tests/unit/test_orchestrator.py and is NOT duplicated here.
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest


def _ensure_imported():
    from src.backtest.common import import_strategies
    import_strategies()


def _make_minimal_ctx(today: date = date(2025, 9, 9)):
    """Mock context — see test_orchestrator.py for the canonical shape."""
    ctx = MagicMock()
    ctx._feed = MagicMock()
    ctx._aggregator = MagicMock()
    ctx._chain_builder = MagicMock()
    ctx.clock = MagicMock()
    ctx.clock.now = MagicMock(return_value=datetime(today.year, today.month, today.day, 9, 30))
    ctx.next_expiry = MagicMock(return_value=date(2025, 9, 16))
    ctx.get_available_expiries = MagicMock(return_value=[
        date(2025, 9, 16), date(2025, 9, 23), date(2025, 9, 30), date(2025, 10, 7),
    ])
    ctx.get_vix = MagicMock(return_value=18.0)
    return ctx


# ─── Pure coordinator math ─────────────────────────────────────────


def test_child_candidate_effective_score_v4_path():
    """With regime_aware=False, effective_score = legacy_score (V4 behaviour)."""
    from src.strategy.coordinator import ChildCandidate
    c = ChildCandidate(
        name="ic", legacy_score=70, regime_confidence=0.30,
        regime_family="premium_selling", expected_margin_lakhs=2.5, list_position=0,
    )
    assert c.effective_score(regime_aware=False) == 70.0
    assert c.effective_score(regime_aware=True) == pytest.approx(21.0)


def test_child_candidate_margin_yield():
    """margin_yield = effective_score / margin_lakhs."""
    from src.strategy.coordinator import ChildCandidate
    c = ChildCandidate(
        name="ib", legacy_score=60, regime_confidence=1.0,
        regime_family="premium_selling", expected_margin_lakhs=1.5, list_position=0,
    )
    assert c.margin_yield(regime_aware=False) == pytest.approx(40.0)


def test_child_candidate_margin_yield_falls_back_when_margin_zero():
    """Defensive: margin <= 0 → fall back to effective_score (no div by zero)."""
    from src.strategy.coordinator import ChildCandidate
    c = ChildCandidate(
        name="x", legacy_score=70, regime_confidence=1.0,
        regime_family="unknown", expected_margin_lakhs=0.0, list_position=0,
    )
    assert c.margin_yield(regime_aware=False) == 70.0


def test_rank_candidates_by_effective_score():
    """Default ranking is by effective score, descending, stable tie-break."""
    from src.strategy.coordinator import ChildCandidate, rank_candidates
    cands = [
        ChildCandidate("a", 75, 1.0, "premium_selling", 2.5, 0),
        ChildCandidate("b", 65, 1.0, "premium_selling", 1.5, 1),
        ChildCandidate("c", 50, 1.0, "premium_selling", 1.0, 2),  # below threshold
    ]
    ranked = rank_candidates(
        cands, min_score_to_trade=60, regime_aware=False,
        margin_aware=False, block_correlated=False,
    )
    assert [c.name for c, _ in ranked] == ["a", "b"]


def test_rank_candidates_margin_aware_promotes_efficient_strategy():
    """When margin_aware=True, IB at score 65 / ₹1.5L beats IC at score 75 / ₹2.5L."""
    from src.strategy.coordinator import ChildCandidate, rank_candidates
    cands = [
        ChildCandidate("ic", 75, 1.0, "premium_selling", 2.5, 0),  # yield 30
        ChildCandidate("ib", 65, 1.0, "premium_selling", 1.5, 1),  # yield 43.3
    ]
    ranked = rank_candidates(
        cands, min_score_to_trade=60, regime_aware=False,
        margin_aware=True, block_correlated=False,
    )
    assert [c.name for c, _ in ranked] == ["ib", "ic"]


def test_rank_candidates_regime_aware_filters_low_confidence():
    """With regime_aware=True, a high legacy score × low confidence is filtered."""
    from src.strategy.coordinator import ChildCandidate, rank_candidates
    cands = [
        ChildCandidate("ic", 80, 0.30, "premium_selling", 2.5, 0),  # eff=24
        ChildCandidate("ib", 70, 0.95, "premium_selling", 1.5, 1),  # eff=66.5
    ]
    ranked = rank_candidates(
        cands, min_score_to_trade=60, regime_aware=True,
        margin_aware=False, block_correlated=False,
    )
    # ic effective=24 < 60 (filtered); ib effective=66.5 (passes)
    assert [c.name for c, _ in ranked] == ["ib"]


def test_rank_candidates_correlation_guard_excludes_family():
    from src.strategy.coordinator import ChildCandidate, rank_candidates
    cands = [
        ChildCandidate("ic", 80, 1.0, "premium_selling", 2.5, 0),
        ChildCandidate("trend", 70, 1.0, "directional_trend", 1.0, 1),
    ]
    ranked = rank_candidates(
        cands, min_score_to_trade=60, regime_aware=False,
        margin_aware=False, block_correlated=True,
        excluded_families={"premium_selling"},
    )
    assert [c.name for c, _ in ranked] == ["trend"]


def test_cash_floor_breached_blocks_when_all_below():
    from src.strategy.coordinator import ChildCandidate, cash_floor_breached
    cands = [
        ChildCandidate("ic", 80, 0.30, "premium_selling", 2.5, 0),
        ChildCandidate("lc", 70, 0.20, "long_vol", 0.6, 1),
        ChildCandidate("td", 65, 0.10, "directional_trend", 1.0, 2),
    ]
    assert cash_floor_breached(cands, floor_confidence=0.50) is True


def test_cash_floor_passes_when_one_family_clears():
    from src.strategy.coordinator import ChildCandidate, cash_floor_breached
    cands = [
        ChildCandidate("ic", 80, 0.30, "premium_selling", 2.5, 0),
        ChildCandidate("td", 60, 0.65, "directional_trend", 1.0, 1),  # clears
    ]
    assert cash_floor_breached(cands, floor_confidence=0.50) is False


def test_cash_floor_zero_threshold_disables():
    from src.strategy.coordinator import ChildCandidate, cash_floor_breached
    cands = [ChildCandidate("ic", 80, 0.0, "premium_selling", 2.5, 0)]
    assert cash_floor_breached(cands, floor_confidence=0.0) is False


def test_record_pnl_change_trips_breaker():
    from src.strategy.coordinator import CoordinatorState, record_pnl_change
    state = CoordinatorState()
    # Three losses pile up; third trips the breaker
    assert record_pnl_change(state, -5000.0, -10000.0) is False
    assert state.day_pnl_inr == -5000.0
    assert record_pnl_change(state, -4000.0, -10000.0) is False
    assert record_pnl_change(state, -2000.0, -10000.0) is True   # tripped here
    assert state.drawdown_tripped is True
    # Subsequent updates don't retrip
    assert record_pnl_change(state, -1000.0, -10000.0) is False


def test_record_pnl_change_disabled_at_zero_threshold():
    from src.strategy.coordinator import CoordinatorState, record_pnl_change
    state = CoordinatorState()
    record_pnl_change(state, -100000.0, 0.0)
    assert state.drawdown_tripped is False


def test_should_block_entries_returns_reason():
    from src.strategy.coordinator import CoordinatorState, should_block_entries, record_pnl_change
    state = CoordinatorState()
    record_pnl_change(state, -20000.0, -10000.0)
    blocked, reason = should_block_entries(state, -10000.0)
    assert blocked is True
    assert "DAILY_DRAWDOWN_TRIPPED" in reason


def test_reset_day_clears_state_idempotently():
    from src.strategy.coordinator import CoordinatorState, reset_day
    state = CoordinatorState(day_pnl_inr=-5000.0, drawdown_tripped=True, last_day=date(2025, 9, 8))
    assert reset_day(state, date(2025, 9, 9)) is True
    assert state.day_pnl_inr == 0.0
    assert state.drawdown_tripped is False
    # Idempotent — second call same day returns False
    assert reset_day(state, date(2025, 9, 9)) is False


def test_best_family_confidence_picks_highest():
    from src.strategy.coordinator import ChildCandidate, best_family_confidence
    cands = [
        ChildCandidate("ic", 80, 0.55, "premium_selling", 2.5, 0),
        ChildCandidate("td", 60, 0.72, "directional_trend", 1.0, 1),
    ]
    family, conf = best_family_confidence(cands)
    assert family == "directional_trend"
    assert conf == pytest.approx(0.72)


def test_best_family_confidence_takes_max_within_family():
    """When two children share a family, the family's confidence is the max
    of the children's individual confidences."""
    from src.strategy.coordinator import ChildCandidate, best_family_confidence
    cands = [
        ChildCandidate("ic", 80, 0.40, "premium_selling", 2.5, 0),
        ChildCandidate("ib", 70, 0.55, "premium_selling", 1.5, 1),
        ChildCandidate("td", 60, 0.30, "directional_trend", 1.0, 2),
    ]
    family, conf = best_family_confidence(cands)
    assert family == "premium_selling"
    assert conf == pytest.approx(0.55)


# ─── End-to-end orchestrator V5 ────────────────────────────────────


@pytest.mark.asyncio
async def test_v5_default_flags_match_v4_behaviour():
    """V5 with all flags at default = V4 router."""
    _ensure_imported()
    from src.core.models import Signal
    from src.core.types import SignalType
    from src.strategy.registry import create_strategy

    o = create_strategy(
        "orchestrator",
        strategy_id="orch_v5_default",
        params={"children": ["iron_condor", "short_strangle"]},
    )
    o.set_context(_make_minimal_ctx())
    await o.on_start()

    # Default flags: every V5 knob is OFF
    assert o.params.regime_aware_scoring is False
    assert o.params.cash_floor_confidence == 0.0
    assert o.params.margin_aware_selection is False
    assert o.params.block_correlated_families is False
    assert o.params.daily_max_drawdown_inr == 0.0

    o._children["iron_condor"].evaluate_score = lambda: 75
    o._children["short_strangle"].evaluate_score = lambda: 65
    o._children["iron_condor"].evaluate_regime_confidence = lambda: 0.20  # Should be IGNORED
    o._children["short_strangle"].evaluate_regime_confidence = lambda: 0.95
    sig = Signal(strategy_id="ic_dummy", signal_type=SignalType.ENTRY, legs=[], reason="x")
    o._children["iron_condor"].on_tick = AsyncMock(return_value=sig)
    o._children["short_strangle"].on_tick = AsyncMock(return_value=None)

    result = await o.on_tick(MagicMock())
    # IC won despite low confidence — default flags = V4 = legacy score only
    assert result is sig
    assert o._active_child == "iron_condor"


@pytest.mark.asyncio
async def test_v5_regime_aware_demotes_low_confidence_high_score_child():
    _ensure_imported()
    from src.core.models import Signal
    from src.core.types import SignalType
    from src.strategy.registry import create_strategy

    o = create_strategy(
        "orchestrator",
        strategy_id="orch_v5_regime",
        params={
            "children": ["iron_condor", "short_strangle"],
            "regime_aware_scoring": True,
        },
    )
    o.set_context(_make_minimal_ctx())
    await o.on_start()

    # IC: legacy 80 × confidence 0.20 = 16 (filtered, below 60)
    # Strangle: legacy 70 × confidence 0.95 = 66.5 (passes)
    o._children["iron_condor"].evaluate_score = lambda: 80
    o._children["short_strangle"].evaluate_score = lambda: 70
    o._children["iron_condor"].evaluate_regime_confidence = lambda: 0.20
    o._children["short_strangle"].evaluate_regime_confidence = lambda: 0.95
    sig = Signal(strategy_id="sg_dummy", signal_type=SignalType.ENTRY, legs=[], reason="r")
    o._children["iron_condor"].on_tick = AsyncMock(return_value=None)  # Should not be called
    o._children["short_strangle"].on_tick = AsyncMock(return_value=sig)

    result = await o.on_tick(MagicMock())
    assert result is sig
    o._children["iron_condor"].on_tick.assert_not_called()
    o._children["short_strangle"].on_tick.assert_awaited_once()


@pytest.mark.asyncio
async def test_v5_cash_floor_blocks_when_no_family_clears():
    _ensure_imported()
    from src.strategy.registry import create_strategy

    o = create_strategy(
        "orchestrator",
        strategy_id="orch_v5_cashfloor",
        params={
            "children": ["iron_condor", "short_strangle"],
            "regime_aware_scoring": True,
            "cash_floor_confidence": 0.50,
            # Set min_score_to_trade=0 so the cash floor is the binding gate
            "min_score_to_trade": 0,
        },
    )
    o.set_context(_make_minimal_ctx())
    await o.on_start()

    # Both legacy scores high, both regime confidences below 0.50 floor
    o._children["iron_condor"].evaluate_score = lambda: 85
    o._children["short_strangle"].evaluate_score = lambda: 75
    o._children["iron_condor"].evaluate_regime_confidence = lambda: 0.30
    o._children["short_strangle"].evaluate_regime_confidence = lambda: 0.40
    o._children["iron_condor"].on_tick = AsyncMock(return_value=None)
    o._children["short_strangle"].on_tick = AsyncMock(return_value=None)

    result = await o.on_tick(MagicMock())
    assert result is None
    o._children["iron_condor"].on_tick.assert_not_called()
    o._children["short_strangle"].on_tick.assert_not_called()


@pytest.mark.asyncio
async def test_v5_margin_aware_picks_higher_yield_strategy():
    _ensure_imported()
    from src.core.models import Signal
    from src.core.types import SignalType
    from src.strategy.registry import create_strategy

    o = create_strategy(
        "orchestrator",
        strategy_id="orch_v5_margin",
        params={
            "children": ["iron_condor", "iron_butterfly"],
            "margin_aware_selection": True,
        },
    )
    o.set_context(_make_minimal_ctx())
    await o.on_start()

    # IC defaults to ₹2.5L margin; IB defaults to ₹1.5L
    # IC at score 75 → yield 30
    # IB at score 70 → yield 46.7 (wins on margin yield)
    o._children["iron_condor"].evaluate_score = lambda: 75
    o._children["iron_butterfly"].evaluate_score = lambda: 70
    sig = Signal(strategy_id="ib_dummy", signal_type=SignalType.ENTRY, legs=[], reason="margin")
    o._children["iron_condor"].on_tick = AsyncMock(return_value=None)
    o._children["iron_butterfly"].on_tick = AsyncMock(return_value=sig)

    result = await o.on_tick(MagicMock())
    assert result is sig
    assert o._active_child == "iron_butterfly"


@pytest.mark.asyncio
async def test_v5_drawdown_breaker_blocks_after_threshold():
    _ensure_imported()
    from src.strategy.registry import create_strategy

    o = create_strategy(
        "orchestrator",
        strategy_id="orch_v5_dd",
        params={
            "children": ["iron_condor"],
            "daily_max_drawdown_inr": -10000.0,
        },
    )
    o.set_context(_make_minimal_ctx())
    await o.on_start()

    # Manually trip the breaker (simulating 3 losses already booked today).
    # Set last_day=today FIRST so on_tick's reset_day call is a no-op (we
    # don't want today's day-rollover detection to clear the breaker).
    from src.strategy.coordinator import record_pnl_change
    o._coord.last_day = date(2025, 9, 9)
    record_pnl_change(o._coord, -15000.0, o.params.daily_max_drawdown_inr)
    assert o._coord.drawdown_tripped is True

    # Even with a great score, no entry
    o._children["iron_condor"].evaluate_score = lambda: 95
    o._children["iron_condor"].on_tick = AsyncMock(return_value=None)

    result = await o.on_tick(MagicMock())
    assert result is None
    o._children["iron_condor"].on_tick.assert_not_called()


@pytest.mark.asyncio
async def test_v5_drawdown_breaker_resets_on_day_rollover():
    _ensure_imported()
    from src.core.models import Signal
    from src.core.types import SignalType
    from src.strategy.registry import create_strategy

    o = create_strategy(
        "orchestrator",
        strategy_id="orch_v5_dd_reset",
        params={
            "children": ["iron_condor"],
            "daily_max_drawdown_inr": -10000.0,
        },
    )
    ctx = _make_minimal_ctx(today=date(2025, 9, 8))
    o.set_context(ctx)
    await o.on_start()

    # Trip breaker on day 1
    from src.strategy.coordinator import record_pnl_change
    record_pnl_change(o._coord, -15000.0, o.params.daily_max_drawdown_inr)
    o._coord.last_day = date(2025, 9, 8)  # mark as set so reset_day actually fires
    assert o._coord.drawdown_tripped is True

    # Roll clock forward → on_tick triggers reset_day
    ctx.clock.now = MagicMock(return_value=datetime(2025, 9, 9, 9, 30))
    o._children["iron_condor"].evaluate_score = lambda: 75
    sig = Signal(strategy_id="ic_dummy", signal_type=SignalType.ENTRY, legs=[], reason="post-rollover")
    o._children["iron_condor"].on_tick = AsyncMock(return_value=sig)

    result = await o.on_tick(MagicMock())
    assert result is sig
    assert o._coord.drawdown_tripped is False
    assert o._coord.day_pnl_inr == 0.0


@pytest.mark.asyncio
async def test_v5_drawdown_breaker_disabled_at_zero():
    """daily_max_drawdown_inr=0.0 (default) → breaker is fully disabled."""
    _ensure_imported()
    from src.core.models import Signal
    from src.core.types import SignalType
    from src.strategy.registry import create_strategy

    o = create_strategy(
        "orchestrator",
        strategy_id="orch_v5_dd_off",
        params={"children": ["iron_condor"]},
    )
    o.set_context(_make_minimal_ctx())
    await o.on_start()

    # Force a "drawdown" — should have no effect because threshold is disabled
    from src.strategy.coordinator import record_pnl_change
    record_pnl_change(o._coord, -50000.0, o.params.daily_max_drawdown_inr)
    assert o._coord.drawdown_tripped is False

    o._children["iron_condor"].evaluate_score = lambda: 75
    sig = Signal(strategy_id="ic_dummy", signal_type=SignalType.ENTRY, legs=[], reason="x")
    o._children["iron_condor"].on_tick = AsyncMock(return_value=sig)

    result = await o.on_tick(MagicMock())
    assert result is sig


# ─── Strategy regime_family declarations ───────────────────────────


def test_each_strategy_declares_regime_family():
    """Every canonical-roster strategy declares a non-unknown family.

    This locks in the V5 contract — adding a new strategy without
    declaring its family should fail this test loud, prompting the
    author to assign one.
    """
    _ensure_imported()
    from src.strategy.registry import create_strategy
    expected = {
        "iron_condor": "premium_selling",
        "iron_butterfly": "premium_selling",
        "short_strangle": "premium_selling",
        "short_straddle": "premium_selling",
        "long_calendar": "long_vol",
        "long_straddle": "long_vol",
        "trend_daily": "directional_trend",
        "trend_itm": "directional_trend",
        "trend_debit_spread": "directional_trend",
    }
    for name, family in expected.items():
        s = create_strategy(name, strategy_id=f"{name}_test")
        assert s.regime_family == family, (
            f"strategy '{name}' has regime_family='{s.regime_family}', expected '{family}'"
        )


def test_each_strategy_declares_margin_estimate():
    """Every canonical-roster strategy has a non-default margin estimate.

    Default 2.0 is the BaseStrategyParams sentinel — strategies that
    forget to override it would all rank identically under
    margin_aware_selection. This test ensures the override actually
    happened on every strategy.
    """
    _ensure_imported()
    from src.strategy.registry import create_strategy
    # All canonical strategies should have set their own margin estimate;
    # any leftover at exactly 2.0 fails this test as a reminder.
    overridden = [
        "iron_condor", "iron_butterfly", "short_strangle", "short_straddle",
        "long_calendar", "long_straddle", "trend_daily", "trend_itm",
        "trend_debit_spread",
    ]
    for name in overridden:
        s = create_strategy(name, strategy_id=f"{name}_margin_test")
        margin = s.params.expected_margin_per_lot_lakhs
        assert margin > 0, f"{name} has non-positive margin {margin}"
        # Each canonical strategy should override (not stay at the default 2.0)
        # short_straddle is the one that genuinely needs ~2.0 — exempt only it
        if name != "short_straddle":
            assert margin != 2.0, (
                f"{name} margin is exactly 2.0 (the BaseStrategyParams default). "
                f"Override expected_margin_per_lot_lakhs in the params class."
            )
