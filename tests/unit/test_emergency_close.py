"""Tests for BaseStrategy.on_error → emergency-close behavior.

Validates the Apr 17 HIGH-2 fix: an unhandled exception inside a strategy
must (a) disable further entries today and (b) flatten any open positions.
Apr 13 incident: 5,321 wing-strike exceptions on IC could have left half-built
positions unmanaged until exchange auto-square-off.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.models import Position
from src.core.types import OrderSide, ProductType, SignalType
from src.strategy.base import BaseStrategy
from src.strategy.params import IronCondorParams


class _Stub(BaseStrategy):
    def get_subscriptions(self): pass
    async def on_start(self): pass
    async def on_tick(self, tick): pass
    async def on_stop(self): pass


def _make_stub(positions: list[Position] | None = None):
    stub = _Stub("test-strategy", IronCondorParams())
    stub._stopped_for_day = False
    ctx = MagicMock()
    ctx.get_open_positions.return_value = positions or []
    ctx.place_signal = AsyncMock(return_value=[])
    stub.set_context(ctx)
    return stub, ctx


def _pos(symbol: str, token: int, qty: int) -> Position:
    return Position(
        instrument_token=token,
        tradingsymbol=symbol,
        strategy_id="test-strategy",
        quantity=qty,
        average_price=Decimal("100"),
    )


class TestOnErrorStopsForDay:
    @pytest.mark.asyncio
    async def test_sets_stopped_for_day(self):
        stub, _ctx = _make_stub()
        await stub.on_error(RuntimeError("kaboom"))
        assert stub._stopped_for_day is True

    @pytest.mark.asyncio
    async def test_safe_when_no_stopped_flag(self):
        # Strategy without _stopped_for_day attr must not crash
        stub = _Stub("test-strategy", IronCondorParams())
        ctx = MagicMock()
        ctx.get_open_positions.return_value = []
        ctx.place_signal = AsyncMock(return_value=[])
        stub.set_context(ctx)
        # Must not raise
        await stub.on_error(RuntimeError("kaboom"))


class TestEmergencyCloseSignals:
    @pytest.mark.asyncio
    async def test_no_positions_no_signal(self):
        stub, ctx = _make_stub(positions=[])
        await stub.on_error(ValueError("test"))
        ctx.place_signal.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_quantity_position_ignored(self):
        stub, ctx = _make_stub(positions=[_pos("X", 1, 0)])
        await stub.on_error(ValueError("test"))
        ctx.place_signal.assert_not_called()

    @pytest.mark.asyncio
    async def test_long_position_flattened_with_sell(self):
        stub, ctx = _make_stub(positions=[_pos("NIFTY24500CE", 100, 75)])
        await stub.on_error(ValueError("test"))
        ctx.place_signal.assert_called_once()
        signal = ctx.place_signal.call_args[0][0]
        assert signal.signal_type == SignalType.EXIT
        assert len(signal.legs) == 1
        assert signal.legs[0].order_side == OrderSide.SELL
        assert signal.legs[0].quantity == 75
        assert "emergency_close" in signal.reason

    @pytest.mark.asyncio
    async def test_short_position_flattened_with_buy(self):
        stub, ctx = _make_stub(positions=[_pos("NIFTY24500CE", 100, -75)])
        await stub.on_error(ValueError("test"))
        signal = ctx.place_signal.call_args[0][0]
        assert signal.legs[0].order_side == OrderSide.BUY
        assert signal.legs[0].quantity == 75

    @pytest.mark.asyncio
    async def test_multi_leg_iron_condor_flattens_all(self):
        positions = [
            _pos("NIFTY24500CE", 1, -75),  # short call
            _pos("NIFTY24600CE", 2, 75),   # long call (wing)
            _pos("NIFTY24400PE", 3, -75),  # short put
            _pos("NIFTY24300PE", 4, 75),   # long put (wing)
        ]
        stub, ctx = _make_stub(positions=positions)
        await stub.on_error(ValueError("wing missing"))
        signal = ctx.place_signal.call_args[0][0]
        assert len(signal.legs) == 4
        # Reversed sides: shorts → BUY, longs → SELL
        sides = [(leg.tradingsymbol, leg.order_side) for leg in signal.legs]
        assert ("NIFTY24500CE", OrderSide.BUY) in sides
        assert ("NIFTY24600CE", OrderSide.SELL) in sides
        assert ("NIFTY24400PE", OrderSide.BUY) in sides
        assert ("NIFTY24300PE", OrderSide.SELL) in sides

    @pytest.mark.asyncio
    async def test_close_failure_does_not_propagate(self):
        # If place_signal itself raises, on_error must swallow (logging only)
        stub, ctx = _make_stub(positions=[_pos("X", 1, 75)])
        ctx.place_signal = AsyncMock(side_effect=RuntimeError("broker down"))
        # Must not re-raise
        await stub.on_error(ValueError("original"))
        assert stub._stopped_for_day is True

    @pytest.mark.asyncio
    async def test_reason_carries_error_type_name(self):
        stub, ctx = _make_stub(positions=[_pos("X", 1, 75)])
        await stub.on_error(KeyError("missing strike"))
        signal = ctx.place_signal.call_args[0][0]
        assert "KeyError" in signal.reason
