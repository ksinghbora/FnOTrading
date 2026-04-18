"""DEPRECATED: BS-priced parameter sweep. Re-tune on chain replay instead.

Apr 2026: this sweep uses synthetic ticks + BS option prices, which the
audit found to be ~60 % too optimistic on P&L. The earlier "optimal" SL/PT
combos picked here turned out to be overfit to BS-derived noise. Re-run
the same grid against `ReplayBacktestEngine` once the chain corpus has
30+ clean days (currently 13/16).

Systematic sweep of Stop Loss / Profit Target / Trail combinations.

Tests all key parameter combinations for premium (IC + strangle) and trend legs.
Runs against 21 days of real historical data.

Usage: uv run python scripts/sweep_sl_pt.py
"""

import asyncio
import itertools
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.WARNING)

from src.backtest.engine import _import_strategies
from src.backtest.historical_engine import HistoricalBacktestEngine


SPOT_CSV = "data/nifty_spot_minute.csv"
VIX_CSV = "data/india_vix_minute.csv"

BASE_PARAMS = {
    "underlying": "NIFTY",
    "quantity_lots": 1,
}


async def run_one(label: str, overrides: dict) -> dict:
    """Run a single backtest with parameter overrides."""
    engine = HistoricalBacktestEngine()
    params = {**BASE_PARAMS, **overrides}
    result = await engine.run(
        strategy_name="portfolio",
        spot_csv=SPOT_CSV,
        vix_csv=VIX_CSV,
        strategy_params=params,
    )
    m = result.get("metrics", {})
    final_pnl = result.get("final_pnl", 0)
    charges = m.get("total_charges", 0)

    return {
        "label": label,
        "final_pnl": final_pnl,
        "charges": charges,
        "net_pnl": final_pnl,  # final_pnl is already net (includes charges deducted in portfolio)
        "trades": m.get("num_trades", 0),
        "win_rate": m.get("win_rate", 0),
        "avg_win": m.get("avg_win", 0),
        "avg_loss": m.get("avg_loss", 0),
        "wl_ratio": abs(m.get("avg_win", 0) / m.get("avg_loss", 1)) if m.get("avg_loss", 0) != 0 else 0,
        "max_dd": m.get("max_drawdown", 0),
        "sharpe": m.get("sharpe_ratio", 0),
        "profit_factor": m.get("profit_factor", 0),
        **overrides,
    }


def print_row(r: dict, extra: str = ""):
    """Print a single result row."""
    wl = f"{r['wl_ratio']:.2f}x" if r['wl_ratio'] > 0 else "  n/a"
    pf = r.get("profit_factor", 0)
    pf_s = f"{pf:.1f}" if isinstance(pf, (int, float)) and pf < 100 else "inf"
    print(
        f"  {r['label']:<35} P&L={r['net_pnl']:>+8,.0f}  "
        f"WR={r['win_rate']:>5.1f}%  W/L={wl}  PF={pf_s:>5}  "
        f"AvgW={r['avg_win']:>+7,.0f} AvgL={r['avg_loss']:>+7,.0f}  "
        f"Trades={r['trades']}{extra}"
    )


async def main():
    _import_strategies()

    # ── Phase 1: IC Stop Loss & Profit Target ────────────────────────
    print("=" * 90)
    print("PHASE 1: IC Stop Loss × Profit Target sweep")
    print("=" * 90)
    results1 = []
    ic_sl_values = [30.0, 35.0, 40.0, 50.0, 60.0]
    ic_pt_values = [30.0, 40.0, 50.0, 60.0, 70.0]

    for sl, pt in itertools.product(ic_sl_values, ic_pt_values):
        label = f"IC SL={sl:.0f}% PT={pt:.0f}%"
        r = await run_one(label, {"ic_stop_loss_pct": sl, "ic_profit_target_pct": pt})
        results1.append(r)
        print_row(r)

    best_ic = max(results1, key=lambda x: x["net_pnl"])
    print(f"\n  >>> BEST IC: {best_ic['label']}  P&L={best_ic['net_pnl']:>+8,.0f}  W/L={best_ic['wl_ratio']:.2f}x")
    best_ic_sl = best_ic.get("ic_stop_loss_pct", 40.0)
    best_ic_pt = best_ic.get("ic_profit_target_pct", 50.0)

    # ── Phase 2: Premium (Strangle) SL & PT ──────────────────────────
    print("\n" + "=" * 90)
    print("PHASE 2: Strangle SL × PT sweep (with best IC)")
    print("=" * 90)
    results2 = []
    prem_sl_values = [20.0, 25.0, 30.0, 35.0, 40.0]
    prem_pt_values = [10.0, 15.0, 20.0, 25.0, 30.0]

    for sl, pt in itertools.product(prem_sl_values, prem_pt_values):
        label = f"Strangle SL={sl:.0f}% PT={pt:.0f}%"
        r = await run_one(label, {
            "ic_stop_loss_pct": best_ic_sl,
            "ic_profit_target_pct": best_ic_pt,
            "premium_stop_loss_pct": sl,
            "premium_profit_target_pct": pt,
        })
        results2.append(r)
        print_row(r)

    best_prem = max(results2, key=lambda x: x["net_pnl"])
    print(f"\n  >>> BEST Strangle: {best_prem['label']}  P&L={best_prem['net_pnl']:>+8,.0f}  W/L={best_prem['wl_ratio']:.2f}x")
    best_prem_sl = best_prem.get("premium_stop_loss_pct", 30.0)
    best_prem_pt = best_prem.get("premium_profit_target_pct", 15.0)

    # ── Phase 3: Trend SL & PT ───────────────────────────────────────
    print("\n" + "=" * 90)
    print("PHASE 3: Trend SL × PT sweep (with best IC + Strangle)")
    print("=" * 90)
    results3 = []
    trend_sl_values = [15.0, 20.0, 25.0, 30.0, 40.0, 50.0]
    trend_pt_values = [30.0, 40.0, 50.0, 60.0, 80.0]

    for sl, pt in itertools.product(trend_sl_values, trend_pt_values):
        label = f"Trend SL={sl:.0f}% PT={pt:.0f}%"
        r = await run_one(label, {
            "ic_stop_loss_pct": best_ic_sl,
            "ic_profit_target_pct": best_ic_pt,
            "premium_stop_loss_pct": best_prem_sl,
            "premium_profit_target_pct": best_prem_pt,
            "trend_stop_loss_pct": sl,
            "trend_profit_target_pct": pt,
        })
        results3.append(r)
        print_row(r)

    best_trend = max(results3, key=lambda x: x["net_pnl"])
    print(f"\n  >>> BEST Trend: {best_trend['label']}  P&L={best_trend['net_pnl']:>+8,.0f}  W/L={best_trend['wl_ratio']:.2f}x")
    best_trend_sl = best_trend.get("trend_stop_loss_pct", 25.0)
    best_trend_pt = best_trend.get("trend_profit_target_pct", 50.0)

    # ── Phase 4: Trail Stop Fine-Tuning ──────────────────────────────
    print("\n" + "=" * 90)
    print("PHASE 4: Trail Stop fine-tuning (with all best params)")
    print("=" * 90)
    results4 = []
    prem_trail_values = [0.0, 10.0, 15.0, 20.0, 25.0]
    trend_trail_values = [0.0, 15.0, 20.0, 25.0, 30.0]

    for pt_trail, tt_trail in itertools.product(prem_trail_values, trend_trail_values):
        label = f"PremTrail={pt_trail:.0f}% TrendTrail={tt_trail:.0f}%"
        r = await run_one(label, {
            "ic_stop_loss_pct": best_ic_sl,
            "ic_profit_target_pct": best_ic_pt,
            "premium_stop_loss_pct": best_prem_sl,
            "premium_profit_target_pct": best_prem_pt,
            "trend_stop_loss_pct": best_trend_sl,
            "trend_profit_target_pct": best_trend_pt,
            "premium_trail_stop_pct": pt_trail,
            "trend_trailing_stop_pct": tt_trail,
        })
        results4.append(r)
        print_row(r)

    best_trail = max(results4, key=lambda x: x["net_pnl"])
    print(f"\n  >>> BEST Trail: {best_trail['label']}  P&L={best_trail['net_pnl']:>+8,.0f}  W/L={best_trail['wl_ratio']:.2f}x")

    # ── Summary ──────────────────────────────────────────────────────
    best_prem_trail = best_trail.get("premium_trail_stop_pct", 15.0)
    best_trend_trail = best_trail.get("trend_trailing_stop_pct", 25.0)

    print("\n" + "=" * 90)
    print("OPTIMAL PARAMETERS")
    print("=" * 90)
    print(f"  IC Stop Loss:           {best_ic_sl:.0f}%")
    print(f"  IC Profit Target:       {best_ic_pt:.0f}%")
    print(f"  Strangle Stop Loss:     {best_prem_sl:.0f}%")
    print(f"  Strangle Profit Target: {best_prem_pt:.0f}%")
    print(f"  Trend Stop Loss:        {best_trend_sl:.0f}%")
    print(f"  Trend Profit Target:    {best_trend_pt:.0f}%")
    print(f"  Premium Trail Stop:     {best_prem_trail:.0f}%")
    print(f"  Trend Trail Stop:       {best_trend_trail:.0f}%")
    print()
    print(f"  Final P&L:       {best_trail['net_pnl']:>+10,.0f}")
    print(f"  W/L Ratio:       {best_trail['wl_ratio']:.2f}x")
    print(f"  Win Rate:        {best_trail['win_rate']:.1f}%")
    print(f"  Avg Win:         {best_trail['avg_win']:>+10,.0f}")
    print(f"  Avg Loss:        {best_trail['avg_loss']:>+10,.0f}")
    print(f"  Profit Factor:   {best_trail.get('profit_factor', 0)}")
    print(f"  Max Drawdown:    {best_trail['max_dd']:>+10,.0f}")
    print(f"  Sharpe:          {best_trail['sharpe']:.2f}")
    print(f"  Trades:          {best_trail['trades']}")

    # Compare with current params
    print("\n" + "=" * 90)
    print("CURRENT vs OPTIMAL comparison")
    print("=" * 90)
    current = await run_one("CURRENT params", {})
    print_row(current, "  << CURRENT")
    print_row(best_trail, "  << OPTIMAL")
    improvement = best_trail["net_pnl"] - current["net_pnl"]
    print(f"\n  Improvement: {improvement:>+10,.0f} ({improvement / max(abs(current['net_pnl']), 1) * 100:+.0f}%)")

    # Save all results
    all_results = results1 + results2 + results3 + results4
    with open("sweep_results.json", "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\n  All {len(all_results)} combinations saved to sweep_results.json")


if __name__ == "__main__":
    asyncio.run(main())
