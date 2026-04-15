"""Walk-forward (out-of-sample) validation for all strategies.

Splits the simulation into rolling train/test windows to detect overfitting
and validate that strategy performance holds on unseen data.

Usage:
    uv run python scripts/walk_forward_test.py
    uv run python scripts/walk_forward_test.py --strategies short_straddle iron_condor
    uv run python scripts/walk_forward_test.py --total-days 240 --train-days 60 --test-days 30
    uv run python scripts/walk_forward_test.py --tick-interval 5  # 5x faster (less accurate)
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
    parser = argparse.ArgumentParser(
        description="Walk-Forward (Out-of-Sample) Testing"
    )
    parser.add_argument(
        "--strategies", nargs="+", default=None,
        help="Strategies to test (default: straddle, strangle, condor)",
    )
    parser.add_argument(
        "--total-days", type=int, default=180,
        help="Total trading days to cover (default: 180 = ~9 months)",
    )
    parser.add_argument(
        "--train-days", type=int, default=60,
        help="Training window size in trading days (default: 60 = ~3 months)",
    )
    parser.add_argument(
        "--test-days", type=int, default=30,
        help="Test (out-of-sample) window size (default: 30 = ~6 weeks)",
    )
    parser.add_argument(
        "--step-days", type=int, default=None,
        help="Days to slide forward between windows (default: test_days)",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed (default: 42)",
    )
    parser.add_argument(
        "--capital", type=float, default=1_000_000,
        help="Initial capital (default: 1,000,000)",
    )
    parser.add_argument(
        "--tick-interval", type=int, default=1,
        help="Minutes between ticks, 1=accurate 5=fast (default: 1)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose logging",
    )
    parser.add_argument(
        "--save", type=str, default="walk_forward_results.json",
        help="Output file (default: walk_forward_results.json)",
    )
    return parser.parse_args()


def print_report(report: WalkForwardReport):
    """Pretty-print a single strategy's walk-forward report."""
    print(f"\n{'=' * 70}")
    print(f"  {report.strategy.upper()}")
    print(f"  {report.num_windows} windows: train={report.train_days}d, "
          f"test={report.test_days}d")
    print(f"{'=' * 70}")

    # Per-window breakdown
    test_windows = [w for w in report.windows if w.window_type == "test"]
    train_windows = [w for w in report.windows if w.window_type == "train"]

    print(f"\n  {'Window':<8} {'Train Period':<25} {'Train P&L':>12} "
          f"{'Test Period':<25} {'Test P&L':>12} {'Verdict':>10}")
    print(f"  {'-' * 95}")

    for i in range(len(test_windows)):
        tw = train_windows[i]
        ow = test_windows[i]
        verdict = "PASS" if ow.final_pnl > 0 else "FAIL"
        print(
            f"  {i + 1:<8} "
            f"{str(tw.start_date)} to {str(tw.end_date):<10} "
            f"{tw.final_pnl:>+12,.0f} "
            f"{str(ow.start_date)} to {str(ow.end_date):<10} "
            f"{ow.final_pnl:>+12,.0f} "
            f"{'  ' + verdict:>10}"
        )

    # Summary
    print(f"\n  {'Aggregated Test (Out-of-Sample) Metrics':^70}")
    print(f"  {'-' * 50}")
    print(f"  Total Test P&L:       Rs {report.test_total_pnl:>12,.2f}")
    print(f"  Mean Test P&L/window: Rs {report.test_mean_pnl:>12,.2f}")
    print(f"  Test Win Rate:        {report.test_win_rate:>11.0f}%  "
          f"({sum(1 for w in test_windows if w.final_pnl > 0)}/{len(test_windows)} windows)")
    print(f"  Test Sharpe (ann.):   {report.test_sharpe:>11.2f}")
    print(f"  Test Max Drawdown:    Rs {report.test_max_drawdown:>12,.2f}")
    print(f"  Test Profit Factor:   {report.test_profit_factor:>11.2f}")

    # Overfitting check
    print(f"\n  {'Overfitting Analysis':^70}")
    print(f"  {'-' * 50}")
    print(f"  Mean Train P&L:       Rs {report.train_mean_pnl:>12,.2f}")
    print(f"  Mean Test P&L:        Rs {report.test_mean_pnl:>12,.2f}")
    print(f"  Degradation:          {report.degradation_pct:>11.1f}%")

    if report.degradation_pct > 80:
        verdict = "OVERFIT — performance collapses out-of-sample"
    elif report.degradation_pct > 50:
        verdict = "CAUTION — significant degradation out-of-sample"
    elif report.degradation_pct > 20:
        verdict = "ACCEPTABLE — some degradation but still profitable"
    elif report.test_mean_pnl > 0:
        verdict = "ROBUST — holds up well out-of-sample"
    else:
        verdict = "UNPROFITABLE — negative test returns"

    print(f"  Verdict:              {verdict}")


def print_summary(reports: list[WalkForwardReport]):
    """Print comparative summary across all strategies."""
    print(f"\n\n{'=' * 80}")
    print(f"  {'WALK-FORWARD SUMMARY':^76}")
    print(f"{'=' * 80}")
    print(f"\n  {'Strategy':<20} {'Test P&L':>12} {'Test Win%':>10} {'Sharpe':>8} "
          f"{'Degrade%':>10} {'Verdict':>12}")
    print(f"  {'-' * 75}")

    for r in reports:
        if r.degradation_pct > 80 or r.test_mean_pnl <= 0:
            verdict = "FAIL"
        elif r.degradation_pct > 50:
            verdict = "CAUTION"
        elif r.test_win_rate >= 50 and r.test_mean_pnl > 0:
            verdict = "PASS"
        else:
            verdict = "MARGINAL"

        print(
            f"  {r.strategy:<20} {r.test_total_pnl:>+12,.0f} "
            f"{r.test_win_rate:>9.0f}% {r.test_sharpe:>8.2f} "
            f"{r.degradation_pct:>9.1f}% {verdict:>12}"
        )

    print(f"\n  Interpretation:")
    print(f"  - PASS:     Profitable out-of-sample, low degradation — ready for live")
    print(f"  - MARGINAL: Borderline — needs more data or param refinement")
    print(f"  - CAUTION:  Significant degradation — possible overfitting")
    print(f"  - FAIL:     Not profitable out-of-sample — do not deploy")
    print(f"{'=' * 80}\n")


def report_to_dict(report: WalkForwardReport) -> dict:
    """Serialize report for JSON output."""
    return {
        "strategy": report.strategy,
        "num_windows": report.num_windows,
        "train_days": report.train_days,
        "test_days": report.test_days,
        "seed": report.seed,
        "test_total_pnl": report.test_total_pnl,
        "test_mean_pnl": report.test_mean_pnl,
        "test_win_rate": report.test_win_rate,
        "test_sharpe": report.test_sharpe,
        "test_max_drawdown": report.test_max_drawdown,
        "test_profit_factor": report.test_profit_factor,
        "train_mean_pnl": report.train_mean_pnl,
        "degradation_pct": report.degradation_pct,
        "windows": [
            {
                "type": w.window_type,
                "num": w.window_num,
                "start": str(w.start_date),
                "end": str(w.end_date),
                "days": w.num_days,
                "pnl": w.final_pnl,
                "sharpe": w.metrics.get("sharpe_ratio", 0),
                "win_rate": w.metrics.get("win_rate", 0),
            }
            for w in report.windows
        ],
        "test_equity_curve": report.test_equity_curve,
    }


async def run_all(
    strategies: list[str],
    total_days: int,
    train_days: int,
    test_days: int,
    step_days: int | None,
    seed: int,
    capital: float,
    tick_interval: int,
) -> list[WalkForwardReport]:
    _import_strategies()
    engine = WalkForwardEngine()
    reports: list[WalkForwardReport] = []

    for strat in strategies:
        print(f"\n  Running walk-forward for {strat}...")
        report = await engine.run(
            strategy_name=strat,
            strategy_params={"underlying": "NIFTY", "quantity_lots": 1},
            total_days=total_days,
            train_days=train_days,
            test_days=test_days,
            step_days=step_days,
            initial_capital=capital,
            seed=seed,
            tick_interval_minutes=tick_interval,
        )
        reports.append(report)
        print_report(report)

    return reports


def main():
    args = parse_args()

    level = logging.WARNING if not args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    strategies = args.strategies or [
        "short_straddle", "short_strangle", "iron_condor"
    ]

    print("=" * 80)
    print("  FnO Trading — Walk-Forward (Out-of-Sample) Validation")
    print("=" * 80)
    print(f"  Strategies:    {', '.join(strategies)}")
    print(f"  Total days:    {args.total_days}")
    print(f"  Train window:  {args.train_days} days")
    print(f"  Test window:   {args.test_days} days")
    print(f"  Step:          {args.step_days or args.test_days} days")
    print(f"  Seed:          {args.seed}")
    print(f"  Tick interval: {args.tick_interval}m")
    num_windows = (args.total_days - args.train_days) // (args.step_days or args.test_days)
    print(f"  Est. windows:  ~{num_windows} per strategy")
    print()

    reports = asyncio.run(run_all(
        strategies, args.total_days, args.train_days, args.test_days,
        args.step_days, args.seed, args.capital, args.tick_interval,
    ))

    if len(reports) > 1:
        print_summary(reports)

    # Save results
    output_path = Path(__file__).parent.parent / args.save
    with open(output_path, "w") as f:
        json.dump([report_to_dict(r) for r in reports], f, indent=2, default=str)
    print(f"  Results saved to: {output_path}")


if __name__ == "__main__":
    main()
