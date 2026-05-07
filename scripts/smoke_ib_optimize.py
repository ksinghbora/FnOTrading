#!/usr/bin/env python
"""IB v2 + calendar — PT/SL + filter optimization sweep.

Hypothesis: IB v2 + calendar's -₹41/trade can be improved by tuning
profit-target / stop-loss / strike-delta / VIX band.

Six ablation configs, each on the same 173-day post-SEBI corpus.
Sequential runs (parallel OOMs the GDFL parquet load).

Reference points:
  IB v2 + calendar (baseline):  -₹41/trade   (40 trips, 50% WR)
  IC v2 + calendar (target):    +₹13.1/trade (20 trips, 47.5% WR)
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


# Base IB v2 + calendar params (the -₹41/trade baseline)
BASE_IB_V2_CAL = {
    "underlying": "NIFTY",
    "quantity_lots": 1,
    "entry_time": "09:30:00",
    "exit_time": "15:00:00",
    "skip_entry_on_expiry_day": True,
    "expiry_day_force_exit_at": "14:30:00",
    "short_call_delta": 0.5,
    "short_put_delta": -0.5,
    "wing_width_strikes": 2,
    "adjustment_threshold_pct": 60.0,
    "stop_loss_pct": 30.0,
    "profit_target_pct": 25.0,
    "max_spread_pct": 5.0,
    "require_premium_selling_regime_v2": True,
    "require_calendar_filter": True,
    "allowed_days_of_week": [1, 2, 3],
    "block_pre_event_days": 1,
    "block_friday": False,
}


VARIANTS = [
    # PT/SL ablations
    ("A1: tight PT 15% / SL 30%", {**BASE_IB_V2_CAL, "profit_target_pct": 15.0, "stop_loss_pct": 30.0}),
    ("A2: loose PT 35% / SL 50%", {**BASE_IB_V2_CAL, "profit_target_pct": 35.0, "stop_loss_pct": 50.0}),
    ("A3: symmetric 25/25",       {**BASE_IB_V2_CAL, "profit_target_pct": 25.0, "stop_loss_pct": 25.0}),
    # Strike / wing variants
    ("B1: narrow IC (Δ=0.35, w=3)", {**BASE_IB_V2_CAL, "short_call_delta": 0.35, "short_put_delta": -0.35, "wing_width_strikes": 3, "stop_loss_pct": 35.0}),
    ("B2: wide IB (Δ=0.50, w=4)",   {**BASE_IB_V2_CAL, "wing_width_strikes": 4, "stop_loss_pct": 35.0}),
    # IC v2 + calendar with TIGHTENED PT/SL (does the calendar's +₹13 hold up at tighter exits?)
    ("C1: IC v2 + cal + tight 15/30", {
        "underlying": "NIFTY", "quantity_lots": 1,
        "entry_time": "09:30:00", "exit_time": "15:00:00",
        "skip_entry_on_expiry_day": True, "expiry_day_force_exit_at": "14:30:00",
        "short_call_delta": 0.15, "short_put_delta": -0.15, "wing_width_strikes": 8,
        "adjustment_threshold_pct": 60.0,
        "stop_loss_pct": 30.0, "profit_target_pct": 15.0,
        "max_spread_pct": 5.0,
        "require_premium_selling_regime_v2": True,
        "require_calendar_filter": True,
        "allowed_days_of_week": [1, 2, 3],
        "block_pre_event_days": 1,
        "block_friday": False,
    }),
]


async def run_one(strategy: str, label: str, params: dict) -> dict:
    print(f"\n{'=' * 60}\nRunning: {label}\n{'=' * 60}", flush=True)
    source = GDFLMarketSource(PARQUET_DIR, "NIFTY", NIFTY_SPOT_TOKEN)
    engine = BacktestEngine()
    return await engine.run(
        strategy_name=strategy,
        strategy_params=params,
        num_days=SMOKE_DAYS,
        start_date=SMOKE_START,
        initial_capital=1_000_000,
        market_source=source,
    )


def fmt(label: str, results: dict) -> str:
    if "error" in results:
        return f"  {label:<40s} ERROR — {results['error']}"
    m = results.get("metrics", {})
    n = int(m.get("num_trades", 0)) // 4
    pnl = float(m.get("total_pnl", 0))
    wr = float(m.get("win_rate", 0))
    sh = float(m.get("sharpe_ratio", 0))
    mean = pnl / max(1, n)
    return f"  {label:<40s} {n:>4d} trips | ₹{pnl:>+10,.0f} | {wr:>5.1f}% WR | Sharpe {sh:>+5.2f} | ₹{mean:>+8,.1f}/trade"


async def main():
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    _import_strategies()

    runs = []
    for label, params in VARIANTS:
        # Strategy name from params: short_call_delta = 0.5 → IB; else IC
        strategy = "iron_butterfly" if params["short_call_delta"] >= 0.4 else "iron_condor"
        result = await run_one(strategy, label, params)
        runs.append((label, result))

    print("\n" + "=" * 90)
    print("OPTIMIZATION SWEEP — IB / IC variants on 173-day post-SEBI corpus")
    print("=" * 90)
    print("Reference points:")
    print("  IC v2 (no cal) train+val:        ~280 trips | PF ~1.20")
    print("  IC v2 (no cal) holdout:           324 trips | ₹+584     | 47.1% WR | Sharpe +0.35  | ₹+1.8/trade")
    print("  IC v2 + calendar (baseline):       20 trips | ₹+263     | 47.5% WR | Sharpe +0.26  | ₹+13.1/trade")
    print("  IB v2 + calendar (baseline):       40 trips | ₹-1,650   | 50.0% WR | Sharpe -0.52  | ₹-41.3/trade")
    print()
    print("Variants (this run):")
    for label, result in runs:
        print(fmt(label, result))
    print()
    print("Decision rule: any variant with > +₹15/trade AND >= 15 trips is promising.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
