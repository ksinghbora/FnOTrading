"""Tests for the LongCalendar strategy structural properties.

Long Calendar is the only positive-vega strategy in the roster (Phase
3b candidate, Apr 27 2026). These tests assert the defaults are sane
and the back-expiry finder handles the expected NIFTY weekly cadence
correctly. Full end-to-end behavior is exercised via the standalone
validation harness on the GDFL parquet corpus.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest


def test_long_calendar_registered():
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.registry import list_strategies
    assert "long_calendar" in list_strategies()


def test_long_calendar_creates_with_defaults():
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.registry import create_strategy
    s = create_strategy("long_calendar", strategy_id="lc_smoke")
    assert type(s).__name__ == "LongCalendarStrategy"
    assert s.params.leg_type == "CE"
    assert s.params.vix_entry_min == 14.0
    assert s.params.vix_entry_max == 25.0
    assert s.params.profit_target_pct == 30.0
    assert s.params.stop_loss_pct == 50.0
    # Gate B opt-in (inherited from BaseStrategyParams) — default disabled
    assert s.params.intraday_vix_spike_enabled is False


def test_long_calendar_back_expiry_picks_first_with_min_gap():
    """_find_back_expiry must return the smallest expiry at least
    ``min_back_days`` after front (default 21)."""
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.registry import create_strategy
    s = create_strategy("long_calendar", strategy_id="lc_back_expiry")

    # Weekly + monthly expiries: front on 2025-09-09, weeklies at +7, +14
    # days, monthly at +21 days. Default min_back_days=21 → must skip the
    # two weeklies and land on the +21-day expiry.
    expiries = [date(2025, 9, 9), date(2025, 9, 16), date(2025, 9, 23), date(2025, 9, 30)]
    ctx = MagicMock()
    ctx.get_available_expiries = MagicMock(return_value=expiries)
    s._context = ctx
    s._front_expiry = date(2025, 9, 9)
    assert s._find_back_expiry() == date(2025, 9, 30)


def test_long_calendar_back_expiry_returns_none_when_nothing_far_enough():
    """If no expiry is at least min_back_days out, return None (skip trade)."""
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.registry import create_strategy
    s = create_strategy("long_calendar", strategy_id="lc_back_expiry_none")
    # Only weeklies — none reach 21 days from front
    expiries = [date(2025, 9, 9), date(2025, 9, 16), date(2025, 9, 23)]
    ctx = MagicMock()
    ctx.get_available_expiries = MagicMock(return_value=expiries)
    s._context = ctx
    s._front_expiry = date(2025, 9, 9)
    assert s._find_back_expiry() is None


def test_long_calendar_back_expiry_unsorted_input_handled():
    """The finder must sort expiries internally — broker chains aren't always sorted."""
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.registry import create_strategy
    s = create_strategy("long_calendar", strategy_id="lc_back_expiry_unsorted")
    # Deliberately scrambled
    expiries = [date(2025, 9, 30), date(2025, 9, 9), date(2025, 9, 23), date(2025, 9, 16)]
    ctx = MagicMock()
    ctx.get_available_expiries = MagicMock(return_value=expiries)
    s._context = ctx
    s._front_expiry = date(2025, 9, 9)
    # min_back_days=21 default → 9/30 is 21 days out, others closer
    assert s._find_back_expiry() == date(2025, 9, 30)


def test_long_calendar_min_back_days_override_picks_weekly():
    """Setting min_back_days=7 reproduces v2 behavior (next weekly)."""
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.registry import create_strategy
    s = create_strategy(
        "long_calendar",
        strategy_id="lc_back_weekly",
        params={"min_back_days": 7},
    )
    expiries = [date(2025, 9, 9), date(2025, 9, 16), date(2025, 9, 23), date(2025, 9, 30)]
    ctx = MagicMock()
    ctx.get_available_expiries = MagicMock(return_value=expiries)
    s._context = ctx
    s._front_expiry = date(2025, 9, 9)
    # 9/16 is exactly 7 days out
    assert s._find_back_expiry() == date(2025, 9, 16)


def test_long_calendar_pe_param_override():
    """leg_type='PE' should produce a put calendar."""
    from src.strategy.registry import create_strategy
    from src.backtest.common import import_strategies
    import_strategies()
    s = create_strategy("long_calendar", strategy_id="lc_pe", params={"leg_type": "PE"})
    assert s.params.leg_type == "PE"


def test_long_calendar_expiry_alias_property():
    """_expiry property must alias _front_expiry so base helpers (DTE, expiry block)
    work without per-method overrides."""
    from src.strategy.registry import create_strategy
    from src.backtest.common import import_strategies
    import_strategies()
    s = create_strategy("long_calendar", strategy_id="lc_expiry_alias")
    s._front_expiry = date(2025, 9, 16)
    assert s._expiry == date(2025, 9, 16)
    s._front_expiry = None
    assert s._expiry is None
