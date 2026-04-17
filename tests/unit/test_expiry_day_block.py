"""Tests for the Tuesday 0DTE expiry-day entry block.

Validates the Apr 17 trader-analysis fix: premium-selling strategies (IC,
strangle, straddle, portfolio premium leg) must never enter new positions
when today is the weekly NIFTY expiry. The 14:30-15:15 gamma vertical can
move ATM 100% in minutes, and ITM auto-exercise STT eats any "win".
"""

from datetime import time
from unittest.mock import MagicMock

import pytest

from src.strategy.params import (
    BaseStrategyParams,
    IronCondorParams,
    PortfolioParams,
    ShortStraddleParams,
    ShortStrangleParams,
)


def _make_strategy_stub(params, is_expiry: bool):
    """Lightweight stub: only what _check_expiry_day_block touches."""
    from src.strategy.base import BaseStrategy

    class _Stub(BaseStrategy):
        def get_subscriptions(self): pass
        async def on_start(self): pass
        async def on_tick(self, tick): pass
        async def on_stop(self): pass

    stub = _Stub("test-id", params)
    ctx = MagicMock()
    ctx.clock.is_expiry_day.return_value = is_expiry
    stub.set_context(ctx)
    return stub


class TestExpiryDayBlockDefaults:
    def test_block_default_enabled_on_base_params(self):
        assert BaseStrategyParams().skip_entry_on_expiry_day is True

    def test_block_default_enabled_on_iron_condor(self):
        assert IronCondorParams().skip_entry_on_expiry_day is True

    def test_block_default_enabled_on_strangle(self):
        assert ShortStrangleParams().skip_entry_on_expiry_day is True

    def test_block_default_enabled_on_straddle(self):
        assert ShortStraddleParams().skip_entry_on_expiry_day is True

    def test_block_default_enabled_on_portfolio(self):
        assert PortfolioParams().skip_entry_on_expiry_day is True

    def test_force_exit_default_at_14_30(self):
        # Before the 14:30-15:15 gamma vertical
        assert BaseStrategyParams().expiry_day_force_exit_at == time(14, 30)


class TestExpiryDayBlockBehavior:
    def test_blocks_when_today_is_expiry(self):
        stub = _make_strategy_stub(IronCondorParams(), is_expiry=True)
        result = stub._check_expiry_day_block("NIFTY")
        assert result is not None
        assert "expiry today" in result
        assert "0DTE" in result

    def test_passes_when_today_is_not_expiry(self):
        stub = _make_strategy_stub(IronCondorParams(), is_expiry=False)
        assert stub._check_expiry_day_block("NIFTY") is None

    def test_can_disable_via_param(self):
        params = IronCondorParams(skip_entry_on_expiry_day=False)
        stub = _make_strategy_stub(params, is_expiry=True)
        # Even on expiry day, returns None when explicitly disabled
        assert stub._check_expiry_day_block("NIFTY") is None

    def test_passes_through_underlying_to_clock(self):
        stub = _make_strategy_stub(IronCondorParams(), is_expiry=False)
        stub._check_expiry_day_block("BANKNIFTY")
        stub.ctx.clock.is_expiry_day.assert_called_once_with("BANKNIFTY")


class TestExpiryBlockWiredIntoStrategies:
    """Sanity: each premium strategy must call _check_expiry_day_block in entry path."""

    def test_short_strangle_calls_expiry_block(self):
        from src.strategy.implementations import short_strangle
        import inspect
        src = inspect.getsource(short_strangle)
        assert "_check_expiry_day_block" in src

    def test_iron_condor_calls_expiry_block(self):
        from src.strategy.implementations import iron_condor
        import inspect
        src = inspect.getsource(iron_condor)
        assert "_check_expiry_day_block" in src

    def test_short_straddle_calls_expiry_block(self):
        from src.strategy.implementations import short_straddle
        import inspect
        src = inspect.getsource(short_straddle)
        assert "_check_expiry_day_block" in src

    def test_portfolio_premium_leg_calls_expiry_block(self):
        from src.strategy.implementations import portfolio_strategy
        import inspect
        src = inspect.getsource(portfolio_strategy)
        assert "_check_expiry_day_block" in src
