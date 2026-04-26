"""Confirm the cross-day candle persistence bug.

The OHLCAggregator's ``_completed_candles`` list is shared across the
entire engine run. The engine does ``aggregator._builders.clear()`` at
day start (intraday in-progress builders) but never clears
``_completed_candles``.

Result: until enough today-only candles have closed, callers like
``BaseStrategy._move_from_open_pct`` and ``momentum_breakout`` that take
``candles[:N]`` (the OLDEST of the slice) operate on YESTERDAY'S late
candles instead of today's morning.

This script feeds two days of synthetic ticks to an OHLCAggregator and
prints what ``get_completed_candles(M15, limit=3)`` returns at 9:31 IST
on day 2 — proving day 1's afternoon candles leak in.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.events import EventBus
from src.core.models import Tick
from src.core.types import Timeframe
from src.market_data.aggregator import OHLCAggregator


def _tick(ts: datetime, ltp: float, token: int = 256265) -> Tick:
    return Tick(
        instrument_token=token,
        tradingsymbol="NIFTY",
        last_price=ltp,
        ltp=ltp,
        timestamp=ts,
    )


def main() -> None:
    bus = EventBus()
    agg = OHLCAggregator(bus, timeframes=[Timeframe.M15])

    # Day 1: feed ticks 9:15 to 15:30 (each minute with a varying LTP).
    day1 = datetime(2024, 9, 2, 9, 15)
    for i in range(0, 376):
        ts = day1 + timedelta(minutes=i)
        ltp = 23000.0 + (i % 50) * 2.0  # arbitrary varying price
        agg.process_tick_direct(_tick(ts, ltp))
    # Force the last day-1 candle to close by sending a 15:30:01 tick.
    agg.process_tick_direct(_tick(day1 + timedelta(hours=6, minutes=15, seconds=1), 23100.0))

    print(f"After day 1: {len(agg._completed_candles)} M15 candles in buffer")

    # Engine's day-boundary reset (mirroring BacktestEngine line 395).
    agg._builders.clear()

    # Day 2: feed only the 9:15-9:30 candle.
    day2 = datetime(2024, 9, 3, 9, 15)
    # Today's open: 23200
    for i in range(0, 16):  # 9:15:00 through 9:30:00
        ts = day2 + timedelta(minutes=i)
        ltp = 23200.0 + i * 0.5
        agg.process_tick_direct(_tick(ts, ltp))
    # Force the 9:15-9:30 candle to close.
    agg.process_tick_direct(_tick(day2 + timedelta(minutes=15, seconds=1), 23210.0))

    print(f"After day 2 09:31: {len(agg._completed_candles)} M15 candles in buffer")
    print()

    candles = agg.get_completed_candles(
        instrument_token=256265, timeframe=Timeframe.M15, limit=3,
    )
    print(f"get_completed_candles(M15, limit=3) at 9:31 IST day 2 returns {len(candles)} candles:")
    for i, c in enumerate(candles):
        date_str = c.timestamp.date().isoformat()
        print(f"  [{i}] date={date_str}  start={c.timestamp.time()}  open={c.open}  close={c.close}")

    print()
    today = day2.date()
    today_count = sum(1 for c in candles if c.timestamp.date() == today)
    yesterday_count = sum(1 for c in candles if c.timestamp.date() != today)
    print(f"  → {today_count} today, {yesterday_count} from yesterday")
    print(f"  → candles[0].open = {float(candles[0].open):.1f}")
    print(f"  → today's actual session open = 23200.0")
    if float(candles[0].open) != 23200.0:
        print()
        print("  *** BUG CONFIRMED: candles[0] is NOT today's session open ***")
        print(f"  *** Off by: {float(candles[0].open) - 23200.0:+.1f} pts ***")


if __name__ == "__main__":
    main()
