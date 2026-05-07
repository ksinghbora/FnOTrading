#!/usr/bin/env python
"""Diagnostic re-run of the orchestrator backtest with per-child breakdown.

Same config as smoke_orchestrator_live.py, but with full per-child
stats from result['trades']:
  - fills per child
  - round-trips per child
  - realized PnL per child
  - days each child fired
  - missing children (e.g. if trend_daily never fires, surfaces here)

Compares each child's contribution to its standalone reference run on
the same corpus to expose:
  - "child fired but lost" → routing chose a winning regime then child
    produced bad trades
  - "child never fired but should have" → orchestrator missed the slot
    (other child took it by tie-break)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections import defaultdict
from datetime import date
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

    # Save raw trades for archival diagnosis
    out_path = Path("/tmp/orch_diagnostic_trades.json")
    out_path.write_text(json.dumps(result.get("trades", []), default=str, indent=2))
    print(f"Trades dumped to {out_path}")
    print()

    # ─── Per-child breakdown ─────────────────────────────────────
    trades = result.get("trades", [])
    by_child: dict[str, dict] = defaultdict(lambda: {
        "fills": 0,
        "buy_value": 0.0,
        "sell_value": 0.0,
        "first_fill": None,
        "last_fill": None,
        "days": set(),
    })

    for t in trades:
        sid = t.get("strategy_id", "?")
        # Strip parent prefix for readable display
        child = sid.split("/", 1)[1] if "/" in sid else sid
        rec = by_child[child]
        rec["fills"] += 1
        avg = float(t.get("average_price", 0) or t.get("price", 0) or 0)
        qty = int(t.get("quantity", 0) or t.get("filled_quantity", 0) or 0)
        side = str(t.get("order_side", t.get("side", ""))).upper()
        if "SELL" in side:
            rec["sell_value"] += avg * qty
        else:
            rec["buy_value"] += avg * qty
        ts = t.get("timestamp") or t.get("fill_time") or t.get("order_time")
        if ts:
            ts_str = str(ts)
            day_str = ts_str[:10] if len(ts_str) >= 10 else ts_str
            rec["days"].add(day_str)
            if rec["first_fill"] is None or ts_str < rec["first_fill"]:
                rec["first_fill"] = ts_str
            if rec["last_fill"] is None or ts_str > rec["last_fill"]:
                rec["last_fill"] = ts_str

    print("=" * 100)
    print("PER-CHILD BREAKDOWN")
    print("=" * 100)
    print(f"  {'child':<22s} {'fills':>6s} {'days':>5s} {'buy_value':>12s} {'sell_value':>12s} {'net (sell-buy)':>15s}")
    print("  " + "-" * 86)

    total_fills = 0
    total_net = 0.0
    for child in ("iron_condor", "iron_butterfly", "short_strangle", "trend_daily"):
        rec = by_child.get(child, {"fills": 0, "buy_value": 0.0, "sell_value": 0.0, "days": set()})
        net = rec["sell_value"] - rec["buy_value"]
        total_fills += rec["fills"]
        total_net += net
        print(f"  {child:<22s} {rec['fills']:>6d} {len(rec['days']):>5d} "
              f"{rec['buy_value']:>12,.0f} {rec['sell_value']:>12,.0f} {net:>+15,.0f}")

    # Catch any unexpected child names in trades
    for child in by_child:
        if child not in ("iron_condor", "iron_butterfly", "short_strangle", "trend_daily"):
            rec = by_child[child]
            net = rec["sell_value"] - rec["buy_value"]
            print(f"  {child:<22s} {rec['fills']:>6d} {len(rec['days']):>5d} "
                  f"{rec['buy_value']:>12,.0f} {rec['sell_value']:>12,.0f} {net:>+15,.0f} [UNEXPECTED]")

    print("  " + "-" * 86)
    print(f"  {'TOTAL':<22s} {total_fills:>6d} {'':>5s} "
          f"{'':>12s} {'':>12s} {total_net:>+15,.0f}")
    print()
    print("(Note: net = sell_value - buy_value, BEFORE charges. Final PnL accounts for charges.)")
    print()

    # ─── Daily fire rate ────────────────────────────────────────
    print("=" * 100)
    print("DAYS EACH CHILD FIRED (first 30 days × child)")
    print("=" * 100)
    all_days = sorted(set().union(*(rec["days"] for rec in by_child.values())))
    if all_days:
        first_30 = all_days[:30]
        print(f"  {'date':<12s} {'IC':>4s} {'IB':>4s} {'SS':>4s} {'TD':>4s}")
        for day in first_30:
            ic = "✓" if day in by_child.get("iron_condor", {"days": set()})["days"] else "·"
            ib = "✓" if day in by_child.get("iron_butterfly", {"days": set()})["days"] else "·"
            ss = "✓" if day in by_child.get("short_strangle", {"days": set()})["days"] else "·"
            td = "✓" if day in by_child.get("trend_daily", {"days": set()})["days"] else "·"
            print(f"  {day:<12s} {ic:>4s} {ib:>4s} {ss:>4s} {td:>4s}")
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
