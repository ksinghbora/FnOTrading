"""Run multi-seed backtest for all strategies in a single process.

Avoids per-run Python startup overhead (~1s each).

Usage:
    uv run python scripts/multi_seed_backtest.py
    uv run python scripts/multi_seed_backtest.py --seeds 20 --days 30
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtest.engine import BacktestEngine, _import_strategies


async def run_all(strategies: list[str], num_seeds: int, num_days: int):
    _import_strategies()
    engine = BacktestEngine()

    results: dict[str, list[dict]] = {}

    for strat in strategies:
        results[strat] = []
        for seed in range(1, num_seeds + 1):
            r = await engine.run(
                strategy_name=strat,
                strategy_params={"underlying": "NIFTY", "quantity_lots": 1},
                num_days=num_days,
                seed=seed,
            )
            pnl = r["final_pnl"]
            results[strat].append({"seed": seed, "pnl": pnl, "metrics": r["metrics"]})
            print(f"  {strat} seed={seed:>2}: {pnl:>+12,.2f}")

    # Summary
    print("\n" + "=" * 70)
    print(f"  {'Strategy':<20} {'Mean P&L':>12} {'Win':>5} {'Best':>12} {'Worst':>12} {'StdDev':>10}")
    print("  " + "-" * 65)

    for strat in strategies:
        pnls = [r["pnl"] for r in results[strat]]
        mean = sum(pnls) / len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        best = max(pnls)
        worst = min(pnls)
        std = (sum((p - mean) ** 2 for p in pnls) / len(pnls)) ** 0.5
        print(
            f"  {strat:<20} {mean:>+12,.0f} {wins:>3}/{len(pnls):>1} "
            f"{best:>+12,.0f} {worst:>+12,.0f} {std:>10,.0f}"
        )

    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Multi-seed backtest")
    parser.add_argument("--seeds", type=int, default=10, help="Number of seeds (default: 10)")
    parser.add_argument("--days", type=int, default=30, help="Trading days per seed (default: 30)")
    parser.add_argument("--strategies", nargs="+", default=None, help="Strategies to test")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    strategies = args.strategies or [
        "short_straddle", "short_strangle", "iron_condor", "delta_neutral"
    ]

    print("=" * 70)
    print(f"  Multi-Seed Validation: {args.seeds} seeds x {args.days} days")
    print(f"  Strategies: {', '.join(strategies)}")
    print("=" * 70)

    asyncio.run(run_all(strategies, args.seeds, args.days))


if __name__ == "__main__":
    main()
