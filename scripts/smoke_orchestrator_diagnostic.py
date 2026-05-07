#!/usr/bin/env python
"""Diagnostic re-run of the orchestrator backtest with per-child breakdown.

Same config as smoke_orchestrator_live.py but instruments the result for
detailed attribution. Each trade carries strategy_id (added in commit
following this script's first version), so we can group by:
  - child name (which child fired)
  - day (when each child fired)
  - direction (buy_value vs sell_value → realised proxy)

Compares each child's contribution to its standalone reference.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtest.engine import BacktestEngine, _import_strategies
from src.backtest.gdfl_market_source import GDFLMarketSource
from src.market_data.simulator import NIFTY_SPOT_TOKEN
from scripts.smoke_orchestrator_live import ORCHESTRATOR_CONFIG, PARQUET_DIR, SMOKE_DAYS, SMOKE_START


async def main():
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    _import_strategies()

    print("=" * 100, flush=True)
    print("ORCHESTRATOR DIAGNOSTIC — per-child breakdown")
    print("=" * 100, flush=True)
    print()

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

    if "error" in result:
        print(f"ERROR: {result['error']}")
        return 1

    out_path = Path("/tmp/orch_diagnostic_trades.json")
    out_path.write_text(json.dumps(result.get("trades", []), default=str, indent=2))
    print(f"Trades dumped to {out_path}")
    print()

    # ─── Per-child breakdown via strategy_id ─────────────────────
    trades = result.get("trades", [])
    by_child: dict[str, dict] = defaultdict(lambda: {
        "fills": 0,
        "buy_value": 0.0,
        "sell_value": 0.0,
        "fill_timestamps": [],
    })

    for t in trades:
        sid = t.get("strategy_id") or "?"
        # Strip parent prefix for readable display
        child = sid.split("/", 1)[1] if "/" in sid else sid
        rec = by_child[child]
        rec["fills"] += 1
        avg = float(t.get("average_price", 0))
        qty = int(t.get("quantity", 0))
        side = str(t.get("transaction_type", "")).upper()
        if side == "SELL":
            rec["sell_value"] += avg * qty
        else:
            rec["buy_value"] += avg * qty
        ts = t.get("fill_timestamp", "")
        if ts:
            rec["fill_timestamps"].append(ts)

    # ─── Print per-child table ──────────────────────────────────
    print("=" * 100)
    print("PER-CHILD BREAKDOWN (via strategy_id)")
    print("=" * 100)
    print(f"  {'child':<22s} {'fills':>6s} {'buy_value':>12s} {'sell_value':>12s} {'net (sell-buy)':>15s}")
    print("  " + "-" * 80)

    expected_children = ("iron_condor", "iron_butterfly", "short_strangle", "trend_daily")
    total_fills = 0
    total_net = 0.0
    for child in expected_children:
        rec = by_child.get(child, {"fills": 0, "buy_value": 0.0, "sell_value": 0.0})
        net = rec["sell_value"] - rec["buy_value"]
        total_fills += rec["fills"]
        total_net += net
        print(f"  {child:<22s} {rec['fills']:>6d} "
              f"{rec['buy_value']:>12,.0f} {rec['sell_value']:>12,.0f} {net:>+15,.0f}")
    # Catch any unexpected children
    for child in by_child:
        if child not in expected_children:
            rec = by_child[child]
            net = rec["sell_value"] - rec["buy_value"]
            print(f"  {child:<22s} {rec['fills']:>6d} "
                  f"{rec['buy_value']:>12,.0f} {rec['sell_value']:>12,.0f} {net:>+15,.0f}  [UNEXPECTED]")
    print("  " + "-" * 80)
    print(f"  {'TOTAL':<22s} {total_fills:>6d} {'':>12s} {'':>12s} {total_net:>+15,.0f}")
    print()
    print("(net = sell_value - buy_value, before charges. Positive net ≈ realised PnL.)")
    print()

    # ─── Daily PnL trail ────────────────────────────────────────
    daily = result.get("daily_results", [])
    if daily:
        print("=" * 100)
        print("DAILY P&L (every day with non-zero pnl)")
        print("=" * 100)
        print(f"  {'date':<12s} {'pnl':>10s} {'realized':>10s} {'unrealized':>11s} {'charges':>8s} {'trades':>7s}")
        non_zero = [d for d in daily if d.get("pnl") not in (0, 0.0, None) or d.get("trades", 0) > 0]
        for d in non_zero[:50]:  # cap to keep output readable
            print(f"  {d.get('date', '?'):<12s} "
                  f"{float(d.get('pnl', 0)):>+10,.0f} "
                  f"{float(d.get('realized_pnl', 0)):>+10,.0f} "
                  f"{float(d.get('unrealized_mtm', 0)):>+11,.0f} "
                  f"{float(d.get('charges', 0)):>8,.0f} "
                  f"{d.get('trades', 0):>7d}")
        if len(non_zero) > 50:
            print(f"  ... ({len(non_zero) - 50} more days truncated)")
        print()
        active_days = sum(1 for d in daily if d.get("trades", 0) > 0)
        print(f"  Days with at least one fill: {active_days} / {len(daily)} total trading days")
    print()

    # ─── Aggregate vs benchmarks ────────────────────────────────
    print("=" * 100)
    print("AGGREGATE")
    print("=" * 100)
    m = result.get("metrics", {})
    print(f"  Orchestrator total:  {m.get('num_trades')} fills | ₹{float(m.get('total_pnl',0)):+,.0f} | "
          f"{float(m.get('win_rate',0)):.1f}% WR | Sharpe {float(m.get('sharpe_ratio',0)):+.2f}")
    print(f"  Total charges:       ₹{float(m.get('total_charges',0)):,.0f}")
    print()
    print("Standalone references (same corpus, run independently):")
    print("  IC v2 + cal:      20 trips | ₹+263  | 47.5% WR | Sharpe +0.26 | +₹13.1/trip")
    print("  IB B2 + cal:      38 trips | ₹+222  | 48.7% WR | Sharpe +0.06 | +₹5.8/trip")
    print("  SS Phase 1.5 V1:  18 trips | ₹+788  | 69.4% WR | Sharpe +0.57 | +₹43.8/trip")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
