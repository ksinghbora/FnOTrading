"""cProfile a 2-day portfolio backtest to locate hotspots.

Run:
    uv run python scripts/profile_backtest.py

Writes cumulative top-30 and tottime top-30 tables to stdout; saves the
raw pstats binary to ``reports/validation/profile.pstats`` for later drill-
down with ``snakeviz`` or ``pyprof2calltree`` if needed.
"""
from __future__ import annotations

import asyncio
import cProfile
import io
import pstats
import sys
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
    days = 2
    start = date(2024, 9, 2)

    out_dir = Path("reports/validation")
    out_dir.mkdir(parents=True, exist_ok=True)
    pstats_path = out_dir / "profile.pstats"

    profiler = cProfile.Profile()
    profiler.enable()
    result = asyncio.run(_run(days, start))
    profiler.disable()

    profiler.dump_stats(str(pstats_path))
    print(f"\n=== Backtest result: {days} days, P&L={result.get('final_pnl', 0)} ===\n")

    for sort_key in ("cumulative", "tottime"):
        buf = io.StringIO()
        stats = pstats.Stats(profiler, stream=buf).sort_stats(sort_key)
        stats.print_stats(30)
        print(f"\n=== Top 30 by {sort_key} ===\n")
        print(buf.getvalue())

    print(f"\nRaw pstats saved: {pstats_path}")


if __name__ == "__main__":
    main()
