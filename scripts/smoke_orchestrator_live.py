#!/usr/bin/env python
"""Backtest the LIVE orchestrator deployment on the 173-day post-SEBI corpus.

Mirrors the production .env STRATEGIES[orchestrator_1] config:
  - 4 children: iron_condor, iron_butterfly, short_strangle, trend_daily
  - V4 orchestrator (V5 flags off — first LIVE run)
  - Indian-validated regime gate (CI≥61.8 AND VRP>0) + calendar filter
    (Tue/Wed/Thu, T-1 pre-event block) on premium-selling children

Reference benchmarks (173-day post-SEBI standalone smokes):
  IC v2 + cal:           20 trips | ₹+263  | 47.5% WR | Sharpe +0.26 | +₹13.1/trade
  IB B2 + cal:           38 trips | ₹+222  | 48.7% WR | Sharpe +0.06 | +₹5.8/trade
  SS Phase 1.5 V1:       18 trips | ₹+788  | 69.4% WR | Sharpe +0.57 | +₹43.8/trade
  TrendDaily (default):  N/A — multi-day; benchmarked separately

The orchestrator picks the highest-scoring child each tick. Single-slot
routing means at most one child is active at any moment. The combined
PnL is the sum of all children's realized trades.

Decision rule:
  Aggregate per-trade EV > +₹10 → orchestrator beats IB B2 baseline → keep LIVE
  +₹0 to +₹10 → marginal; investigate which children fired vs. didn't
  Negative → roll back; orchestrator misroutes vs. standalone shadows

Usage:
    uv run python scripts/smoke_orchestrator_live.py
"""

from __future__ import annotations

import asyncio
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


# Mirrors .env STRATEGIES[orchestrator_1] verbatim
ORCHESTRATOR_CONFIG = {
    "underlying": "NIFTY",
    "quantity_lots": 1,
    "shadow_only": False,
    "min_score_to_trade": 60,
    "children": [
        "iron_condor",
        "iron_butterfly",
        "short_strangle",
        "trend_daily",
    ],
    "children_params": {
        "iron_condor": {
            "vix_entry_min": 16.0,
            "vix_entry_max": 22.0,
            "vix_reduce_above": 20.0,
            "short_call_delta": 0.15,
            "short_put_delta": -0.15,
            "wing_width_strikes": 8,
            "adjustment_threshold_pct": 60.0,
            "stop_loss_pct": 40.0,
            "profit_target_pct": 25.0,
            "max_spread_pct": 5.0,
            "entry_time": "09:30:00",
            "exit_time": "15:00:00",
            "skip_entry_on_expiry_day": True,
            "expiry_day_force_exit_at": "14:30:00",
            "require_premium_selling_regime_v2": True,
            "require_calendar_filter": True,
            "allowed_days_of_week": [1, 2, 3],
            "block_pre_event_days": 1,
        },
        "iron_butterfly": {
            "short_call_delta": 0.5,
            "short_put_delta": -0.5,
            "wing_width_strikes": 4,
            "adjustment_threshold_pct": 60.0,
            "stop_loss_pct": 35.0,
            "profit_target_pct": 25.0,
            "max_spread_pct": 5.0,
            "entry_time": "09:30:00",
            "exit_time": "15:00:00",
            "skip_entry_on_expiry_day": True,
            "expiry_day_force_exit_at": "14:30:00",
            "require_premium_selling_regime_v2": True,
            "require_calendar_filter": True,
            "allowed_days_of_week": [1, 2, 3],
            "block_pre_event_days": 1,
        },
        "short_strangle": {
            # PT/SL/trail come from V1 winner defaults in calibration module
            "call_delta": 0.15,
            "put_delta": -0.15,
            "add_hedge": True,
            "hedge_offset_strikes": 5,
            "vix_entry_min": 13.0,
            "vix_entry_max": 16.0,
            "max_spread_pct": 5.0,
            "entry_time": "09:30:00",
            "exit_time": "15:00:00",
            "skip_entry_on_expiry_day": True,
            "expiry_day_force_exit_at": "14:30:00",
            "require_premium_selling_regime_v2": True,
            "require_calendar_filter": True,
            "allowed_days_of_week": [1, 2, 3],
            "block_pre_event_days": 1,
        },
        "trend_daily": {
            "donchian_lookback": 20,
            "atr_period": 14,
            "atr_floor_pct": 0.5,
            "atr_stop_mult": 2.0,
            "vix_entry_min": 12.0,
            "vix_entry_max": 22.0,
            "max_hold_days": 30,
        },
    },
}


def fmt_metrics(label: str, results: dict) -> str:
    if "error" in results:
        return f"  {label:<40s} ERROR — {results['error']}"
    m = results.get("metrics", {})
    n_fills = int(m.get("num_trades", 0))
    n_trips = n_fills // 4  # IC has 4 legs per round-trip; rough estimate
    pnl = float(m.get("total_pnl", 0))
    wr = float(m.get("win_rate", 0))
    sh = float(m.get("sharpe_ratio", 0))
    mean = pnl / max(1, n_trips)
    return (
        f"  {label:<40s} {n_fills:>4d} fills | ~{n_trips:>3d} trips | "
        f"₹{pnl:>+10,.0f} | {wr:>5.1f}% WR | "
        f"Sharpe {sh:>+5.2f} | ₹{mean:>+8,.1f}/trip"
    )


async def main():
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )
    _import_strategies()

    print("=" * 100, flush=True)
    print("ORCHESTRATOR LIVE BACKTEST — 173-day post-SEBI corpus (2024-11-20 → 2025-08-08)")
    print("=" * 100, flush=True)
    print()
    print("Children (single-slot routing, V4 mode):")
    print("  iron_condor      vix [16,22]  v2+cal  Δ=0.15  wing=8  PT=25 SL=40")
    print("  iron_butterfly   ATM body     v2+cal  Δ=0.5   wing=4  PT=25 SL=35")
    print("  short_strangle   vix [13,16]  v2+cal  Δ=0.15  hedge=5 PT=25 SL=30 (V1, no trail)")
    print("  trend_daily      Donchian-20  ATR     vix [12,22]  max-hold 30d")
    print(flush=True)
    print("Reference benchmarks (standalone, same corpus):", flush=True)
    print("  IC v2 + cal:           20 trips | ₹+263  | 47.5% WR | Sharpe +0.26 | +₹13.1/trade")
    print("  IB B2 + cal:           38 trips | ₹+222  | 48.7% WR | Sharpe +0.06 | +₹5.8/trade")
    print("  SS Phase 1.5 V1:       18 trips | ₹+788  | 69.4% WR | Sharpe +0.57 | +₹43.8/trade")
    print(flush=True)

    source = GDFLMarketSource(PARQUET_DIR, "NIFTY", NIFTY_SPOT_TOKEN)
    engine = BacktestEngine()
    result = await engine.run(
        strategy_name="orchestrator",
        strategy_id="orchestrator_bt",
        strategy_params=ORCHESTRATOR_CONFIG,
        num_days=SMOKE_DAYS,
        start_date=SMOKE_START,
        initial_capital=1_000_000,
        market_source=source,
    )

    print("=" * 100, flush=True)
    print("ORCHESTRATOR RESULTS")
    print("=" * 100, flush=True)
    print(fmt_metrics("orchestrator (4 children, V4 single-slot)", result))
    print()

    if "error" not in result:
        m = result.get("metrics", {})
        n_fills = int(m.get("num_trades", 0))
        pnl = float(m.get("total_pnl", 0))
        # Report per-trip EV at multiple trip-count assumptions
        for trip_size, name in [(2, "2-leg trips (strangle/straddle)"),
                                 (4, "4-leg trips (IC/IB)")]:
            trips = n_fills // trip_size
            if trips > 0:
                mean = pnl / trips
                print(f"  Per-trip EV @{trip_size}-leg ({name}): ~{trips} trips × ₹{mean:+,.1f}/trip")
        print()
        # Decision
        # Use 4-leg trips as conservative estimate (worst case for per-trip EV)
        trips_4 = max(1, n_fills // 4)
        ev_4 = pnl / trips_4
        if ev_4 > 10:
            print(f"VERDICT: ✅ Orchestrator EV ₹{ev_4:+.1f}/trip > +₹10 — beats IB B2 baseline")
        elif ev_4 > 0:
            print(f"VERDICT: ⚠️  Orchestrator EV ₹{ev_4:+.1f}/trip marginal (0 < EV < 10)")
        else:
            print(f"VERDICT: ❌ Orchestrator EV ₹{ev_4:+.1f}/trip negative — investigate routing")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
