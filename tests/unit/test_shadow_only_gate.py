"""Tests for the shadow_only runner gate (Apr 18 plan-review fix).

Motivation: the prior paper-trading plan ran 5 NIFTY strategies (portfolio
+ ic + strangle + straddle + trend) live in parallel, on overlapping
weekly options. Four of those are short-vol — running them in parallel is
not diversification, it's a 4× concentrated short-vol position. P&L
attribution is impossible because positions/Greeks overlap.

The shadow_only flag lets us run a single live "champion" (portfolio) plus
N "challenger" strategies whose decisions are logged but never routed to
the OMS. Champion P&L stays clean; challenger hypothetical P&L is
reconstructable from the decision logs.

Contracts pinned here:
  1. shadow_only=True signal does NOT call _order_callback
  2. shadow_only=False (default) signal DOES call _order_callback
  3. Outside market hours, shadow signals also skip routing
  4. State persistence still runs in shadow mode (so "entered" flags
     survive a restart and the strategy doesn't re-fire the same entry)
  5. The shadow log line carries enough leg detail for offline analysis
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.core.events import EventBus  # noqa: E402
from src.core.models import Signal, SignalLeg  # noqa: E402
from src.core.types import OrderSide, OrderType, SignalType  # noqa: E402
from src.strategy.params import BaseStrategyParams  # noqa: E402
from src.strategy.runner import StrategyRunner  # noqa: E402


def _make_runner(order_callback) -> StrategyRunner:
    """Spin up a runner with mocks for everything _process_signal touches."""
    event_bus = EventBus()
    feed = MagicMock()
    chain_builder = MagicMock()
    aggregator = MagicMock()
    clock = MagicMock()
    clock.is_market_open.return_value = True

    state_store = MagicMock()
    state_store.enabled = False  # Skip persistence path; covered separately.
    state_store.save_state = AsyncMock()

    runner = StrategyRunner(
        event_bus, feed, chain_builder, aggregator, clock,
        order_callback, lambda what, sid: None,
        state_store=state_store,
    )
    return runner


def _make_signal(strategy_id: str = "shadow_test") -> Signal:
    return Signal(
        strategy_id=strategy_id,
        signal_type=SignalType.ENTRY,
        legs=[
            SignalLeg(
                instrument_token=12345,
                tradingsymbol="NIFTY26APR24500CE",
                order_side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                quantity=75,
                price=Decimal("0"),
            ),
        ],
        reason="test entry",
        timestamp=datetime.now(),
    )


def _make_strategy(strategy_id: str, shadow_only: bool):
    """Bare-minimum stand-in: only needs `.params.shadow_only` and a
    state_data shape that satisfies _persist_state's no-op path."""
    strategy = MagicMock()
    strategy.strategy_id = strategy_id
    strategy.params = BaseStrategyParams(shadow_only=shadow_only)
    strategy.get_state_data = MagicMock(return_value=None)
    return strategy


def test_shadow_only_signal_skips_oms(caplog):
    """shadow_only=True must NOT invoke the order callback."""
    callback = AsyncMock()
    runner = _make_runner(callback)
    runner._strategies["shadow_test"] = _make_strategy("shadow_test", shadow_only=True)

    signal = _make_signal("shadow_test")
    with caplog.at_level(logging.INFO, logger="src.strategy.runner"):
        asyncio.run(runner._process_signal(signal))

    callback.assert_not_called()
    assert any("[SHADOW_ORDER]" in r.message for r in caplog.records), \
        "shadow signal must produce a [SHADOW_ORDER] log line for offline reconstruction"


def test_live_signal_calls_oms():
    """shadow_only=False (default) must invoke the order callback."""
    callback = AsyncMock()
    runner = _make_runner(callback)
    runner._strategies["live_test"] = _make_strategy("live_test", shadow_only=False)

    signal = _make_signal("live_test")
    asyncio.run(runner._process_signal(signal))

    callback.assert_called_once()
    called_signal = callback.call_args[0][0]
    assert called_signal.strategy_id == "live_test"


def test_shadow_log_contains_leg_detail(caplog):
    """The shadow log must capture side / quantity / symbol for offline P&L
    reconstruction. Without this the challenger run is just a count."""
    callback = AsyncMock()
    runner = _make_runner(callback)
    runner._strategies["shadow_test"] = _make_strategy("shadow_test", shadow_only=True)

    signal = _make_signal("shadow_test")
    with caplog.at_level(logging.INFO, logger="src.strategy.runner"):
        asyncio.run(runner._process_signal(signal))

    msg = next(r.message for r in caplog.records if "[SHADOW_ORDER]" in r.message)
    # Side (SELL), quantity (75), symbol — all must appear.
    assert "SELL" in msg
    assert "75" in msg
    assert "NIFTY26APR24500CE" in msg


def test_shadow_strategy_unknown_to_runner_falls_through_to_live():
    """If the runner has no record of the strategy_id (race condition during
    add/remove), default to live behavior — better to place a real order
    than silently swallow it. The OMS layer below has its own guards."""
    callback = AsyncMock()
    runner = _make_runner(callback)
    # _strategies is empty; signal arrives for unknown id.
    signal = _make_signal("ghost_strategy")
    asyncio.run(runner._process_signal(signal))
    callback.assert_called_once()


def test_market_closed_blocks_shadow_too():
    """Outside market hours, shadow signals also skip everything. No log
    spam from off-hours bot test signals."""
    callback = AsyncMock()
    runner = _make_runner(callback)
    runner._clock.is_market_open.return_value = False
    runner._strategies["shadow_test"] = _make_strategy("shadow_test", shadow_only=True)

    signal = _make_signal("shadow_test")
    asyncio.run(runner._process_signal(signal))

    callback.assert_not_called()


def test_state_persistence_still_runs_in_shadow_mode():
    """The strategy's day flags (entered, trades_today) must persist even
    in shadow mode, otherwise a restart would re-fire the same entry on
    the next tick."""
    callback = AsyncMock()
    runner = _make_runner(callback)
    runner._state_store.enabled = True
    runner._state_store.save_state = AsyncMock()
    runner._clock.now = MagicMock(return_value=datetime.now())

    strategy = _make_strategy("shadow_test", shadow_only=True)
    strategy.get_state_data = MagicMock(return_value={"prem_entered": True})
    runner._strategies["shadow_test"] = strategy

    signal = _make_signal("shadow_test")
    asyncio.run(runner._process_signal(signal))

    callback.assert_not_called()
    runner._state_store.save_state.assert_awaited_once()
