"""Time a 10-day portfolio backtest with the P1.5 gate enabled.

Compares against the pre-P1.5 baseline documented in session memory
(46s wall for 10 days). If this number is materially higher, the gate
itself is adding per-tick overhead that explains the 2h+ short-baseline
runtime.
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtest.engine import BacktestEngine
from src.backtest.gdfl_market_source import GDFLMarketSource
from src.market_data.simulator import NIFTY_SPOT_TOKEN


async def _run(days: int, start: date) -> dict:
    engine = BacktestEngine()
    source = GDFLMarketSource(Path("data/gdfl_snapshots"), "NIFTY", NIFTY_SPOT_TOKEN)
    return await engine.run(
        strategy_name="portfolio",
        num_days=days,
        start_date=start,
        initial_capital=1_000_000,
        market_source=source,
    )


def main() -> None:
    days = 10
    start = date(2024, 9, 2)
    t0 = time.time()
    result = asyncio.run(_run(days, start))
    elapsed = time.time() - t0
    print(f"{days} days: {elapsed:.1f}s wall, {elapsed/days:.1f}s/day")
    print(f"P&L={result.get('final_pnl', 0):.0f} trades={result.get('num_trades', 0)}")


if __name__ == "__main__":
    main()
