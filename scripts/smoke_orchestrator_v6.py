#!/usr/bin/env python
"""V6 multi-slot orchestrator backtest — does parallel execution beat single-slot?

Tests two V6 configs against the V5_MARGIN single-slot winner (last
night's deployment) and the SS V1 standalone reference:

  V6_MULTISLOT_4 — max_concurrent_slots=4, all V5 flags off (pure
                   activity unlock; lets every child fire when its own
                   gate passes, no inter-child routing competition)

  V6_MULTISLOT_4_MARGIN — slots=4 + V5_MARGIN flags ON (regime-aware
                          + margin-aware, but no slot competition)

Reference points:
  V5_MARGIN (deployed): 40 fills | +₹238 | Sharpe +0.18 | +₹23.8/trip
  SS V1 standalone:     72 fills | +₹788 | Sharpe +0.57 | +₹43.8/trip
  Sum-of-standalones:   76 trips | +₹1,273 (theoretical max)

Decision rule: V6 ≥ V5_MARGIN and Sharpe ≥ +0.18 → upgrade deployment.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtest.engine import BacktestEngine, _import_strategies
from src.backtest.gdfl_market_source import GDFLMarketSource
from src.market_data.simulator import NIFTY_SPOT_TOKEN
from scripts.smoke_orchestrator_live import ORCHESTRATOR_CONFIG, PARQUET_DIR, SMOKE_DAYS, SMOKE_START


def _v6_config(slots: int, **flag_overrides) -> dict:
    cfg = copy.deepcopy(ORCHESTRATOR_CONFIG)
    cfg["max_concurrent_slots"] = slots
    for k, v in flag_overrides.items():
        cfg[k] = v
    return cfg


CONFIGS = [
    ("V6_MULTISLOT_4_RAW",
     _v6_config(4)),
    ("V6_MULTISLOT_4_MARGIN",
     _v6_config(4, regime_aware_scoring=True, margin_aware_selection=True)),
    ("V6_MULTISLOT_4_CORR_GUARD",
     _v6_config(4, regime_aware_scoring=True, margin_aware_selection=True,
                block_correlated_families=True)),
]


def fmt(name: str, result: dict) -> dict:
    if "error" in result:
        return {"name": name, "error": result["error"], "fills": 0,
                "pnl": 0.0, "wr": 0.0, "sharpe": 0.0, "ev_per_trip_4": 0.0}
    m = result.get("metrics", {})
    fills = int(m.get("num_trades", 0))
    pnl = float(m.get("total_pnl", 0))
    wr = float(m.get("win_rate", 0))
    sh = float(m.get("sharpe_ratio", 0))
    trips_4 = max(1, fills // 4)
    ev_4 = pnl / trips_4
    return {"name": name, "fills": fills, "pnl": pnl, "wr": wr, "sharpe": sh,
            "trips_4": trips_4, "ev_per_trip_4": ev_4, "error": None,
            "params": result.get("params", {})}


async def run_one(name: str, params: dict) -> dict:
    print(f"\n{'='*100}\nRUN: {name}\n{'='*100}", flush=True)
    print(f"  max_concurrent_slots: {params.get('max_concurrent_slots', 1)}", flush=True)
    flags = {k: params[k] for k in
             ("regime_aware_scoring", "cash_floor_confidence",
              "margin_aware_selection", "block_correlated_families",
              "max_total_margin_lakhs") if k in params}
    if flags:
        print(f"  flags: {flags}", flush=True)
    source = GDFLMarketSource(PARQUET_DIR, "NIFTY", NIFTY_SPOT_TOKEN)
    engine = BacktestEngine()
    return await engine.run(
        strategy_name="orchestrator",
        strategy_id=f"{name.lower()}_bt",
        strategy_params=params,
        num_days=SMOKE_DAYS,
        start_date=SMOKE_START,
        initial_capital=1_000_000,
        market_source=source,
    )


async def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    _import_strategies()
    print("=" * 100)
    print("V6 MULTI-SLOT ORCHESTRATOR BACKTEST")
    print("=" * 100)
    print(f"Configs: {len(CONFIGS)} × ~16 min runtime each")
    print()
    print("References:")
    print("  V5_MARGIN (single-slot, deployed):  40 fills | +₹238  | Sharpe +0.18 | +₹23.8/trip")
    print("  SS V1 standalone:                   72 fills | +₹788  | Sharpe +0.57 | +₹43.8/trip")
    print("  Sum-of-standalones (theoretical):  ~76 trips | +₹1,273 (upper bound)")
    print()

    results = []
    for name, params in CONFIGS:
        try:
            result = await run_one(name, params)
            row = fmt(name, result)
            results.append(row)
            print(f"\n  → {name}: {row['fills']} fills | ₹{row['pnl']:+,.0f} | "
                  f"{row['wr']:.1f}% WR | Sharpe {row['sharpe']:+.2f} | "
                  f"₹{row['ev_per_trip_4']:+.1f}/trip\n", flush=True)
        except Exception as e:
            logging.exception(f"Run failed for {name}")
            results.append({"name": name, "error": str(e), "fills": 0,
                            "pnl": 0.0, "wr": 0.0, "sharpe": 0.0,
                            "trips_4": 0, "ev_per_trip_4": 0.0})

    # Summary
    print()
    print("=" * 100)
    print("V6 RESULTS (sorted by per-trip EV)")
    print("=" * 100)
    print(f"  {'name':<32s} {'fills':>6s} {'PnL':>10s} {'WR':>7s} {'Sharpe':>8s} {'₹/trip':>10s}")
    print("  " + "-" * 80)
    successful = [r for r in results if not r.get("error")]
    successful.sort(key=lambda r: -r["ev_per_trip_4"])
    for r in successful:
        marker = " ✓ DEPLOY" if r["pnl"] > 238 and r["sharpe"] > 0.18 else (
            " ⚠ marginal" if r["pnl"] > 0 else " ✗")
        print(f"  {r['name']:<32s} {r['fills']:>6d} {r['pnl']:>+10,.0f} "
              f"{r['wr']:>6.1f}% {r['sharpe']:>+8.2f} {r['ev_per_trip_4']:>+10,.1f}{marker}")

    # Pick winner
    print()
    deploy_ready = [r for r in successful
                    if r["pnl"] > 238 and r["sharpe"] > 0.18]
    if deploy_ready:
        winner = deploy_ready[0]
        print(f"  V6 WINNER: {winner['name']} — beats V5_MARGIN baseline")
    else:
        print("  No V6 config beats V5_MARGIN baseline. Keep current deployment.")

    out = Path("/tmp/v6_results.json")
    out.write_text(json.dumps(results, default=str, indent=2))
    print(f"\n  Results saved to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
