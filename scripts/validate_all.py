"""Comprehensive walk-forward validation: filters ON vs OFF + trend debit spread.

Runs 3 comparisons:
1. Premium sellers with filters OFF (baseline)
2. Premium sellers with filters ON (PCR + max pain enabled)
3. Trend Debit Spread strategy (log_only=False)

Usage:
    uv run python scripts/validate_all.py
    uv run python scripts/validate_all.py --tick-interval 5  # 5x faster
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtest.engine import _import_strategies
from src.backtest.walk_forward import WalkForwardEngine, WalkForwardReport


def parse_args():
    parser = argparse.ArgumentParser(description="Full Walk-Forward Validation")
    parser.add_argument("--total-days", type=int, default=180)
    parser.add_argument("--train-days", type=int, default=60)
    parser.add_argument("--test-days", type=int, default=30)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--capital", type=float, default=1_000_000)
    parser.add_argument("--tick-interval", type=int, default=5, help="Default 5 for speed")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def print_comparison(label_a: str, a: WalkForwardReport, label_b: str, b: WalkForwardReport):
    """Print side-by-side comparison of two walk-forward runs."""
    print(f"\n  {'Metric':<30} {label_a:>18} {label_b:>18} {'Delta':>12}")
    print(f"  {'-' * 80}")
    print(f"  {'Test Total P&L':<30} {a.test_total_pnl:>+18,.0f} {b.test_total_pnl:>+18,.0f} {b.test_total_pnl - a.test_total_pnl:>+12,.0f}")
    print(f"  {'Test Mean P&L/window':<30} {a.test_mean_pnl:>+18,.0f} {b.test_mean_pnl:>+18,.0f} {b.test_mean_pnl - a.test_mean_pnl:>+12,.0f}")
    print(f"  {'Test Win Rate':<30} {a.test_win_rate:>17.0f}% {b.test_win_rate:>17.0f}% {b.test_win_rate - a.test_win_rate:>+11.0f}%")
    print(f"  {'Test Sharpe':<30} {a.test_sharpe:>18.2f} {b.test_sharpe:>18.2f} {b.test_sharpe - a.test_sharpe:>+12.2f}")
    print(f"  {'Test Max Drawdown':<30} {a.test_max_drawdown:>18,.0f} {b.test_max_drawdown:>18,.0f} {b.test_max_drawdown - a.test_max_drawdown:>+12,.0f}")
    print(f"  {'Degradation':<30} {a.degradation_pct:>17.1f}% {b.degradation_pct:>17.1f}% {b.degradation_pct - a.degradation_pct:>+11.1f}%")

    # Verdict
    pnl_improved = b.test_total_pnl > a.test_total_pnl
    sharpe_improved = b.test_sharpe > a.test_sharpe
    dd_improved = b.test_max_drawdown > a.test_max_drawdown  # less negative is better

    improvements = sum([pnl_improved, sharpe_improved, dd_improved])
    if improvements >= 2:
        verdict = "IMPROVED"
    elif improvements == 1:
        verdict = "MIXED"
    else:
        verdict = "WORSE"
    print(f"\n  Verdict: {label_b} vs {label_a} = {verdict}")


async def run_validation(args):
    _import_strategies()
    engine = WalkForwardEngine()
    all_results = {}

    premium_strategies = ["short_straddle", "short_strangle", "iron_condor"]

    # ─── 1. Baseline: Filters OFF ─────────────────────────────
    print("\n" + "=" * 80)
    print("  PHASE 1: Premium Sellers — Filters OFF (Baseline)")
    print("=" * 80)

    baseline_reports = {}
    for strat in premium_strategies:
        print(f"\n  Running {strat} (filters OFF)...")
        report = await engine.run(
            strategy_name=strat,
            strategy_params={
                "underlying": "NIFTY",
                "quantity_lots": 1,
                "pcr_filter_enabled": False,
                "max_pain_filter_enabled": False,
            },
            total_days=args.total_days,
            train_days=args.train_days,
            test_days=args.test_days,
            initial_capital=args.capital,
            seed=args.seed,
            tick_interval_minutes=args.tick_interval,
        )
        baseline_reports[strat] = report
        print(f"    Test P&L: {report.test_total_pnl:+,.0f}  "
              f"Sharpe: {report.test_sharpe:.2f}  "
              f"Win: {report.test_win_rate:.0f}%")

    # ─── 2. Filters ON ────────────────────────────────────────
    print("\n" + "=" * 80)
    print("  PHASE 2: Premium Sellers — Filters ON (PCR + Max Pain)")
    print("=" * 80)

    filtered_reports = {}
    for strat in premium_strategies:
        print(f"\n  Running {strat} (filters ON)...")
        report = await engine.run(
            strategy_name=strat,
            strategy_params={
                "underlying": "NIFTY",
                "quantity_lots": 1,
                "pcr_filter_enabled": True,
                "max_pain_filter_enabled": True,
            },
            total_days=args.total_days,
            train_days=args.train_days,
            test_days=args.test_days,
            initial_capital=args.capital,
            seed=args.seed,
            tick_interval_minutes=args.tick_interval,
        )
        filtered_reports[strat] = report
        print(f"    Test P&L: {report.test_total_pnl:+,.0f}  "
              f"Sharpe: {report.test_sharpe:.2f}  "
              f"Win: {report.test_win_rate:.0f}%")

    # ─── 3. Trend Debit Spread ────────────────────────────────
    print("\n" + "=" * 80)
    print("  PHASE 3: Trend Debit Spread (log_only=False)")
    print("=" * 80)

    print(f"\n  Running trend_debit_spread...")
    trend_report = await engine.run(
        strategy_name="trend_debit_spread",
        strategy_params={
            "underlying": "NIFTY",
            "quantity_lots": 1,
            "log_only": False,
        },
        total_days=args.total_days,
        train_days=args.train_days,
        test_days=args.test_days,
        initial_capital=args.capital,
        seed=args.seed,
        tick_interval_minutes=args.tick_interval,
    )
    print(f"    Test P&L: {trend_report.test_total_pnl:+,.0f}  "
          f"Sharpe: {trend_report.test_sharpe:.2f}  "
          f"Win: {trend_report.test_win_rate:.0f}%  "
          f"Degradation: {trend_report.degradation_pct:.1f}%")

    # ─── Summary ──────────────────────────────────────────────
    print("\n\n" + "=" * 80)
    print("  FILTER IMPACT COMPARISON (OFF vs ON)")
    print("=" * 80)

    for strat in premium_strategies:
        print(f"\n  --- {strat.upper()} ---")
        print_comparison("Filters OFF", baseline_reports[strat],
                        "Filters ON", filtered_reports[strat])

    print("\n\n" + "=" * 80)
    print("  COMPLETE STRATEGY PORTFOLIO (Out-of-Sample)")
    print("=" * 80)
    print(f"\n  {'Strategy':<25} {'Test P&L':>12} {'Win%':>8} {'Sharpe':>8} {'Degrade%':>10} {'Verdict':>10}")
    print(f"  {'-' * 75}")

    # Show filtered results for premium sellers + trend strategy
    for strat in premium_strategies:
        r = filtered_reports[strat]
        v = "PASS" if r.test_mean_pnl > 0 and r.test_win_rate >= 50 else "FAIL"
        print(f"  {strat + ' (filtered)':<25} {r.test_total_pnl:>+12,.0f} "
              f"{r.test_win_rate:>7.0f}% {r.test_sharpe:>8.2f} "
              f"{r.degradation_pct:>9.1f}% {v:>10}")

    r = trend_report
    v = "PASS" if r.test_mean_pnl > 0 and r.test_win_rate >= 50 else "FAIL"
    print(f"  {'trend_debit_spread':<25} {r.test_total_pnl:>+12,.0f} "
          f"{r.test_win_rate:>7.0f}% {r.test_sharpe:>8.2f} "
          f"{r.degradation_pct:>9.1f}% {v:>10}")

    # Combined portfolio P&L
    combined = sum(filtered_reports[s].test_total_pnl for s in premium_strategies) + trend_report.test_total_pnl
    print(f"\n  {'COMBINED PORTFOLIO':<25} {combined:>+12,.0f}")
    print(f"{'=' * 80}\n")

    # Save all results
    all_results = {
        "baseline_filters_off": {s: _report_summary(r) for s, r in baseline_reports.items()},
        "filters_on": {s: _report_summary(r) for s, r in filtered_reports.items()},
        "trend_debit_spread": _report_summary(trend_report),
        "combined_test_pnl": combined,
    }
    return all_results


def _report_summary(r: WalkForwardReport) -> dict:
    return {
        "test_total_pnl": r.test_total_pnl,
        "test_mean_pnl": r.test_mean_pnl,
        "test_win_rate": r.test_win_rate,
        "test_sharpe": r.test_sharpe,
        "test_max_drawdown": r.test_max_drawdown,
        "test_profit_factor": r.test_profit_factor,
        "train_mean_pnl": r.train_mean_pnl,
        "degradation_pct": r.degradation_pct,
        "num_windows": r.num_windows,
    }


def main():
    args = parse_args()

    level = logging.WARNING if not args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    print("=" * 80)
    print("  FnO Trading — Complete Walk-Forward Validation")
    print("  Filters ON vs OFF + Trend Debit Spread")
    print("=" * 80)
    print(f"  Total days:    {args.total_days}")
    print(f"  Train/Test:    {args.train_days}d / {args.test_days}d")
    print(f"  Tick interval: {args.tick_interval}m")
    print(f"  Seed:          {args.seed}")
    print()

    results = asyncio.run(run_validation(args))

    output = Path(__file__).parent.parent / "validation_results.json"
    with open(output, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"  Results saved to: {output}")


if __name__ == "__main__":
    main()
