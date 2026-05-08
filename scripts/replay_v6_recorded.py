#!/usr/bin/env python
"""V6 multi-slot orchestrator replay against the LIVE recorded chain (28 days).

Cross-validates V6_RAW backtest results against real recorded option
prices instead of GDFL historical data:

  GDFL backtest (173d):  116 fills | +₹2,422 | Sharpe +0.97 | +₹83.5/trip
  This replay (~28d):    [pending — fewer days, lower statistical power]

The two data sources are independent:
  - GDFL: tick-level historical from broker provider
  - Recorded: minute-snapshot chain captured by chain_recorder.py

If V6_RAW shows positive PnL on this independent set with reasonable
per-trip EV (>+₹0), that's confidence the deployed config holds in
the recent live regime. If it diverges materially, dig in.

Sample size caveat: 28 trading days, calendar filter limits to ~12
Tue/Wed/Thu sessions, expect ~3-8 round trips. Single confirming run,
not a statistical claim.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Disable PAPER_TRADING shadow-mode override (replay measures real
# strategy gating, not the data-collection-mode override).
os.environ["PAPER_TRADING"] = "false"

from src.backtest.replay_engine import ReplayBacktestEngine  # noqa: E402

# Mirror .env STRATEGIES[orchestrator_1] V6_RAW config exactly
V6_CONFIG = {
    "underlying": "NIFTY",
    "quantity_lots": 1,
    "shadow_only": False,
    "min_score_to_trade": 60,
    "max_concurrent_slots": 4,
    "children": ["iron_condor", "iron_butterfly", "short_strangle", "trend_daily"],
    "children_params": {
        "iron_condor": {
            "vix_entry_min": 16.0, "vix_entry_max": 22.0, "vix_reduce_above": 20.0,
            "short_call_delta": 0.15, "short_put_delta": -0.15,
            "wing_width_strikes": 8, "adjustment_threshold_pct": 60.0,
            "stop_loss_pct": 40.0, "profit_target_pct": 25.0,
            "max_spread_pct": 5.0,
            "entry_time": "09:30:00", "exit_time": "15:00:00",
            "skip_entry_on_expiry_day": True,
            "expiry_day_force_exit_at": "14:30:00",
            "require_premium_selling_regime_v2": True,
            "require_calendar_filter": True,
            "allowed_days_of_week": [1, 2, 3],
            "block_pre_event_days": 1,
        },
        "iron_butterfly": {
            "short_call_delta": 0.5, "short_put_delta": -0.5,
            "wing_width_strikes": 4, "adjustment_threshold_pct": 60.0,
            "stop_loss_pct": 35.0, "profit_target_pct": 25.0,
            "max_spread_pct": 5.0,
            "entry_time": "09:30:00", "exit_time": "15:00:00",
            "skip_entry_on_expiry_day": True,
            "expiry_day_force_exit_at": "14:30:00",
            "require_premium_selling_regime_v2": True,
            "require_calendar_filter": True,
            "allowed_days_of_week": [1, 2, 3],
            "block_pre_event_days": 1,
        },
        "short_strangle": {
            "call_delta": 0.15, "put_delta": -0.15,
            "add_hedge": True, "hedge_offset_strikes": 5,
            "vix_entry_min": 13.0, "vix_entry_max": 16.0,
            "max_spread_pct": 5.0,
            "entry_time": "09:30:00", "exit_time": "15:00:00",
            "skip_entry_on_expiry_day": True,
            "expiry_day_force_exit_at": "14:30:00",
            "require_premium_selling_regime_v2": True,
            "require_calendar_filter": True,
            "allowed_days_of_week": [1, 2, 3],
            "block_pre_event_days": 1,
        },
        "trend_daily": {
            "donchian_lookback": 20, "atr_period": 14,
            "atr_floor_pct": 0.5, "atr_stop_mult": 2.0,
            "vix_entry_min": 12.0, "vix_entry_max": 22.0,
            "max_hold_days": 30,
        },
    },
}


async def main() -> int:
    print("=" * 100)
    print("V6 MULTI-SLOT REPLAY — recorded chain (Mar 25 — May 8 2026, 28 trading days)")
    print("=" * 100)
    print()
    print("Reference (GDFL 173-day backtest):")
    print("  V6_RAW: 116 fills | +₹2,422 | 53.4% WR | Sharpe +0.97 | +₹83.5/trip")
    print()

    engine = ReplayBacktestEngine()
    result = await engine.run(
        strategy_name="orchestrator",
        strategy_id="v6_replay",
        strategy_params=V6_CONFIG,
        snapshot_dir="data/chain_snapshots",
        spot_csv="data/nifty_spot_minute_chain.csv",
        vix_csv="data/india_vix_minute_chain.csv",
        initial_capital=1_000_000,
    )

    if "error" in result:
        print(f"ERROR: {result['error']}")
        return 1

    m = result.get("metrics", {})
    fills = int(m.get("num_trades", 0))
    pnl = float(m.get("total_pnl", 0))
    wr = float(m.get("win_rate", 0))
    sh = float(m.get("sharpe_ratio", 0))
    trips_4 = max(1, fills // 4)
    ev_4 = pnl / trips_4

    print()
    print("=" * 100)
    print("V6 REPLAY RESULT")
    print("=" * 100)
    print(f"  Period:          {result.get('period', 'n/a')}")
    print(f"  Fills:           {fills}")
    print(f"  Round trips (4-leg basis):  ~{trips_4}")
    print(f"  Total PnL:       ₹{pnl:+,.2f}")
    print(f"  Win rate:        {wr:.1f}%")
    print(f"  Sharpe:          {sh:+.2f}")
    print(f"  EV/trip:         ₹{ev_4:+,.1f}")
    print(f"  Total charges:   ₹{float(m.get('total_charges', 0)):,.2f}")
    print()

    # Per-child attribution from broker._trades
    trades = result.get("trades", [])
    by_child = {}
    for t in trades:
        sid = t.get("strategy_id", "?")
        child = sid.split("/", 1)[1] if "/" in sid else sid
        rec = by_child.setdefault(child, {"fills": 0, "buy": 0.0, "sell": 0.0})
        rec["fills"] += 1
        avg = float(t.get("average_price", 0))
        qty = int(t.get("quantity", 0))
        side = str(t.get("transaction_type", "")).upper()
        if side == "SELL":
            rec["sell"] += avg * qty
        else:
            rec["buy"] += avg * qty
    if by_child:
        print("Per-child breakdown:")
        for child in ("iron_condor", "iron_butterfly", "short_strangle", "trend_daily"):
            r = by_child.get(child, {"fills": 0, "buy": 0.0, "sell": 0.0})
            net = r["sell"] - r["buy"]
            print(f"  {child:<22s} {r['fills']:>4d} fills  net=₹{net:>+10,.0f}")
    print()

    # Chain quality
    if "chain_quality" in result:
        clean = sum(1 for v in result["chain_quality"].values() if v == "clean")
        partial = sum(1 for v in result["chain_quality"].values() if v == "partial")
        degraded = sum(1 for v in result["chain_quality"].values() if v == "degraded")
        total = len(result["chain_quality"])
        print(f"Chain quality: {clean} clean / {partial} partial / {degraded} degraded / {total} total")
    if "skipped_days" in result and result["skipped_days"]:
        print(f"Skipped days (degraded): {result['skipped_days']}")
    print()

    # Verdict
    print("=" * 100)
    print("VERDICT")
    print("=" * 100)
    if ev_4 > 0 and sh > 0:
        print(f"  ✓ Live-recorded replay confirms V6_RAW edge: EV=₹{ev_4:+.1f}/trip Sharpe={sh:+.2f}")
        print("    The deployed config holds in recent live regime.")
    elif ev_4 > 0:
        print(f"  ⚠ Positive EV (₹{ev_4:+.1f}/trip) but Sharpe={sh:+.2f} — small sample, watch")
    elif fills == 0:
        print("  ⚠ Zero fills on recorded set — likely calendar filter dominating (28d / Tue/Wed/Thu = ~12 days)")
    else:
        print(f"  ✗ Live-recorded replay diverges from GDFL backtest: EV=₹{ev_4:+.1f}/trip Sharpe={sh:+.2f}")
        print("    GDFL showed +₹83.5/trip — investigate the divergence")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
