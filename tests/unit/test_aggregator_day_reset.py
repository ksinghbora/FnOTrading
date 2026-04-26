"""Tests for ``OHLCAggregator.clear_day()`` — per-trading-day reset contract.

The Apr 25 2026 audit found that the aggregator's
``_completed_candles`` list persisted across trading-day boundaries in
backtests. The engine cleared in-progress builders at day start but
left the completed-candle history untouched, so callers like
``BaseStrategy._move_from_open_pct`` and ``momentum_breakout`` that take
``candles[:N]`` (oldest of a limit-N slice) silently mixed yesterday's
late-afternoon candles into today's morning window.

This file locks in the contract:

1. ``clear_day()`` wipes both ``_builders`` and ``_completed_candles``.
2. After ``clear_day()``, ``get_completed_candles(limit=N)`` returns
   only candles produced AFTER the reset.
3. ``candles[0]`` after reset is today's first closed candle, not
   yesterday's late candle.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.core.events import EventBus
from src.core.models import Tick
from src.core.types import Timeframe
from src.market_data.aggregator import OHLCAggregator


SPOT_TOKEN = 256265  # NIFTY 50


def _tick(ts: datetime, ltp: float, token: int = SPOT_TOKEN) -> Tick:
    return Tick(
        instrument_token=token,
        tradingsymbol="NIFTY",
        last_price=ltp,
        ltp=ltp,
        timestamp=ts,
    )


def _feed_full_day(agg: OHLCAggregator, day_open: datetime, base_ltp: float) -> int:
    """Feed a full 9:15-15:30 IST trading day's worth of M15 ticks.

    Returns the number of M15 candles closed for the spot token.
    """
    pre = len(
        [c for c in agg._completed_candles
         if c.instrument_token == SPOT_TOKEN and c.timeframe == Timeframe.M15]
    )
    # 6h15m = 375 minutes; one tick per minute is enough for builder closure.
    for i in range(0, 376):
        ts = day_open + timedelta(minutes=i)
        agg.process_tick_direct(_tick(ts, base_ltp + (i % 50) * 2.0))
    # Trip the last bar's close.
    agg.process_tick_direct(
        _tick(day_open + timedelta(hours=6, minutes=15, seconds=1), base_ltp + 100.0)
    )
    post = len(
        [c for c in agg._completed_candles
         if c.instrument_token == SPOT_TOKEN and c.timeframe == Timeframe.M15]
    )
    return post - pre


def test_clear_day_wipes_both_builders_and_completed_candles():
    bus = EventBus()
    agg = OHLCAggregator(bus, timeframes=[Timeframe.M15])
    day1 = datetime(2024, 9, 2, 9, 15)
    closed = _feed_full_day(agg, day1, 23000.0)
    assert closed > 0, "fixture failed to produce candles for day 1"
    assert len(agg._builders) > 0
    assert len(agg._completed_candles) > 0

    agg.clear_day()

    assert agg._builders == {}
    assert agg._completed_candles == []


def test_after_clear_day_first_candle_is_todays_open():
    """The canonical regression test for the cross-day candle leak.

    Before the fix, ``get_completed_candles(M15, limit=3)`` at 9:31 IST
    on day 2 returned [day1 15:00, day1 15:15, day2 9:15] — and
    ``candles[0].open`` was day 1's afternoon open, not day 2's session
    open. After ``clear_day()`` is called at the day boundary, the only
    candle in the buffer is day 2's freshly-closed 9:15 bar.
    """
    bus = EventBus()
    agg = OHLCAggregator(bus, timeframes=[Timeframe.M15])

    day1 = datetime(2024, 9, 2, 9, 15)
    _feed_full_day(agg, day1, 23000.0)

    # Engine's day-boundary reset.
    agg.clear_day()

    day2 = datetime(2024, 9, 3, 9, 15)
    today_open_price = 23200.0
    for i in range(0, 16):  # 9:15 → 9:30
        ts = day2 + timedelta(minutes=i)
        agg.process_tick_direct(_tick(ts, today_open_price + i * 0.5))
    # Trip the 9:15-9:30 close.
    agg.process_tick_direct(_tick(day2 + timedelta(minutes=15, seconds=1), 23210.0))

    candles = agg.get_completed_candles(SPOT_TOKEN, Timeframe.M15, limit=3)
    assert len(candles) == 1, (
        f"expected exactly 1 today-only M15 candle, got {len(candles)} — "
        f"day-boundary reset failed to wipe yesterday's history"
    )
    assert float(candles[0].open) == pytest.approx(today_open_price, abs=0.5)
    assert candles[0].timestamp.date() == day2.date()


def test_clear_day_idempotent():
    """Calling ``clear_day()`` on an already-empty aggregator must not raise."""
    bus = EventBus()
    agg = OHLCAggregator(bus, timeframes=[Timeframe.M15])
    agg.clear_day()  # no-op
    agg.clear_day()  # still no-op


def test_clear_day_followed_by_full_day_returns_only_today():
    """End-to-end: after clear_day at the boundary, a full day-2 replay
    produces exactly the day-2 candles in the buffer — no day-1 leak.
    """
    bus = EventBus()
    agg = OHLCAggregator(bus, timeframes=[Timeframe.M15])

    day1 = datetime(2024, 9, 2, 9, 15)
    _feed_full_day(agg, day1, 23000.0)

    agg.clear_day()

    day2 = datetime(2024, 9, 3, 9, 15)
    closed_today = _feed_full_day(agg, day2, 23200.0)
    assert closed_today > 0

    candles = agg.get_completed_candles(SPOT_TOKEN, Timeframe.M15, limit=999)
    # Every candle should be from day 2.
    assert all(c.timestamp.date() == day2.date() for c in candles), (
        f"day-1 candles leaked through clear_day: dates="
        f"{sorted({c.timestamp.date() for c in candles})}"
    )
