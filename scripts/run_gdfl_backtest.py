"""Run backtest for one or more strategies using real GDFL tick data.

Reads per-day parquet files produced by `scripts/extract_gdfl.py`, feeds
them through `BacktestEngine` via `GDFLMarketSource`. Same metrics and
output format as the synthetic backtest, but numbers are trustworthy.

Usage:
    uv run python scripts/run_gdfl_backtest.py \
        --strategy short_strangle \
        --from 2026-02-01

    uv run python scripts/run_gdfl_backtest.py --all --from 2026-02-01
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
from src.backtest.gdfl_market_source import GDFLMarketSource
from src.market_data.simulator import BANKNIFTY_SPOT_TOKEN, NIFTY_SPOT_TOKEN
from src.strategy.registry import list_strategies

STRATEGIES = ["short_straddle", "short_strangle", "iron_condor", "delta_neutral"]


def parse_args():
    p = argparse.ArgumentParser(description="Run backtest on GDFL real data")
    p.add_argument("--strategy", "-s", default="short_strangle",
                   help="Strategy name (or use --all)")
    p.add_argument("--all", action="store_true",
                   help="Run all four registered strategies sequentially")
    p.add_argument("--underlying", "-u", default="NIFTY",
                   choices=["NIFTY", "BANKNIFTY"])
    p.add_argument("--from", dest="from_date", default="",
                   help="Start date YYYY-MM-DD (default: earliest available)")
    p.add_argument("--days", "-d", type=int, default=365,
                   help="Max days to run (default: all available in range)")
    p.add_argument("--capital", "-c", type=float, default=1_000_000)
    p.add_argument("--lots", type=int, default=1)
    p.add_argument("--parquet-dir", default="data/gdfl_snapshots/")
    p.add_argument("--out-dir", default="data/backtest_runs/")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


async def run_one(strategy: str, args, spot_token: int) -> dict:
    source = GDFLMarketSource(args.parquet_dir, args.underlying, spot_token)
    days = source.available_days()
    if not days:
        return {"strategy": strategy, "error": "No GDFL parquet available"}

    start = date.fromisoformat(args.from_date) if args.from_date else None

    engine = BacktestEngine()
    results = await engine.run(
        strategy_name=strategy,
        strategy_params={
            "underlying": args.underlying,
            "quantity_lots": args.lots,
        },
        num_days=args.days,
        start_date=start,
        initial_capital=args.capital,
        market_source=source,
    )
    return results


def print_summary(strategy: str, results: dict) -> None:
    if "error" in results:
        print(f"\n{strategy}: ERROR — {results['error']}")
        return
    m = results["metrics"]
    print(f"\n── {strategy} ─────────────────────────────────────")
    print(f"  Period:       {results.get('period', 'n/a')}")
    print(f"  Days:         {results.get('num_days', 0)}")
    print(f"  Total P&L:    Rs {m.get('total_pnl', 0):>12,.2f}")
    print(f"  Charges:      Rs {m.get('total_charges', 0):>12,.2f}")
    print(f"  Max DD:       Rs {m.get('max_drawdown', 0):>12,.2f}")
    print(f"  Sharpe:       {m.get('sharpe_ratio', 0):>11.2f}")
    print(f"  Win Rate:     {m.get('win_rate', 0):>11.1f}%")
    print(f"  Trades:       {m.get('num_trades', 0):>11d}")


def main():
    args = parse_args()

    log_dir = Path("data/backtest_logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    tag = "all" if args.all else args.strategy
    log_file = log_dir / f"gdfl_{tag}_{date.today().isoformat()}.log"

    handlers = [logging.FileHandler(log_file, mode="w")]
    if args.verbose:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
    if not args.verbose:
        for noisy in ("src.portfolio.positions", "src.portfolio.pnl", "src.broker.paper"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    print(f"Logs: {log_file}")

    _import_strategies()
    registered = set(list_strategies())

    spot_token = NIFTY_SPOT_TOKEN if args.underlying == "NIFTY" else BANKNIFTY_SPOT_TOKEN

    strategies = STRATEGIES if args.all else [args.strategy]
    strategies = [s for s in strategies if s in registered]
    if not strategies:
        sys.exit(f"No valid strategies. Registered: {sorted(registered)}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_results = {}
    for strategy in strategies:
        print(f"\n{'=' * 60}\nRunning {strategy} on GDFL real data\n{'=' * 60}")
        results = asyncio.run(run_one(strategy, args, spot_token))
        all_results[strategy] = results

        # Per-strategy file
        today = date.today().isoformat()
        out_file = out_dir / f"gdfl_{strategy}_{today}.json"
        with open(out_file, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print_summary(strategy, results)
        print(f"\n  Saved: {out_file}")

    # Combined summary file
    combined = out_dir / f"gdfl_summary_{date.today().isoformat()}.json"
    with open(combined, "w") as f:
        json.dump(
            {s: r.get("metrics", {"error": r.get("error")}) for s, r in all_results.items()},
            f, indent=2, default=str,
        )
    print(f"\nCombined summary: {combined}")


if __name__ == "__main__":
    main()
