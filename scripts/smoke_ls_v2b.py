#!/usr/bin/env python
"""Smoke test for LongStraddle v2b (pure VRP<0 gate, no CI requirement).

Built post LC v2 / LC v2b verdict to test whether the calendar
STRUCTURE was the binding constraint. Long straddle profits from
ANY directional movement, opposite of LC's "spot-near-strike"
requirement. Same gate, different vehicle.

If LS v2b shows materially better PnL than LC v2b, the gate was
fine and the structure was the problem.
If LS v2b also fails, the long-vol thesis on Indian post-SEBI
options is structurally untradable.

Usage:
    uv run python scripts/smoke_ls_v2b.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtest.engine import BacktestEngine, _import_strategies
from src.backtest.gdfl_market_source import GDFLMarketSource
from src.market_data.simulator import NIFTY_SPOT_TOKEN


PARAMS_PATH = Path("reports/standalone_post_sebi/ls_v2b_research_params.json")
PARQUET_DIR = "data/gdfl_v2"
SMOKE_DAYS = 173
SMOKE_START = date(2024, 11, 20)


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("src.portfolio.positions", "src.portfolio.pnl", "src.broker.paper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger = logging.getLogger("smoke_ls_v2b")

    if not PARAMS_PATH.exists():
        logger.error(f"params file not found: {PARAMS_PATH}")
        return 2

    with PARAMS_PATH.open() as f:
        params_raw = json.load(f)
    params_raw.pop("_doc", None)

    logger.info(f"LS v2b smoke: params={PARAMS_PATH.name}, days={SMOKE_DAYS}, from={SMOKE_START}")
    logger.info(f"  require_long_vol_regime_v2b = {params_raw.get('require_long_vol_regime_v2b')}")

    _import_strategies()

    source = GDFLMarketSource(PARQUET_DIR, "NIFTY", NIFTY_SPOT_TOKEN)
    avail = source.available_days()
    if not avail:
        logger.error(f"No GDFL parquet in {PARQUET_DIR}")
        return 2
    logger.info(f"GDFL corpus: {len(avail)} days, range {avail[0]} → {avail[-1]}")

    engine = BacktestEngine()
    results = await engine.run(
        strategy_name="long_straddle",
        strategy_params=params_raw,
        num_days=SMOKE_DAYS,
        start_date=SMOKE_START,
        initial_capital=1_000_000,
        market_source=source,
    )

    if "error" in results:
        logger.error(f"Backtest error: {results['error']}")
        return 2

    m = results.get("metrics", {})
    n_trades = int(m.get("num_trades", 0))
    pnl = float(m.get("total_pnl", 0.0))
    win_rate = float(m.get("win_rate", 0.0))
    period = results.get("period", "n/a")

    print()
    print("=" * 60)
    print(f"LS v2b smoke result — {period}")
    print("=" * 60)
    print(f"  Days backtested: {results.get('num_days', 0)}")
    print(f"  Trade fills:     {n_trades}")
    print(f"  Total P&L:       Rs {pnl:>12,.2f}")
    print(f"  Win Rate:        {win_rate:>11.1f}%")
    print(f"  Sharpe:          {m.get('sharpe_ratio', 0):>11.2f}")
    print(f"  Max DD:          Rs {m.get('max_drawdown', 0):>12,.2f}")
    print()

    # n_trades counts trade fills (entry+exit per leg = 4 fills per round trip).
    round_trips = n_trades // 4
    print(f"  ≈ {round_trips} round trips ({n_trades} fills)")
    print()

    if round_trips == 0:
        print("VERDICT: ❌ 0 round trips — gate dead even with LS structure")
        return 1
    elif round_trips < 30:
        print(f"VERDICT: ⚠️  {round_trips} round trips — sparse, sample-thin")
        return 0
    else:
        print(f"VERDICT: ✅ {round_trips} round trips — substantive sample")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
