#!/usr/bin/env python
"""Smoke test for LongCalendar v2 (CI>=61.8 AND VRP<0) gate.

Mirrors the IC v2 smoke test pattern: a fast 30-day backtest on the
post-SEBI GDFL corpus to confirm the gate FIRES on real data with
non-zero entries, before committing to a full WF+holdout validation.

The critical question: do CI ≥ 61.8 AND VRP < 0 ever co-occur on Indian
post-SEBI data? If 0/N samples pass (analogous to the v1 principled
ADX+BB+RV/IV gate's failure), we know the gate is empirically dead and
need to redesign before more work. If even 5-10 entries fire, the gate
is alive and we proceed to formal validation.

Usage:
    uv run python scripts/smoke_lc_v2.py
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


PARAMS_PATH = Path("reports/standalone_post_sebi/lc_v2_research_params.json")
PARQUET_DIR = "data/gdfl_v2"
SMOKE_DAYS = 173  # full post-SEBI train+val window
SMOKE_START = date(2024, 11, 20)  # post-SEBI break


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("src.portfolio.positions", "src.portfolio.pnl", "src.broker.paper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger = logging.getLogger("smoke_lc_v2")

    if not PARAMS_PATH.exists():
        logger.error(f"params file not found: {PARAMS_PATH}")
        return 2

    with PARAMS_PATH.open() as f:
        params_raw = json.load(f)
    params_raw.pop("_doc", None)

    logger.info(f"LC v2 smoke: params={PARAMS_PATH.name}, days={SMOKE_DAYS}, from={SMOKE_START}")
    logger.info(f"  require_long_vol_regime_v2 = {params_raw.get('require_long_vol_regime_v2')}")

    _import_strategies()

    source = GDFLMarketSource(PARQUET_DIR, "NIFTY", NIFTY_SPOT_TOKEN)
    avail = source.available_days()
    if not avail:
        logger.error(f"No GDFL parquet in {PARQUET_DIR}")
        return 2
    logger.info(f"GDFL corpus: {len(avail)} days, range {avail[0]} → {avail[-1]}")

    engine = BacktestEngine()
    results = await engine.run(
        strategy_name="long_calendar",
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
    print(f"LC v2 smoke result — {period}")
    print("=" * 60)
    print(f"  Days backtested: {results.get('num_days', 0)}")
    print(f"  Trades:          {n_trades}")
    print(f"  Total P&L:       Rs {pnl:>12,.2f}")
    print(f"  Win Rate:        {win_rate:>11.1f}%")
    print(f"  Sharpe:          {m.get('sharpe_ratio', 0):>11.2f}")
    print(f"  Max DD:          Rs {m.get('max_drawdown', 0):>12,.2f}")
    print()

    if n_trades == 0:
        print("VERDICT: ❌ 0 entries — gate empirically dead on this window.")
        print("         Same failure mode as the v1 principled ADX+BB+RV/IV")
        print("         AND-gate (0/2590 samples). Investigate: is VRP < 0")
        print("         and CI >= 61.8 ever simultaneous on Indian post-SEBI?")
        return 1
    elif n_trades < 3:
        print("VERDICT: ⚠️  Very thin gate — only a few entries in {} days.".format(SMOKE_DAYS))
        print("         Sample-size concern. Run a longer window before formal validation.")
        return 0
    else:
        print(f"VERDICT: ✅ Gate fires — {n_trades} entries in {SMOKE_DAYS} days.")
        print("         Ready for formal WF+holdout validation.")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
