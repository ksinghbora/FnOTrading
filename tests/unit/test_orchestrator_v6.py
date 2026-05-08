"""Tests for V6 multi-slot orchestrator behaviour.

V6 (May 8 2026) — orchestrator can hold multiple children active
concurrently up to ``max_concurrent_slots``. Default 1 preserves V4/V5
single-slot semantics; existing tests in test_orchestrator.py and
test_orchestrator_v5.py are unchanged and still pass.

Contract locked in here:
  1. Multi-slot entry: 2 children with different families both enter
     on the same tick when slots available
  2. Per-slot exits: child A exits while child B keeps running
  3. max_concurrent_slots cap respected
  4. Capital cap (max_total_margin_lakhs) blocks entries that would
     exceed budget
  5. Correlation guard with multi-slot: same-family children blocked
     after first family member entered
  6. Phase 1 exits + Phase 5 entries can coexist on the same tick
     (orchestrator returns list[Signal])
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest


def _ensure_imported():
    from src.backtest.common import import_strategies
    import_strategies()


def _make_minimal_ctx():
    ctx = MagicMock()
    ctx._feed = MagicMock()
    ctx._aggregator = MagicMock()
    ctx._chain_builder = MagicMock()
    ctx.clock = MagicMock()
    ctx.clock.now = MagicMock(return_value=datetime(2025, 9, 9, 9, 30))
    ctx.next_expiry = MagicMock(return_value=date(2025, 9, 16))
    ctx.get_available_expiries = MagicMock(return_value=[
        date(2025, 9, 16), date(2025, 9, 23), date(2025, 9, 30), date(2025, 10, 7),
    ])
    ctx.get_vix = MagicMock(return_value=18.0)
    return ctx


# ─── Multi-slot entry on the same tick ─────────────────────────────


@pytest.mark.asyncio
async def test_v6_two_children_enter_concurrently():
    """With max_concurrent_slots=2, two children both enter on the same tick."""
    _ensure_imported()
    from src.core.models import Signal
    from src.core.types import SignalType
    from src.strategy.registry import create_strategy

    o = create_strategy(
        "orchestrator",
        strategy_id="orch_v6_2slot",
        params={
            "children": ["iron_condor", "short_strangle"],
            "max_concurrent_slots": 2,
        },
    )
    o.set_context(_make_minimal_ctx())
    await o.on_start()

    o._children["iron_condor"].evaluate_score = lambda: 80
    o._children["short_strangle"].evaluate_score = lambda: 70
    ic_sig = Signal(strategy_id="ic", signal_type=SignalType.ENTRY, legs=[], reason="ic")
    ss_sig = Signal(strategy_id="ss", signal_type=SignalType.ENTRY, legs=[], reason="ss")
    o._children["iron_condor"].on_tick = AsyncMock(return_value=ic_sig)
    o._children["short_strangle"].on_tick = AsyncMock(return_value=ss_sig)

    result = await o.on_tick(MagicMock())

    # Both children should be active
    assert "iron_condor" in o._active_children
    assert "short_strangle" in o._active_children
    # Both signals returned (as a list)
    assert isinstance(result, list)
    assert len(result) == 2
    assert ic_sig in result
    assert ss_sig in result


@pytest.mark.asyncio
async def test_v6_max_slots_cap_respected():
    """With max_concurrent_slots=1, only the highest-scoring child enters
    even if multiple are eligible (V4 backward-compat default)."""
    _ensure_imported()
    from src.core.models import Signal
    from src.core.types import SignalType
    from src.strategy.registry import create_strategy

    o = create_strategy(
        "orchestrator",
        strategy_id="orch_v6_1slot",
        params={
            "children": ["iron_condor", "short_strangle"],
            "max_concurrent_slots": 1,  # Default
        },
    )
    o.set_context(_make_minimal_ctx())
    await o.on_start()

    o._children["iron_condor"].evaluate_score = lambda: 80
    o._children["short_strangle"].evaluate_score = lambda: 70
    ic_sig = Signal(strategy_id="ic", signal_type=SignalType.ENTRY, legs=[], reason="ic")
    o._children["iron_condor"].on_tick = AsyncMock(return_value=ic_sig)
    o._children["short_strangle"].on_tick = AsyncMock(return_value=None)

    result = await o.on_tick(MagicMock())

    assert o._active_children == {"iron_condor"}
    o._children["short_strangle"].on_tick.assert_not_called()
    assert result is ic_sig


# ─── Per-slot exits ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_v6_one_child_exits_other_stays():
    """When child A exits, child B's slot remains active."""
    _ensure_imported()
    from src.core.models import Signal
    from src.core.types import SignalType
    from src.strategy.registry import create_strategy

    o = create_strategy(
        "orchestrator",
        strategy_id="orch_v6_exit",
        params={
            "children": ["iron_condor", "short_strangle"],
            "max_concurrent_slots": 2,
        },
    )
    o.set_context(_make_minimal_ctx())
    await o.on_start()
    # Pre-set both as active (simulating a prior tick that entered both)
    o._active_children = {"iron_condor", "short_strangle"}

    # IC emits EXIT, SS emits no signal (still holding)
    ic_exit = Signal(strategy_id="ic", signal_type=SignalType.EXIT, legs=[], reason="profit")
    o._children["iron_condor"].on_tick = AsyncMock(return_value=ic_exit)
    o._children["short_strangle"].on_tick = AsyncMock(return_value=None)
    # Mock evaluate_score so no NEW entry attempts
    o._children["iron_condor"].evaluate_score = lambda: 0
    o._children["short_strangle"].evaluate_score = lambda: 0

    result = await o.on_tick(MagicMock())

    assert "iron_condor" not in o._active_children
    assert "short_strangle" in o._active_children
    assert result is ic_exit


@pytest.mark.asyncio
async def test_v6_phase1_exit_plus_phase5_entry_same_tick():
    """An EXIT signal from one child + ENTRY signal from another can coexist."""
    _ensure_imported()
    from src.core.models import Signal
    from src.core.types import SignalType
    from src.strategy.registry import create_strategy

    o = create_strategy(
        "orchestrator",
        strategy_id="orch_v6_mixed",
        params={
            "children": ["iron_condor", "short_strangle", "iron_butterfly"],
            "max_concurrent_slots": 3,
        },
    )
    o.set_context(_make_minimal_ctx())
    await o.on_start()
    # IC pre-active; SS and IB inactive
    o._active_children = {"iron_condor"}

    ic_exit = Signal(strategy_id="ic", signal_type=SignalType.EXIT, legs=[], reason="exit")
    ss_entry = Signal(strategy_id="ss", signal_type=SignalType.ENTRY, legs=[], reason="ss-entry")
    ib_entry = Signal(strategy_id="ib", signal_type=SignalType.ENTRY, legs=[], reason="ib-entry")

    o._children["iron_condor"].on_tick = AsyncMock(return_value=ic_exit)
    o._children["short_strangle"].evaluate_score = lambda: 80
    o._children["short_strangle"].on_tick = AsyncMock(return_value=ss_entry)
    o._children["iron_butterfly"].evaluate_score = lambda: 70
    o._children["iron_butterfly"].on_tick = AsyncMock(return_value=ib_entry)
    # IC's evaluate_score doesn't matter — Phase 1 polls active children
    # without re-checking score.
    o._children["iron_condor"].evaluate_score = lambda: 0

    result = await o.on_tick(MagicMock())

    # IC released its slot; SS + IB took new slots
    assert "iron_condor" not in o._active_children
    assert "short_strangle" in o._active_children
    assert "iron_butterfly" in o._active_children
    # All three signals returned
    assert isinstance(result, list)
    assert len(result) == 3
    assert ic_exit in result
    assert ss_entry in result
    assert ib_entry in result


# ─── Capital cap (max_total_margin_lakhs) ──────────────────────────


@pytest.mark.asyncio
async def test_v6_capital_cap_blocks_third_entry():
    """Sum of active expected_margin_per_lot_lakhs must stay under cap."""
    _ensure_imported()
    from src.core.models import Signal
    from src.core.types import SignalType
    from src.strategy.registry import create_strategy

    # IC=2.5L, IB=1.5L, SS=1.5L — set cap to 4.5L: max 2 of (IC+IB) or
    # similar combos before the third blocks.
    o = create_strategy(
        "orchestrator",
        strategy_id="orch_v6_capital",
        params={
            "children": ["iron_condor", "iron_butterfly", "short_strangle"],
            "max_concurrent_slots": 3,
            "max_total_margin_lakhs": 4.5,
        },
    )
    o.set_context(_make_minimal_ctx())
    await o.on_start()

    # All three score eligible
    for cn in ("iron_condor", "iron_butterfly", "short_strangle"):
        o._children[cn].evaluate_score = lambda: 80
    ic_sig = Signal(strategy_id="ic", signal_type=SignalType.ENTRY, legs=[], reason="ic")
    ib_sig = Signal(strategy_id="ib", signal_type=SignalType.ENTRY, legs=[], reason="ib")
    ss_sig = Signal(strategy_id="ss", signal_type=SignalType.ENTRY, legs=[], reason="ss")
    o._children["iron_condor"].on_tick = AsyncMock(return_value=ic_sig)
    o._children["iron_butterfly"].on_tick = AsyncMock(return_value=ib_sig)
    o._children["short_strangle"].on_tick = AsyncMock(return_value=ss_sig)

    result = await o.on_tick(MagicMock())

    # Order: IC (2.5L) tried first by list position with score-tie.
    # IC + IB = 4.0L (under 4.5L) → both enter.
    # SS would push to 5.5L → blocked.
    assert "iron_condor" in o._active_children
    assert "iron_butterfly" in o._active_children
    assert "short_strangle" not in o._active_children
    o._children["short_strangle"].on_tick.assert_not_called()


@pytest.mark.asyncio
async def test_v6_capital_cap_zero_disables():
    """max_total_margin_lakhs=0.0 disables the budget gate (enters all)."""
    _ensure_imported()
    from src.core.models import Signal
    from src.core.types import SignalType
    from src.strategy.registry import create_strategy

    o = create_strategy(
        "orchestrator",
        strategy_id="orch_v6_no_cap",
        params={
            "children": ["iron_condor", "iron_butterfly", "short_strangle"],
            "max_concurrent_slots": 3,
            "max_total_margin_lakhs": 0.0,  # Disabled
        },
    )
    o.set_context(_make_minimal_ctx())
    await o.on_start()

    for cn in ("iron_condor", "iron_butterfly", "short_strangle"):
        o._children[cn].evaluate_score = lambda: 80
        sig = Signal(strategy_id=cn, signal_type=SignalType.ENTRY, legs=[], reason="x")
        o._children[cn].on_tick = AsyncMock(return_value=sig)

    await o.on_tick(MagicMock())
    # All 3 enter (no cap)
    assert o._active_children == {"iron_condor", "iron_butterfly", "short_strangle"}


# ─── Correlation guard with multi-slot ─────────────────────────────


@pytest.mark.asyncio
async def test_v6_correlation_guard_blocks_same_family():
    """With block_correlated_families on, after IC enters (premium_selling),
    IB and SS (also premium_selling) are blocked even with free slots."""
    _ensure_imported()
    from src.core.models import Signal
    from src.core.types import SignalType
    from src.strategy.registry import create_strategy

    o = create_strategy(
        "orchestrator",
        strategy_id="orch_v6_corr",
        params={
            "children": ["iron_condor", "iron_butterfly", "short_strangle"],
            "max_concurrent_slots": 3,
            "block_correlated_families": True,
        },
    )
    o.set_context(_make_minimal_ctx())
    await o.on_start()

    for cn in ("iron_condor", "iron_butterfly", "short_strangle"):
        o._children[cn].evaluate_score = lambda: 80
        sig = Signal(strategy_id=cn, signal_type=SignalType.ENTRY, legs=[], reason="x")
        o._children[cn].on_tick = AsyncMock(return_value=sig)

    await o.on_tick(MagicMock())

    # Only one premium_selling child enters; IB + SS blocked by correlation guard
    assert len(o._active_children) == 1
    assert "iron_condor" in o._active_children  # tie-break wins by list order
    assert "iron_butterfly" not in o._active_children
    assert "short_strangle" not in o._active_children


# ─── Backward-compat property accessor ─────────────────────────────


def test_v6_active_child_property_returns_first_active():
    """Legacy ``_active_child`` accessor returns the first (by list position)
    active child name, or None when no slots are active."""
    _ensure_imported()
    from src.strategy.registry import create_strategy

    o = create_strategy(
        "orchestrator",
        strategy_id="orch_v6_compat",
        params={"children": ["iron_condor", "short_strangle", "iron_butterfly"]},
    )

    # No active children
    assert o._active_child is None
    # Multiple actives — returns first by list position
    o._active_children = {"short_strangle", "iron_butterfly"}
    assert o._active_child == "short_strangle"
    # Setting via legacy setter clears + sets one
    o._active_child = "iron_condor"
    assert o._active_children == {"iron_condor"}
    o._active_child = None
    assert o._active_children == set()
