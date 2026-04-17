"""Tests for the Apr 17 trail-stop activation gates.

Without gates, the trail-stop fires during the first 30 min of session
when ATM premium can swing 10-20% on auction-imbalance noise alone. The
two-gate guard:
  1. Time gate — wait past 10:15 (default), letting opening noise pass.
  2. Move gate — require the premium to have decayed at least
     `trail_stop_min_decay_pct` (5% default) so we know it's a real winner
     before locking in profits.
"""

from datetime import datetime, time
from unittest.mock import MagicMock

import pytest
import pytz

from src.strategy.base import BaseStrategy
from src.strategy.params import (
    BaseStrategyParams,
    IronCondorParams,
    ShortStraddleParams,
    ShortStrangleParams,
)


IST = pytz.timezone("Asia/Kolkata")


class _Stub(BaseStrategy):
    def get_subscriptions(self): pass
    async def on_start(self): pass
    async def on_tick(self, tick): pass
    async def on_stop(self): pass


def _make_stub(params, hour: int, minute: int):
    stub = _Stub("test-id", params)
    ctx = MagicMock()
    fixed_now = IST.localize(datetime(2026, 4, 17, hour, minute))
    ctx.clock.now.return_value = fixed_now
    stub.set_context(ctx)
    return stub


class TestParamDefaults:
    def test_activate_after_default_is_10_15(self):
        assert BaseStrategyParams().trail_stop_activate_after_time == time(10, 15)

    def test_min_decay_default_5pct(self):
        assert BaseStrategyParams().trail_stop_min_decay_pct == 5.0

    def test_defaults_inherited_by_strangle(self):
        p = ShortStrangleParams()
        assert p.trail_stop_activate_after_time == time(10, 15)
        assert p.trail_stop_min_decay_pct == 5.0

    def test_defaults_inherited_by_straddle(self):
        p = ShortStraddleParams()
        assert p.trail_stop_activate_after_time == time(10, 15)

    def test_defaults_inherited_by_iron_condor(self):
        p = IronCondorParams()
        assert p.trail_stop_activate_after_time == time(10, 15)


class TestTimeGate:
    def test_blocks_before_activation_time(self):
        # 9:45 — well past entry but before 10:15 trail-activation
        stub = _make_stub(IronCondorParams(), 9, 45)
        # Even with strong decay, time gate blocks
        assert stub._can_activate_trail_stop(decay_pct=20.0) is False

    def test_blocks_at_one_minute_before_activation(self):
        stub = _make_stub(IronCondorParams(), 10, 14)
        assert stub._can_activate_trail_stop(decay_pct=20.0) is False

    def test_passes_at_activation_time(self):
        stub = _make_stub(IronCondorParams(), 10, 15)
        assert stub._can_activate_trail_stop(decay_pct=20.0) is True

    def test_passes_after_activation_time(self):
        stub = _make_stub(IronCondorParams(), 13, 30)
        assert stub._can_activate_trail_stop(decay_pct=20.0) is True


class TestMoveGate:
    def test_blocks_when_decay_below_threshold(self):
        # Past the time gate, but premium has barely moved
        stub = _make_stub(IronCondorParams(), 11, 0)
        assert stub._can_activate_trail_stop(decay_pct=4.9) is False

    def test_passes_at_threshold(self):
        stub = _make_stub(IronCondorParams(), 11, 0)
        assert stub._can_activate_trail_stop(decay_pct=5.0) is True

    def test_passes_above_threshold(self):
        stub = _make_stub(IronCondorParams(), 11, 0)
        assert stub._can_activate_trail_stop(decay_pct=15.0) is True

    def test_negative_decay_blocked(self):
        # decay_pct < 0 means premium has expanded (losing) — never trail
        stub = _make_stub(IronCondorParams(), 11, 0)
        assert stub._can_activate_trail_stop(decay_pct=-10.0) is False


class TestBothGatesMustPass:
    def test_time_ok_decay_low_blocks(self):
        stub = _make_stub(IronCondorParams(), 14, 0)
        assert stub._can_activate_trail_stop(decay_pct=2.0) is False

    def test_time_early_decay_high_blocks(self):
        stub = _make_stub(IronCondorParams(), 9, 30)
        assert stub._can_activate_trail_stop(decay_pct=30.0) is False

    def test_both_pass(self):
        stub = _make_stub(IronCondorParams(), 11, 30)
        assert stub._can_activate_trail_stop(decay_pct=10.0) is True


class TestParamOverrides:
    def test_custom_activation_time(self):
        params = IronCondorParams(trail_stop_activate_after_time=time(11, 30))
        stub = _make_stub(params, 11, 0)
        assert stub._can_activate_trail_stop(decay_pct=20.0) is False
        stub2 = _make_stub(params, 11, 30)
        assert stub2._can_activate_trail_stop(decay_pct=20.0) is True

    def test_custom_min_decay(self):
        params = IronCondorParams(trail_stop_min_decay_pct=20.0)
        stub = _make_stub(params, 14, 0)
        assert stub._can_activate_trail_stop(decay_pct=15.0) is False
        assert stub._can_activate_trail_stop(decay_pct=25.0) is True

    def test_zero_min_decay_disables_move_gate(self):
        params = IronCondorParams(trail_stop_min_decay_pct=0.0)
        stub = _make_stub(params, 11, 0)
        # Even tiny decay (or zero) passes the move gate
        assert stub._can_activate_trail_stop(decay_pct=0.0) is True


class TestWiredIntoStrategies:
    """Sanity: each premium strategy must consult the gate before trailing."""

    def test_short_strangle_calls_gate(self):
        from src.strategy.implementations import short_strangle
        import inspect
        src = inspect.getsource(short_strangle)
        assert "_can_activate_trail_stop" in src

    def test_short_straddle_calls_gate(self):
        from src.strategy.implementations import short_straddle
        import inspect
        src = inspect.getsource(short_straddle)
        assert "_can_activate_trail_stop" in src

    def test_portfolio_strategy_calls_gate(self):
        from src.strategy.implementations import portfolio_strategy
        import inspect
        src = inspect.getsource(portfolio_strategy)
        assert "_can_activate_trail_stop" in src
