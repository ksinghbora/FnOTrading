"""Tests for OrchestratorStrategy decoupling guarantees.

Phase 3c (Apr 27 2026). The orchestrator MUST:
  1. Discover children via the registry, NOT hardcoded imports
  2. Skip unknown child names without crashing
  3. Refuse to nest itself
  4. Apply per-child param overrides without leaking params across children
  5. Pick the highest-scoring child each tick when no slot is held
  6. Route ALL ticks to the active child once it's holding a position

These tests assert the structural guarantees that prevent strategy
addition/removal from regressing the orchestrator.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time
from unittest.mock import AsyncMock, MagicMock

import pytest


def _ensure_imported():
    from src.backtest.common import import_strategies
    import_strategies()


# ── Registration ──────────────────────────────────────────────────


def test_orchestrator_registers_in_registry():
    _ensure_imported()
    from src.strategy.registry import list_strategies
    assert "orchestrator" in list_strategies()


def test_orchestrator_constructs_with_defaults():
    _ensure_imported()
    from src.strategy.registry import create_strategy
    o = create_strategy("orchestrator", strategy_id="orch")
    # 5 children listed; not yet instantiated until on_start
    assert o.params.children == [
        "iron_condor",
        "iron_butterfly",
        "short_strangle",
        "short_straddle",
        "long_calendar",
    ]
    assert o.params.min_score_to_trade == 60
    assert o._children == {}
    assert o._active_child is None


# ── Decoupling: unknown name is skipped, not crashed ────────────────


@pytest.mark.asyncio
async def test_orchestrator_skips_unknown_child_name(caplog):
    _ensure_imported()
    from src.strategy.registry import create_strategy

    o = create_strategy(
        "orchestrator",
        strategy_id="orch_unknown",
        params={"children": ["iron_condor", "nonexistent_strategy_xyz"]},
    )
    # Mock context — we just need it to exist, not function
    o.set_context(_make_minimal_ctx())
    await o.on_start()

    # Real child instantiated; unknown one skipped
    assert "iron_condor" in o._children
    assert "nonexistent_strategy_xyz" not in o._children
    assert "not in registry" in caplog.text


@pytest.mark.asyncio
async def test_orchestrator_refuses_to_nest_itself(caplog):
    _ensure_imported()
    from src.strategy.registry import create_strategy

    o = create_strategy(
        "orchestrator",
        strategy_id="orch_nested",
        params={"children": ["orchestrator", "iron_condor"]},
    )
    o.set_context(_make_minimal_ctx())
    await o.on_start()

    assert "orchestrator" not in o._children
    assert "iron_condor" in o._children
    assert "refusing to nest" in caplog.text.lower()


# ── Per-child param isolation ──────────────────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_per_child_params_isolated():
    _ensure_imported()
    from src.strategy.registry import create_strategy

    # Override iron_condor's wing_width but NOT iron_butterfly's
    overrides = {
        "iron_condor": {"wing_width_strikes": 12, "vix_entry_min": 18.0},
    }
    o = create_strategy(
        "orchestrator",
        strategy_id="orch_overrides",
        params={
            "children": ["iron_condor", "iron_butterfly"],
            "children_params": overrides,
        },
    )
    o.set_context(_make_minimal_ctx())
    await o.on_start()

    ic = o._children["iron_condor"]
    ib = o._children["iron_butterfly"]
    # IC got the override
    assert ic.params.wing_width_strikes == 12
    assert ic.params.vix_entry_min == 18.0
    # IB inherits from IC params class but is a separate instance,
    # so its wing_width should be the IC default (8), NOT 12
    assert ib.params.wing_width_strikes == 8


# ── Selection logic ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_routes_to_highest_scoring_child():
    _ensure_imported()
    from src.core.types import OrderSide, OrderType, SignalType
    from src.core.models import Signal, SignalLeg
    from src.strategy.registry import create_strategy

    o = create_strategy(
        "orchestrator",
        strategy_id="orch_picks",
        params={"children": ["iron_condor", "short_strangle"]},
    )
    o.set_context(_make_minimal_ctx())
    await o.on_start()

    # Mock child scores so iron_condor wins
    ic = o._children["iron_condor"]
    sg = o._children["short_strangle"]
    ic.evaluate_score = lambda: 75
    sg.evaluate_score = lambda: 65
    # Mock on_tick on both — only the winner should be called
    sample_signal = Signal(
        strategy_id="ic_dummy",
        signal_type=SignalType.ENTRY,
        legs=[],
        reason="dummy",
    )
    ic.on_tick = AsyncMock(return_value=sample_signal)
    sg.on_tick = AsyncMock(return_value=None)

    tick = MagicMock()
    result = await o.on_tick(tick)

    ic.on_tick.assert_awaited_once()
    sg.on_tick.assert_not_called()
    assert result is sample_signal
    assert o._active_child == "iron_condor"


@pytest.mark.asyncio
async def test_orchestrator_returns_none_when_all_below_threshold():
    _ensure_imported()
    from src.strategy.registry import create_strategy

    o = create_strategy(
        "orchestrator",
        strategy_id="orch_below",
        params={
            "children": ["iron_condor", "short_strangle"],
            "min_score_to_trade": 60,
        },
    )
    o.set_context(_make_minimal_ctx())
    await o.on_start()

    # All below threshold
    o._children["iron_condor"].evaluate_score = lambda: 50
    o._children["short_strangle"].evaluate_score = lambda: 40
    o._children["iron_condor"].on_tick = AsyncMock(return_value=None)
    o._children["short_strangle"].on_tick = AsyncMock(return_value=None)

    tick = MagicMock()
    result = await o.on_tick(tick)

    assert result is None
    o._children["iron_condor"].on_tick.assert_not_called()
    o._children["short_strangle"].on_tick.assert_not_called()
    assert o._active_child is None


@pytest.mark.asyncio
async def test_orchestrator_routes_only_to_active_child_once_position_held():
    """Once a child holds the position, ONLY that child's on_tick fires —
    other children (even high-scoring ones) are not invoked. This prevents
    competing entries while a position is open."""
    _ensure_imported()
    from src.strategy.registry import create_strategy

    o = create_strategy(
        "orchestrator",
        strategy_id="orch_active_routing",
        params={"children": ["iron_condor", "short_strangle"]},
    )
    o.set_context(_make_minimal_ctx())
    await o.on_start()

    # Manually set active child (simulating a prior entry)
    o._active_child = "iron_condor"

    # Other child has a higher score — but should NOT be called
    o._children["iron_condor"].on_tick = AsyncMock(return_value=None)
    o._children["short_strangle"].on_tick = AsyncMock(return_value=None)
    o._children["short_strangle"].evaluate_score = lambda: 99  # would win otherwise

    tick = MagicMock()
    await o.on_tick(tick)

    o._children["iron_condor"].on_tick.assert_awaited_once()
    o._children["short_strangle"].on_tick.assert_not_called()
    assert o._active_child == "iron_condor"  # still active


@pytest.mark.asyncio
async def test_orchestrator_releases_slot_on_active_child_exit():
    _ensure_imported()
    from src.core.models import Signal
    from src.core.types import SignalType
    from src.strategy.registry import create_strategy

    o = create_strategy(
        "orchestrator",
        strategy_id="orch_exit",
        params={"children": ["iron_condor"]},
    )
    o.set_context(_make_minimal_ctx())
    await o.on_start()
    o._active_child = "iron_condor"

    exit_sig = Signal(
        strategy_id="ic_dummy",
        signal_type=SignalType.EXIT,
        legs=[],
        reason="profit target",
    )
    o._children["iron_condor"].on_tick = AsyncMock(return_value=exit_sig)

    tick = MagicMock()
    result = await o.on_tick(tick)

    assert result is exit_sig
    assert o._active_child is None  # slot released


# ── Helpers ────────────────────────────────────────────────────────


def _make_minimal_ctx():
    """Mock context that lets BaseStrategy.set_context succeed and
    children's on_start (which often calls ctx.next_expiry,
    ctx._feed/aggregator/chain_builder) work without real data."""
    ctx = MagicMock()
    ctx._feed = MagicMock()
    ctx._aggregator = MagicMock()
    ctx._chain_builder = MagicMock()
    ctx.clock = MagicMock()
    ctx.clock.now = MagicMock(return_value=datetime(2025, 9, 9, 9, 30))
    ctx.next_expiry = MagicMock(return_value=datetime(2025, 9, 16).date())
    ctx.get_available_expiries = MagicMock(return_value=[
        datetime(2025, 9, 16).date(),
        datetime(2025, 9, 23).date(),
        datetime(2025, 9, 30).date(),
        datetime(2025, 10, 7).date(),
    ])
    ctx.get_vix = MagicMock(return_value=18.0)
    return ctx
