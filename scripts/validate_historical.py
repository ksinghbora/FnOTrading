"""DEPRECATED: real spot/VIX but BS-priced options. Use replay for new tuning.

Apr 2026: this script uses the real NIFTY+VIX minute CSVs but still derives
option prices via Black-Scholes. The chain-replay engine
(`scripts/replay_23days.py`) uses real recorded option prices and is the
preferred path. Kept here because the historical spot+VIX series is much
longer (~123 trading days back to Sep 2025) than the chain corpus, so it
remains the best option for long-window regime work until we have a full
year of recorded chains.

Walk-forward validation using real NIFTY + VIX historical data.

Prerequisite: Run download_spot_data.py first to get CSV files.

Usage:
    uv run python scripts/validate_historical.py
    uv run python scripts/validate_historical.py --strategies iron_condor short_straddle
    uv run python scripts/validate_historical.py --train-days 60 --test-days 30
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtest.engine import _import_strategies
from src.backtest.historical_engine import HistoricalBacktestEngine, _load_spot_csv


def parse_args():
    parser = argparse.ArgumentParser(description="Historical Walk-Forward Validation")
    parser.add_argument("--strategies", nargs="+", default=None)
    parser.add_argument("--spot-csv", default=None, help="Path to spot CSV (auto-detected if omitted)")
    parser.add_argument("--vix-csv", default=None, help="Path to VIX CSV (auto-detected if omitted)")
    parser.add_argument("--train-days", type=int, default=60)
    parser.add_argument("--test-days", type=int, default=30)
    parser.add_argument("--step-days", type=int, default=None)
    parser.add_argument("--capital", type=float, default=1_000_000)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--save", default="historical_validation.json")
    return parser.parse_args()


def find_data_files() -> tuple[Path | None, Path | None]:
    """Auto-detect spot and VIX CSV files in data/ directory."""
    data_dir = Path(__file__).parent.parent / "data"
    spot = None
    vix = None
    for f in sorted(data_dir.glob("*_spot_*.csv")):
        spot = f
        break
    for f in sorted(data_dir.glob("india_vix_*.csv")):
        vix = f
        break
    return spot, vix


async def run_walk_forward_historical(
    strategies: list[str],
    spot_csv: Path,
    vix_csv: Path | None,
    train_days: int,
    test_days: int,
    step_days: int,
    capital: float,
):
    """Run walk-forward using real historical data."""
    _import_strategies()
    engine = HistoricalBacktestEngine()

    # Load spot data to determine trading days
    spot_data = _load_spot_csv(spot_csv)
    all_days = sorted(spot_data.keys())
    total_days = len(all_days)

    print(f"  Data range: {all_days[0]} to {all_days[-1]} ({total_days} trading days)")
    print(f"  Train: {train_days}d, Test: {test_days}d, Step: {step_days}d")

    # Build windows
    windows: list[tuple[list[date], list[date]]] = []
    idx = 0
    while idx + train_days + test_days <= total_days:
        train_slice = all_days[idx: idx + train_days]
        test_slice = all_days[idx + train_days: idx + train_days + test_days]
        windows.append((train_slice, test_slice))
        idx += step_days

    if not windows:
        print(f"  ERROR: Not enough data for walk-forward. Need {train_days + test_days} days, have {total_days}")
        return {}

    print(f"  Windows: {len(windows)}")
    print()

    all_results = {}

    for strat in strategies:
        print(f"\n{'=' * 70}")
        print(f"  {strat.upper()} — Historical Walk-Forward")
        print(f"{'=' * 70}")

        train_pnls = []
        test_pnls = []
        test_daily_pnls = []

        for win_idx, (train_slice, test_slice) in enumerate(windows):
            # Train window
            train_result = await engine.run(
                strategy_name=strat,
                spot_csv=spot_csv,
                vix_csv=vix_csv,
                strategy_id=f"{strat}_hist_train_{win_idx}",
                strategy_params={"underlying": "NIFTY", "quantity_lots": 1},
                num_days=len(train_slice),
                start_date=train_slice[0],
                initial_capital=capital,
            )

            # Test window
            test_result = await engine.run(
                strategy_name=strat,
                spot_csv=spot_csv,
                vix_csv=vix_csv,
                strategy_id=f"{strat}_hist_test_{win_idx}",
                strategy_params={"underlying": "NIFTY", "quantity_lots": 1},
                num_days=len(test_slice),
                start_date=test_slice[0],
                initial_capital=capital,
            )

            train_pnl = train_result.get("final_pnl", 0)
            test_pnl = test_result.get("final_pnl", 0)
            train_pnls.append(train_pnl)
            test_pnls.append(test_pnl)

            for d in test_result.get("daily_results", []):
                test_daily_pnls.append(d["pnl"])

            verdict = "PASS" if test_pnl > 0 else "FAIL"
            print(
                f"  Window {win_idx + 1}: "
                f"train {train_slice[0]}..{train_slice[-1]} = {train_pnl:>+10,.0f}  |  "
                f"test {test_slice[0]}..{test_slice[-1]} = {test_pnl:>+10,.0f}  {verdict}"
            )

        # Aggregate
        test_total = sum(test_pnls)
        test_mean = test_total / len(test_pnls) if test_pnls else 0
        train_mean = sum(train_pnls) / len(train_pnls) if train_pnls else 0
        test_wins = sum(1 for p in test_pnls if p > 0)
        test_win_rate = test_wins / len(test_pnls) * 100 if test_pnls else 0

        degradation = 0
        if train_mean > 0:
            degradation = (train_mean - test_mean) / train_mean * 100

        # Sharpe from daily
        import numpy as np, math
        if test_daily_pnls:
            arr = np.array(test_daily_pnls)
            sharpe = float(arr.mean() / arr.std() * math.sqrt(252)) if arr.std() > 0 else 0
            peak = np.maximum.accumulate(capital + np.cumsum(arr))
            dd = (capital + np.cumsum(arr)) - peak
            max_dd = float(np.min(dd))
        else:
            sharpe = max_dd = 0

        print(f"\n  {'Summary':^60}")
        print(f"  {'-' * 50}")
        print(f"  Test Total P&L:       Rs {test_total:>12,.2f}")
        print(f"  Test Mean P&L/window: Rs {test_mean:>12,.2f}")
        print(f"  Test Win Rate:        {test_win_rate:>11.0f}%  ({test_wins}/{len(test_pnls)})")
        print(f"  Test Sharpe (ann.):   {sharpe:>11.2f}")
        print(f"  Test Max Drawdown:    Rs {max_dd:>12,.2f}")
        print(f"  Train Mean P&L:       Rs {train_mean:>12,.2f}")
        print(f"  Degradation:          {degradation:>11.1f}%")

        if degradation > 80 or test_mean <= 0:
            v = "FAIL"
        elif degradation > 50:
            v = "CAUTION"
        elif test_win_rate >= 50 and test_mean > 0:
            v = "PASS"
        else:
            v = "MARGINAL"
        print(f"  Verdict:              {v}")

        all_results[strat] = {
            "test_total_pnl": round(test_total, 2),
            "test_mean_pnl": round(test_mean, 2),
            "test_win_rate": round(test_win_rate, 1),
            "test_sharpe": round(sharpe, 2),
            "test_max_drawdown": round(max_dd, 2),
            "train_mean_pnl": round(train_mean, 2),
            "degradation_pct": round(degradation, 1),
            "num_windows": len(windows),
            "windows": [
                {"train_pnl": round(tp, 2), "test_pnl": round(ep, 2)}
                for tp, ep in zip(train_pnls, test_pnls)
            ],
            "verdict": v,
        }

    # Final summary
    print(f"\n\n{'=' * 80}")
    print(f"  {'HISTORICAL WALK-FORWARD SUMMARY (Real NIFTY Data)':^76}")
    print(f"{'=' * 80}")
    print(f"\n  {'Strategy':<25} {'Test P&L':>12} {'Win%':>8} {'Sharpe':>8} {'Degrade%':>10} {'Verdict':>10}")
    print(f"  {'-' * 75}")
    for strat, r in all_results.items():
        print(
            f"  {strat:<25} {r['test_total_pnl']:>+12,.0f} "
            f"{r['test_win_rate']:>7.0f}% {r['test_sharpe']:>8.2f} "
            f"{r['degradation_pct']:>9.1f}% {r['verdict']:>10}"
        )
    print(f"{'=' * 80}\n")

    return all_results


def main():
    args = parse_args()

    level = logging.WARNING if not args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    strategies = args.strategies or ["short_straddle", "short_strangle", "iron_condor", "trend_debit_spread"]

    # Find data files
    if args.spot_csv:
        spot_csv = Path(args.spot_csv)
    else:
        spot_csv, _ = find_data_files()

    if args.vix_csv:
        vix_csv = Path(args.vix_csv)
    else:
        _, vix_csv = find_data_files()

    if not spot_csv or not spot_csv.exists():
        print("ERROR: No spot data found. Run first:")
        print("  uv run python scripts/download_spot_data.py")
        sys.exit(1)

    step_days = args.step_days or args.test_days

    print("=" * 80)
    print("  FnO Trading — Historical Walk-Forward Validation")
    print("=" * 80)
    print(f"  Strategies:   {', '.join(strategies)}")
    print(f"  Spot data:    {spot_csv}")
    print(f"  VIX data:     {vix_csv or 'not found (will use default VIX)'}")
    print(f"  Train/Test:   {args.train_days}d / {args.test_days}d")
    print(f"  Step:         {step_days}d")
    print()

    results = asyncio.run(run_walk_forward_historical(
        strategies, spot_csv, vix_csv,
        args.train_days, args.test_days, step_days, args.capital,
    ))

    output = Path(__file__).parent.parent / args.save
    with open(output, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"  Results saved to: {output}")


if __name__ == "__main__":
    main()
