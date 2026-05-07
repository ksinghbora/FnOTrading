#!/usr/bin/env python
"""Orchestrator tournament — sweep configs to find a profitable orchestrator.

Background: V4 orchestrator with 4 children (IC, IB, SS, trend_daily) lost
-₹150 over 173 days while standalones netted +₹1,273 combined. User
instruction: "make orchestrator profitable."

This script runs N orchestrator configurations sequentially against the
same 173-day post-SEBI corpus and ranks them by per-trip EV. The winner
gets recorded to /tmp/orch_tournament_winner.json so the morning .env
update is data-driven.

Configs tested (each ~16 min):

  V4 baselines:
    1. IC + SS only        (drop IB tie-break, drop TD slot lock)
    2. IC + IB + SS        (drop TD, keep IB to confirm tie-break theory)
    3. all 4 children      (current LIVE config — known negative)

  V5 enhancements (all 4 children):
    4. regime_aware_scoring=true                 — confidence-weighted ranking
    5. regime_aware + cash_floor_confidence=0.50 — trade only when regime clear
    6. regime_aware + margin_aware_selection     — prefer cheap-margin strategies

  Reference standalones:
    7. SS V1 standalone (re-validate +₹43.8/trip)

Decision rule: best per-trip EV ≥ +₹10 → write winner. Otherwise → recommend
SS V1 solo LIVE (Plan B), orchestrator stays shadow.
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


def _orch_config(children: list[str], **flag_overrides) -> dict:
    """Return a deep-copied orchestrator config with only listed children
    and optional V5 flag overrides."""
    cfg = copy.deepcopy(ORCHESTRATOR_CONFIG)
    cfg["children"] = list(children)
    # Drop children_params for any child not in the list
    cfg["children_params"] = {
        k: v for k, v in cfg["children_params"].items() if k in children
    }
    for k, v in flag_overrides.items():
        cfg[k] = v
    return cfg


# Standalone SS V1 config — used for reference re-validation
SS_V1_STANDALONE = {
    "underlying": "NIFTY",
    "quantity_lots": 1,
    "shadow_only": False,  # backtest doesn't care; engine runs same path
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
    # PT/SL/trail come from V1 winner defaults in calibration module
}


CONFIGS = [
    # (name, strategy_name, params)
    ("ORCH_IC_SS",
     "orchestrator",
     _orch_config(["iron_condor", "short_strangle"])),

    ("ORCH_IC_IB_SS",
     "orchestrator",
     _orch_config(["iron_condor", "iron_butterfly", "short_strangle"])),

    ("ORCH_ALL4_V5_REGIME",
     "orchestrator",
     _orch_config(
         ["iron_condor", "iron_butterfly", "short_strangle", "trend_daily"],
         regime_aware_scoring=True,
     )),

    ("ORCH_ALL4_V5_CASHFLOOR",
     "orchestrator",
     _orch_config(
         ["iron_condor", "iron_butterfly", "short_strangle", "trend_daily"],
         regime_aware_scoring=True,
         cash_floor_confidence=0.50,
     )),

    ("ORCH_ALL4_V5_MARGIN",
     "orchestrator",
     _orch_config(
         ["iron_condor", "iron_butterfly", "short_strangle", "trend_daily"],
         regime_aware_scoring=True,
         margin_aware_selection=True,
     )),

    ("ORCH_IC_SS_V5_REGIME",
     "orchestrator",
     _orch_config(
         ["iron_condor", "short_strangle"],
         regime_aware_scoring=True,
     )),

    ("STANDALONE_SS_V1_RECHECK",
     "short_strangle",
     SS_V1_STANDALONE),
]


def fmt(name: str, result: dict) -> dict:
    if "error" in result:
        return {
            "name": name, "error": result["error"],
            "fills": 0, "pnl": 0.0, "wr": 0.0, "sharpe": 0.0, "ev_per_trip_4": 0.0
        }
    m = result.get("metrics", {})
    fills = int(m.get("num_trades", 0))
    pnl = float(m.get("total_pnl", 0))
    wr = float(m.get("win_rate", 0))
    sh = float(m.get("sharpe_ratio", 0))
    # Conservative trip estimate: 4 legs per round trip (matches IC/IB/SS-with-hedge)
    trips_4 = max(1, fills // 4)
    ev_4 = pnl / trips_4
    return {
        "name": name, "fills": fills, "pnl": pnl, "wr": wr, "sharpe": sh,
        "trips_4": trips_4, "ev_per_trip_4": ev_4, "error": None,
        "params": result.get("params", {}),
    }


async def run_one(name: str, strategy_name: str, params: dict) -> dict:
    print(f"\n{'='*100}\nRUN: {name}  (strategy={strategy_name})\n{'='*100}", flush=True)
    print(f"  children: {params.get('children', '(standalone)')}", flush=True)
    flags = {k: params[k] for k in
             ("regime_aware_scoring", "cash_floor_confidence",
              "margin_aware_selection", "block_correlated_families",
              "daily_max_drawdown_inr") if k in params}
    if flags:
        print(f"  V5 flags: {flags}", flush=True)
    source = GDFLMarketSource(PARQUET_DIR, "NIFTY", NIFTY_SPOT_TOKEN)
    engine = BacktestEngine()
    return await engine.run(
        strategy_name=strategy_name,
        strategy_id=f"{name.lower()}_bt",
        strategy_params=params,
        num_days=SMOKE_DAYS,
        start_date=SMOKE_START,
        initial_capital=1_000_000,
        market_source=source,
    )


async def main() -> int:
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(message)s", datefmt="%H:%M:%S"
    )
    _import_strategies()

    print("=" * 100)
    print("ORCHESTRATOR TOURNAMENT — find a profitable config")
    print("=" * 100)
    print(f"Configs to test: {len(CONFIGS)}")
    print(f"Corpus: {SMOKE_DAYS} days starting {SMOKE_START}")
    print()

    results = []
    for name, strategy_name, params in CONFIGS:
        try:
            result = await run_one(name, strategy_name, params)
            row = fmt(name, result)
            results.append(row)
            print(f"\n  → {name}: {row['fills']} fills | ₹{row['pnl']:+,.0f} | "
                  f"{row['wr']:.1f}% WR | Sharpe {row['sharpe']:+.2f} | "
                  f"₹{row['ev_per_trip_4']:+.1f}/trip\n",
                  flush=True)
        except Exception as e:
            logging.exception(f"Run failed for {name}")
            results.append({"name": name, "error": str(e), "fills": 0,
                            "pnl": 0.0, "wr": 0.0, "sharpe": 0.0,
                            "trips_4": 0, "ev_per_trip_4": 0.0})

    # ─── Final summary ──────────────────────────────────────────
    print()
    print("=" * 100)
    print("TOURNAMENT SUMMARY (sorted by per-trip EV, conservative 4-leg trip count)")
    print("=" * 100)
    print(f"  {'name':<32s} {'fills':>6s} {'PnL':>10s} {'WR':>7s} {'Sharpe':>8s} {'₹/trip':>10s}")
    print("  " + "-" * 80)

    successful = [r for r in results if not r.get("error")]
    successful.sort(key=lambda r: -r["ev_per_trip_4"])
    for r in successful:
        marker = ""
        if r["ev_per_trip_4"] > 10 and r["sharpe"] > 0:
            marker = " ✓ DEPLOY-READY"
        elif r["ev_per_trip_4"] > 0:
            marker = " ⚠ marginal"
        else:
            marker = " ✗ negative"
        print(f"  {r['name']:<32s} {r['fills']:>6d} {r['pnl']:>+10,.0f} "
              f"{r['wr']:>6.1f}% {r['sharpe']:>+8.2f} {r['ev_per_trip_4']:>+10,.1f}{marker}")
    for r in [r for r in results if r.get("error")]:
        print(f"  {r['name']:<32s} ERROR: {r['error']}")

    # ─── Pick winner ────────────────────────────────────────────
    print()
    print("=" * 100)
    print("DECISION")
    print("=" * 100)

    deploy_ready = [r for r in successful
                    if r["ev_per_trip_4"] > 10 and r["sharpe"] > 0
                    and not r["name"].startswith("STANDALONE")]
    if deploy_ready:
        winner = deploy_ready[0]  # already sorted by EV desc
        print(f"  WINNER: {winner['name']}")
        print(f"    {winner['fills']} fills | ₹{winner['pnl']:+,.0f} | "
              f"{winner['wr']:.1f}% WR | Sharpe {winner['sharpe']:+.2f} | "
              f"₹{winner['ev_per_trip_4']:+.1f}/trip")
        # Find the original config
        winner_cfg = next(c for c in CONFIGS if c[0] == winner["name"])
        recommendation = {
            "verdict": "deploy_orchestrator",
            "config_name": winner["name"],
            "params": winner_cfg[2],
            "metrics": winner,
        }
    else:
        print("  No orchestrator config beat the +₹10/trip threshold.")
        print("  FALLBACK: deploy SS V1 solo LIVE (the validated +Sharpe winner).")
        ss_check = next((r for r in successful if r["name"] == "STANDALONE_SS_V1_RECHECK"), None)
        if ss_check and ss_check["ev_per_trip_4"] > 10:
            print(f"    SS V1 re-check: ₹{ss_check['ev_per_trip_4']:+.1f}/trip — confirmed winner")
        recommendation = {
            "verdict": "fallback_ss_v1_solo",
            "config_name": "STANDALONE_SS_V1_RECHECK",
            "params": SS_V1_STANDALONE,
            "metrics": ss_check,
        }

    # Persist for the morning .env update
    out = Path("/tmp/orch_tournament_winner.json")
    out.write_text(json.dumps({
        "all_results": results,
        "recommendation": recommendation,
    }, default=str, indent=2))
    print(f"\n  Recommendation saved to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
