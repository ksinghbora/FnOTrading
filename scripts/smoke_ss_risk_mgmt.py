#!/usr/bin/env python
"""SS Phase 1.5 — Risk management ablation.

User insight: SS Phase 1 had 59.4% win rate but -₹52/trade. Risk
management (not regime selection) is the binding constraint. Test
4 exit-policy variants holding the v2+calendar regime gate constant.

Decomposed math:
  Current (PT=15, SL=30, trail=15): 0.60 × 450 − 0.40 × 900 = -₹90/trade ✓
  V1 (PT=25, SL=30, no trail):       0.60 × 750 − 0.40 × 900 = +₹90/trade ← projection
  V2 (PT=15, SL=20, keep trail):     0.60 × 450 − 0.40 × 600 = +₹30/trade
  V3 (PT=25, SL=20, no trail):       0.60 × 750 − 0.40 × 600 = +₹210/trade ← best projection
  V4 (PT=50 tasty, SL=30, no trail): 0.60 × 1500 − 0.40 × 900 = +₹540/trade
                                     but win rate drops with longer holds

Decision rule (per smoke result):
  Best variant per-trade EV > +₹8 → deploy as ss_1 shadow
  Best variant ≥ +₹0 → marginal, more variants worth testing
  All negative → SS structurally impossible to fix; retire shadow
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


# Base SS Phase 1 config (v2+cal, 0.15Δ, 5-strike hedge)
BASE = {
    "underlying": "NIFTY",
    "quantity_lots": 1,
    "entry_time": "09:30:00",
    "exit_time": "15:00:00",
    "skip_entry_on_expiry_day": True,
    "expiry_day_force_exit_at": "14:30:00",
    "call_delta": 0.15,
    "put_delta": -0.15,
    "add_hedge": True,
    "hedge_offset_strikes": 5,
    "vix_entry_min": 13.0,
    "vix_entry_max": 16.0,
    "max_spread_pct": 5.0,
    "require_premium_selling_regime_v2": True,
    "require_calendar_filter": True,
    "allowed_days_of_week": [1, 2, 3],
    "block_pre_event_days": 1,
    "block_friday": False,
}


VARIANTS = [
    # (label, override-dict)
    ("Phase 1 baseline (PT=15, SL=30, trail=15)",
     {"profit_target_pct": 15.0, "stop_loss_pct": 30.0, "trail_stop_pct": 15.0}),
    ("V1: PT=25, SL=30, no trail",
     {"profit_target_pct": 25.0, "stop_loss_pct": 30.0, "trail_stop_pct": 0.0}),
    ("V2: PT=15, SL=20, keep trail",
     {"profit_target_pct": 15.0, "stop_loss_pct": 20.0, "trail_stop_pct": 15.0}),
    ("V3: PT=25, SL=20, no trail (asymmetric)",
     {"profit_target_pct": 25.0, "stop_loss_pct": 20.0, "trail_stop_pct": 0.0}),
    ("V4: PT=50, SL=30, no trail (tasty-canonical)",
     {"profit_target_pct": 50.0, "stop_loss_pct": 30.0, "trail_stop_pct": 0.0}),
]


async def run_one(label: str, override: dict) -> dict:
    params = {**BASE, **override}
    print(f"\n{'=' * 60}\nRunning: {label}\n  PT={params['profit_target_pct']} SL={params['stop_loss_pct']} trail={params['trail_stop_pct']}\n{'=' * 60}", flush=True)
    source = GDFLMarketSource(PARQUET_DIR, "NIFTY", NIFTY_SPOT_TOKEN)
    engine = BacktestEngine()
    return await engine.run(
        strategy_name="short_strangle",
        strategy_params=params,
        num_days=SMOKE_DAYS,
        start_date=SMOKE_START,
        initial_capital=1_000_000,
        market_source=source,
    )


def fmt(label: str, results: dict) -> str:
    if "error" in results:
        return f"  {label:<50s} ERROR — {results['error']}"
    m = results.get("metrics", {})
    n = int(m.get("num_trades", 0)) // 4
    pnl = float(m.get("total_pnl", 0))
    wr = float(m.get("win_rate", 0))
    sh = float(m.get("sharpe_ratio", 0))
    mean = pnl / max(1, n)
    return f"  {label:<50s} {n:>4d} trips | ₹{pnl:>+10,.0f} | {wr:>5.1f}% WR | Sharpe {sh:>+5.2f} | ₹{mean:>+8,.1f}/trade"


async def main():
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    _import_strategies()

    runs = []
    for label, override in VARIANTS:
        result = await run_one(label, override)
        runs.append((label, result))

    print("\n" + "=" * 100)
    print("SS PHASE 1.5 — Risk management ablation (173-day post-SEBI corpus)")
    print("=" * 100)
    print("Reference points:")
    print("  IC v2 + cal:     20 trips | ₹+263    | 47.5% WR | Sharpe +0.26 | +₹13.1/trade (target)")
    print("  IB B2 + cal:     38 trips | ₹+222    | 48.7% WR | Sharpe +0.06 | +₹5.8/trade")
    print()
    print("Variants (this run):")
    for label, r in runs:
        print(fmt(label, r))
    print()
    print("Decision rule: best variant > +₹8/trade → deploy ss_1 shadow")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
