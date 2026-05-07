"""Tests for the runner's resolution of orchestrator child IDs.

May 7 2026 (FnO-v5-orchestration-impl branch).

Orchestrator children carry composite strategy_ids like
``orchestrator_1/iron_condor``. The runner's ``_strategies`` dict only
contains the parent (``orchestrator_1``). Without explicit fallback,
order updates and signal-routing for child IDs miss the parent and
strand the child:

  - ``_on_order_update`` would never call orchestrator.on_order_update
    → child's ``on_order_update`` never fires → child position state
    diverges from reality on every fill
  - ``_process_signal`` would default ``shadow_only=False`` (instead of
    inheriting from the orchestrator) → orchestrated children would
    bypass shadow-mode if the orchestrator is shadow

These tests lock in the ``_resolve_strategy`` fallback so a future
change can't accidentally strand orchestrated children again.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _make_runner_with_orchestrator(shadow_only: bool = False):
    """Build a StrategyRunner with one orchestrator registered.

    Returns (runner, orchestrator_mock). The orchestrator is registered
    in ``_strategies`` under id 'orchestrator_1'; its children are NOT
    in the dict (matching production behaviour — the runner only sees
    the parent).
    """
    from src.strategy.runner import StrategyRunner

    # Mock everything the runner pulls in at __init__
    event_bus = MagicMock()
    feed = MagicMock()
    chain_builder = MagicMock()
    aggregator = MagicMock()
    clock = MagicMock()
    runner = StrategyRunner(
        event_bus=event_bus,
        feed=feed,
        option_chain_builder=chain_builder,
        aggregator=aggregator,
        clock=clock,
        order_callback=MagicMock(),
        portfolio_getter=MagicMock(),
    )

    # Register a stand-in orchestrator
    orchestrator = MagicMock()
    orchestrator.strategy_id = "orchestrator_1"
    orchestrator.params = MagicMock()
    orchestrator.params.shadow_only = shadow_only
    runner._strategies["orchestrator_1"] = orchestrator
    return runner, orchestrator


# ── Resolver behaviour ─────────────────────────────────────────────


def test_resolve_strategy_direct_lookup_for_standalone_id():
    runner, orchestrator = _make_runner_with_orchestrator()
    # Direct lookup hits — matches production behaviour for non-orchestrated strategies
    assert runner._resolve_strategy("orchestrator_1") is orchestrator


def test_resolve_strategy_falls_back_to_parent_for_child_id():
    """Composite child id resolves to the parent orchestrator."""
    runner, orchestrator = _make_runner_with_orchestrator()
    assert runner._resolve_strategy("orchestrator_1/iron_condor") is orchestrator
    assert runner._resolve_strategy("orchestrator_1/iron_butterfly") is orchestrator
    assert runner._resolve_strategy("orchestrator_1/short_strangle") is orchestrator
    assert runner._resolve_strategy("orchestrator_1/trend_daily") is orchestrator


def test_resolve_strategy_returns_none_for_unknown_id():
    runner, _ = _make_runner_with_orchestrator()
    assert runner._resolve_strategy("nonexistent_42") is None
    # Unknown parent prefix also returns None
    assert runner._resolve_strategy("ghost_orch/iron_condor") is None


def test_resolve_strategy_handles_no_slash_id():
    """Plain ids without '/' just direct-lookup (no fallback)."""
    runner, _ = _make_runner_with_orchestrator()
    assert runner._resolve_strategy("standalone_strategy") is None  # not registered


# ── Order-update routing ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_order_update_for_child_id_dispatches_to_parent():
    """Orchestrator's on_order_update fires when an orchestrated child's
    order fills — proves the fallback resolution works end-to-end."""
    from src.core.events import Event, EventType

    runner, orchestrator = _make_runner_with_orchestrator()
    # AsyncMock for on_order_update — record calls
    from unittest.mock import AsyncMock
    orchestrator.on_order_update = AsyncMock()

    # Build a fake order event with a CHILD strategy_id
    event = Event.create(
        EventType.ORDER_FILLED,
        source="test",
        order={
            "broker_order_id": "test-1",
            "tradingsymbol": "NIFTY24500CE",
            "instrument_token": 12345,
            "order_side": "SELL",
            "order_type": "LIMIT",
            "product": "NRML",
            "quantity": 75,
            "price": 100.0,
            "strategy_id": "orchestrator_1/iron_condor",
        },
    )

    await runner._on_order_update(event)

    # Orchestrator's on_order_update was called once
    orchestrator.on_order_update.assert_awaited_once()
    forwarded = orchestrator.on_order_update.await_args.args[0]
    # The order's strategy_id is preserved as the child id, so the
    # orchestrator's internal fan-out can match by prefix.
    assert forwarded.strategy_id == "orchestrator_1/iron_condor"


# ── Shadow-only inheritance ────────────────────────────────────────


@pytest.mark.asyncio
async def test_signal_from_child_inherits_orchestrator_shadow_flag():
    """When the orchestrator is shadow_only, ALL its children's signals
    skip the OMS — fall through to the parent's shadow flag, not the
    None-strategy default which would bypass shadow gating."""
    from src.core.models import Signal
    from src.core.types import SignalType

    runner, orchestrator = _make_runner_with_orchestrator(shadow_only=True)
    # Mock market open + persist_state
    runner._clock.is_market_open = MagicMock(return_value=True)
    from unittest.mock import AsyncMock
    runner._persist_state = AsyncMock()
    runner._order_callback = AsyncMock()

    signal = Signal(
        strategy_id="orchestrator_1/iron_condor",
        signal_type=SignalType.ENTRY,
        legs=[],
        reason="test entry",
    )
    await runner._process_signal(signal)

    # Shadow mode → order_callback NOT invoked, persist_state IS
    runner._order_callback.assert_not_awaited()
    runner._persist_state.assert_awaited()


@pytest.mark.asyncio
async def test_signal_from_child_routes_live_when_orchestrator_live():
    """When the orchestrator is LIVE (shadow_only=False), the child's
    signal reaches the OMS — proves the fallback doesn't accidentally
    block live orchestration."""
    from src.core.models import Signal
    from src.core.types import SignalType

    runner, orchestrator = _make_runner_with_orchestrator(shadow_only=False)
    runner._clock.is_market_open = MagicMock(return_value=True)
    from unittest.mock import AsyncMock
    runner._persist_state = AsyncMock()
    runner._order_callback = AsyncMock()

    signal = Signal(
        strategy_id="orchestrator_1/iron_condor",
        signal_type=SignalType.ENTRY,
        legs=[],
        reason="test entry",
    )
    await runner._process_signal(signal)

    # LIVE → order_callback awaited
    runner._order_callback.assert_awaited_once_with(signal)
