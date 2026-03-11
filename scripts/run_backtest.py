"""Run a backtest for any registered strategy with synthetic NIFTY data.

Uses BacktestEngine which wires the same pipeline as live trading
(TickFeedManager, OptionChainBuilder, PaperBrokerClient, PortfolioManager)
but with synthetic data and direct method calls for speed.

Usage:
    uv run python scripts/run_backtest.py
    uv run python scripts/run_backtest.py --strategy short_strangle --days 60
    uv run python scripts/run_backtest.py --strategy iron_condor --capital 2000000
    uv run python scripts/run_backtest.py --strategy short_straddle --days 30 --seed 99
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtest.engine import BacktestEngine, _import_strategies
from src.strategy.registry import list_strategies


def parse_args():
    parser = argparse.ArgumentParser(description="FnO Trading — Strategy Backtest")
    parser.add_argument(
        "--strategy", "-s",
        default="short_straddle",
        help="Strategy name (default: short_straddle)",
    )
    parser.add_argument(
        "--days", "-d",
        type=int, default=30,
        help="Number of trading days (default: 30)",
    )
    parser.add_argument(
        "--capital", "-c",
        type=float, default=1_000_000,
        help="Initial capital (default: 1,000,000)",
    )
    parser.add_argument(
        "--seed",
        type=int, default=42,
        help="Random seed for reproducibility (default: 42)",
    )
    parser.add_argument(
        "--underlying", "-u",
        default="NIFTY",
        help="Underlying instrument (default: NIFTY)",
    )
    parser.add_argument(
        "--lots",
        type=int, default=1,
        help="Number of lots (default: 1)",
    )
    parser.add_argument(
        "--tick-interval",
        type=int, default=1,
        help="Minutes between ticks, 1=accurate, 5=fast (default: 1)",
    )
    parser.add_argument(
        "--start-date",
        type=str, default="",
        help="Start date YYYY-MM-DD (default: ~45 days ago)",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List available strategies and exit",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable verbose logging",
    )
    return parser.parse_args()


async def run_backtest(args):
    engine = BacktestEngine()

    start = None
    if args.start_date:
        start = date.fromisoformat(args.start_date)

    results = await engine.run(
        strategy_name=args.strategy,
        strategy_params={
            "underlying": args.underlying,
            "quantity_lots": args.lots,
        },
        num_days=args.days,
        start_date=start,
        initial_capital=args.capital,
        seed=args.seed,
        tick_interval_minutes=args.tick_interval,
    )

    return results


def print_results(results: dict):
    if "error" in results:
        print(f"\nError: {results['error']}")
        return

    m = results["metrics"]

    print("\n" + "=" * 60)
    print(f"  Strategy:  {results['strategy']}")
    print(f"  Underlying: {results['underlying']}")
    print(f"  Period:    {results['period']}")
    print(f"  Days:      {results['num_days']}")
    print(f"  Lots:      {results['lots']} ({results['lots'] * results['lot_size']} qty)")
    print("=" * 60)

    print("\n  Performance Metrics")
    print("  " + "-" * 40)
    print(f"  Total P&L:      Rs {m.get('total_pnl', 0):>12,.2f}")
    print(f"  Total Charges:  Rs {m.get('total_charges', 0):>12,.2f}")
    print(f"  Return:         {m.get('total_return_pct', 0):>11.2f}%")
    print(f"  Max Drawdown:   Rs {m.get('max_drawdown', 0):>12,.2f}")
    print(f"  Sharpe Ratio:   {m.get('sharpe_ratio', 0):>11.2f}")
    print(f"  Sortino Ratio:  {m.get('sortino_ratio', 0):>11.2f}")
    print(f"  Calmar Ratio:   {m.get('calmar_ratio', 0):>11.2f}")
    print(f"  Win Rate:       {m.get('win_rate', 0):>11.1f}%")
    print(f"  Num Trades:     {m.get('num_trades', 0):>11d}")
    print(f"  Avg Win:        Rs {m.get('avg_win', 0):>12,.2f}")
    print(f"  Avg Loss:       Rs {m.get('avg_loss', 0):>12,.2f}")

    pf = m.get('profit_factor', 0)
    if isinstance(pf, str):
        print(f"  Profit Factor:  {'inf':>11}")
    else:
        print(f"  Profit Factor:  {pf:>11.2f}")
    print("  " + "-" * 40)

    # Daily breakdown
    daily = results.get("daily_results", [])
    if daily:
        print(f"\n  {'Date':<12} {'Day':<10} {'Open':>8} {'Close':>8} {'P&L':>10} {'Trades':>7}")
        print("  " + "-" * 60)
        for d in daily:
            pnl_str = f"Rs {d['pnl']:>8,.0f}"
            print(
                f"  {d['date']:<12} {d['day_of_week']:<10} "
                f"{d['spot_open']:>8,.0f} {d['spot_close']:>8,.0f} "
                f"{pnl_str:>10} {d['trades']:>7}"
            )

    print(f"\n  Final P&L: Rs {results['final_pnl']:>12,.2f}")
    if results.get("equity_curve"):
        print(f"  Final Equity: Rs {results['equity_curve'][-1]['equity']:>12,.2f}")


def main():
    args = parse_args()

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet noisy loggers in non-verbose mode
    if not args.verbose:
        logging.getLogger("src.portfolio.positions").setLevel(logging.WARNING)
        logging.getLogger("src.portfolio.pnl").setLevel(logging.WARNING)
        logging.getLogger("src.broker.paper").setLevel(logging.WARNING)

    _import_strategies()

    if args.list:
        strategies = list_strategies()
        print("Available strategies:")
        for s in strategies:
            print(f"  - {s}")
        return

    print("=" * 60)
    print("FnO Trading System -- Strategy Backtest")
    print("=" * 60)
    print(f"  Strategy:       {args.strategy}")
    print(f"  Underlying:     {args.underlying}")
    print(f"  Days:           {args.days}")
    print(f"  Capital:        Rs {args.capital:,.0f}")
    print(f"  Lots:           {args.lots}")
    print(f"  Seed:           {args.seed}")
    print(f"  Tick Interval:  {args.tick_interval}m")
    print()

    results = asyncio.run(run_backtest(args))

    print_results(results)

    # Save results
    output_path = Path(__file__).parent.parent / "backtest_results.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n  Results saved to: {output_path}")


if __name__ == "__main__":
    main()
