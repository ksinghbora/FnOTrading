#!/usr/bin/env python
"""Two-step ablation: IB v2 no-calendar, then IC v2 with calendar.

Isolates which of (IB structure, calendar filter) caused IB v2+cal to
land at -₹1,650 / 40 trips on the 173-day post-SEBI smoke.

Run sequentially because each backtest holds GDFL data in memory and
running two in parallel can OOM.
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


PARQUET_DIR = "data/gdfl_v2"
SMOKE_DAYS = 173
SMOKE_START = date(2024, 11, 20)


async def run_one(strategy: str, params_path: Path, label: str) -> dict:
    with params_path.open() as f:
        params_raw = json.load(f)
    params_raw.pop("_doc", None)

    print(f"\n{'=' * 60}")
    print(f"Running ablation: {label}")
    print(f"  strategy={strategy}, params={params_path.name}")
    print(f"  v2_regime={params_raw.get('require_premium_selling_regime_v2')}")
    print(f"  calendar_filter={params_raw.get('require_calendar_filter')}")
    print('=' * 60)

    source = GDFLMarketSource(PARQUET_DIR, "NIFTY", NIFTY_SPOT_TOKEN)
    engine = BacktestEngine()
    results = await engine.run(
        strategy_name=strategy,
        strategy_params=params_raw,
        num_days=SMOKE_DAYS,
        start_date=SMOKE_START,
        initial_capital=1_000_000,
        market_source=source,
    )
    return results


def fmt(label: str, results: dict) -> str:
    if "error" in results:
        return f"  {label}: ERROR — {results['error']}"
    m = results.get("metrics", {})
    n = int(m.get("num_trades", 0)) // 4
    pnl = float(m.get("total_pnl", 0))
    wr = float(m.get("win_rate", 0))
    sh = float(m.get("sharpe_ratio", 0))
    mean = pnl / max(1, n)
    return f"  {label:<35s} {n:>4d} trips | ₹{pnl:>+10,.0f} | {wr:>5.1f}% WR | Sharpe {sh:>+5.2f} | ₹{mean:>+8,.1f}/trade"


async def main():
    logging.basicConfig(
        level=logging.WARNING,  # Quiet for sequential runs
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    _import_strategies()

    runs = []

    # Ablation 1: IB v2 WITHOUT calendar
    r1 = await run_one(
        "iron_butterfly",
        Path("reports/standalone_post_sebi/ib_v2_no_calendar_params.json"),
        "IB v2 (no calendar)",
    )
    runs.append(("IB v2 (no calendar)         ", r1))

    # Ablation 2: IC v2 WITH calendar
    r2 = await run_one(
        "iron_condor",
        Path("reports/standalone_post_sebi/ic_v2_calendar_params.json"),
        "IC v2 + calendar",
    )
    runs.append(("IC v2 (+ calendar)          ", r2))

    print("\n" + "=" * 80)
    print("ABLATION SUMMARY (173-day post-SEBI corpus)")
    print("=" * 80)
    print("Reference points:")
    print("  IC v2 (no calendar) train+val:  ~280 trips | PF~1.20")
    print("  IC v2 holdout (no calendar):     324 trips | ₹+584     | 47.1% WR | Sharpe +0.35")
    print("  IB v2 + calendar (just-ran):      40 trips | ₹-1,650   | 50.0% WR | Sharpe -0.52")
    print()
    print("Ablations:")
    for label, results in runs:
        print(fmt(label, results))
    print()
    print("Interpretation:")
    print("  * IB v2 (no cal) much better than IB v2 (cal) → calendar filter is the issue")
    print("  * IB v2 (no cal) similar to IB v2 (cal)       → IB gamma is the issue")
    print("  * IC v2 (+ cal) positive AND beats IB v2(cal) → calendar can help, IB structure hurts")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
