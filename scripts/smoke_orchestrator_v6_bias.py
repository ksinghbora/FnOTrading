#!/usr/bin/env python
"""V6 + advisor bias sensitivity test on 173-day GDFL post-SEBI corpus.

Compares two configs:
  V6_RAW (deployed baseline):  +₹2,422 / Sharpe +0.97 / +₹83.5/trip
  V6_BIAS (NEW):               same config + use_advisor_bias=True

The bias source is data/day_bias_backfill_gdfl/ (synthetic, VIX-based,
NOT real AI predictions). Set BACKFILL_DAY_BIAS_DIR env var so
load_day_bias() reads from the backfill dir instead of the live
data/day_bias.json.

Decision rule (sensitivity test):
  PnL shift > +10% from baseline → integration adds value, deploy
  ±10% shift → noise, don't bother
  PnL shift < -10% → bias HURTS, do NOT deploy

This tests PLUMBING + sensitivity to the synthetic bias rule. It is
NOT a claim about real AI alpha — that requires forward-collection
of real Claude outputs over months.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Point load_day_bias() at the synthetic GDFL backfill dir
os.environ["BACKFILL_DAY_BIAS_DIR"] = "data/day_bias_backfill_gdfl"

from src.backtest.engine import BacktestEngine, _import_strategies
from src.backtest.gdfl_market_source import GDFLMarketSource
from src.market_data.simulator import NIFTY_SPOT_TOKEN
from scripts.smoke_orchestrator_live import ORCHESTRATOR_CONFIG, PARQUET_DIR, SMOKE_DAYS, SMOKE_START


def _config_with_bias(use_bias: bool) -> dict:
    cfg = copy.deepcopy(ORCHESTRATOR_CONFIG)
    cfg["max_concurrent_slots"] = 4  # V6 multi-slot
    cfg["use_advisor_bias"] = use_bias
    cfg["advisor_bias_min_confidence"] = 0.5
    return cfg


async def run_one(name: str, params: dict) -> dict:
    print(f"\n{'='*100}\nRUN: {name}\n{'='*100}", flush=True)
    print(f"  use_advisor_bias: {params.get('use_advisor_bias', False)}", flush=True)
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


def fmt(name: str, result: dict) -> dict:
    if "error" in result:
        return {"name": name, "error": result["error"], "fills": 0,
                "pnl": 0.0, "wr": 0.0, "sharpe": 0.0, "ev": 0.0}
    m = result.get("metrics", {})
    fills = int(m.get("num_trades", 0))
    pnl = float(m.get("total_pnl", 0))
    wr = float(m.get("win_rate", 0))
    sh = float(m.get("sharpe_ratio", 0))
    trips_4 = max(1, fills // 4)
    return {"name": name, "fills": fills, "pnl": pnl, "wr": wr, "sharpe": sh,
            "trips_4": trips_4, "ev": pnl / trips_4, "error": None}


async def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    _import_strategies()
    print("=" * 100)
    print("V6 + ADVISOR BIAS SENSITIVITY TEST — 173-day GDFL post-SEBI corpus")
    print("=" * 100)
    print(f"  Bias source: data/day_bias_backfill_gdfl (370 synthetic JSON files)")
    print(f"  V6 multi-slot: max_concurrent_slots=4")
    print()

    configs = [
        ("V6_RAW", _config_with_bias(False)),
        ("V6_BIAS_SYNTHETIC", _config_with_bias(True)),
    ]

    results = []
    for name, params in configs:
        try:
            result = await run_one(name, params)
            row = fmt(name, result)
            results.append(row)
            print(f"\n  → {name}: {row['fills']} fills | ₹{row['pnl']:+,.0f} | "
                  f"{row['wr']:.1f}% WR | Sharpe {row['sharpe']:+.2f} | "
                  f"₹{row['ev']:+.1f}/trip\n", flush=True)
        except Exception as e:
            logging.exception(f"Run failed for {name}")
            results.append({"name": name, "error": str(e), "fills": 0, "pnl": 0.0,
                            "wr": 0.0, "sharpe": 0.0, "trips_4": 0, "ev": 0.0})

    print()
    print("=" * 100)
    print("SENSITIVITY ANALYSIS")
    print("=" * 100)
    print(f"  {'config':<24s} {'fills':>6s} {'PnL':>10s} {'WR':>7s} {'Sharpe':>8s} {'₹/trip':>10s}")
    print("  " + "-" * 70)
    for r in results:
        if r.get("error"):
            print(f"  {r['name']:<24s} ERROR: {r['error']}")
            continue
        print(f"  {r['name']:<24s} {r['fills']:>6d} {r['pnl']:>+10,.0f} "
              f"{r['wr']:>6.1f}% {r['sharpe']:>+8.2f} {r['ev']:>+10,.1f}")

    if len(results) == 2 and not any(r.get("error") for r in results):
        baseline = results[0]
        with_bias = results[1]
        pnl_delta = with_bias["pnl"] - baseline["pnl"]
        pnl_pct = (pnl_delta / abs(baseline["pnl"])) * 100 if baseline["pnl"] else 0
        sharpe_delta = with_bias["sharpe"] - baseline["sharpe"]
        ev_delta = with_bias["ev"] - baseline["ev"]
        print()
        print(f"  PnL delta (synthetic bias vs raw): ₹{pnl_delta:+,.0f} ({pnl_pct:+.1f}%)")
        print(f"  Sharpe delta:                      {sharpe_delta:+.2f}")
        print(f"  EV/trip delta:                     ₹{ev_delta:+.1f}")
        print()
        if abs(pnl_pct) > 10:
            verdict = (
                "LARGE shift detected — bias plumbing is sensitive. "
                "Decision: deploy real-AI integration if confidence in advisor signal."
                if pnl_pct > 0 else
                "LARGE NEGATIVE shift — bias HURTS. Do NOT deploy real-AI integration."
            )
        elif abs(pnl_pct) > 5:
            verdict = "MODERATE shift — bias has measurable effect. Worth investigating."
        else:
            verdict = "NOISE — bias has minimal effect on V6's PnL. Don't bother integrating."
        print(f"  VERDICT: {verdict}")

    out = Path("/tmp/v6_bias_sensitivity.json")
    out.write_text(json.dumps(results, default=str, indent=2))
    print(f"\n  Saved to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
