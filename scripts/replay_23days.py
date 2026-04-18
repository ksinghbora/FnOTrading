"""Replay recorded chain snapshots through every strategy.

Driver for ReplayBacktestEngine — uses real recorded option prices instead
of synthetic BS pricing. This is the most realistic backtest mode we have
short of live capital.

Spot + VIX are reconstructed from the chain by
`scripts/extract_spot_vix_from_chain.py`. After the Apr 17 audit, that
script prefers the real India VIX feed when available and falls back to
the ATM-IV proxy with a `source` tag so the proxy bias (~50-70 % high on
weekly options near expiry) doesn't silently leak into VIX-gated logic.

The replay engine itself now refuses to run on `degraded` chain CSVs and
warns on `partial` ones (LTP-only, no bid/ask depth) — see
`scripts/audit_chain_quality.py` for the per-day classification.

Usage:
    uv run python scripts/audit_chain_quality.py        # see what's available
    uv run python scripts/extract_spot_vix_from_chain.py  # rebuild spot+vix
    uv run python scripts/replay_23days.py              # this script
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Make `src` importable when running this script directly via uv run
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# CRITICAL: disable PAPER_TRADING shadow-mode override BEFORE importing the
# strategy modules. With PAPER_TRADING=true, portfolio_strategy.py line 450
# turns "score below threshold" into "enter anyway" — that's a data-collection
# feature, not a real strategy. Replay should measure properly-gated behavior.
os.environ["PAPER_TRADING"] = "false"

from src.backtest.replay_engine import ReplayBacktestEngine  # noqa: E402

CAPITAL = 1_000_000  # ₹10 lakh, matches paper broker default
SNAPSHOT_DIR = "data/chain_snapshots"
SPOT_CSV = "data/nifty_spot_minute_chain.csv"
VIX_CSV = "data/india_vix_minute_chain.csv"

STRATEGIES = [
    "portfolio",
    "iron_condor",
    "short_strangle",
    "short_straddle",
    "trend_debit_spread",
]


async def run_one(name: str) -> dict:
    engine = ReplayBacktestEngine()
    result = await engine.run(
        strategy_name=name,
        snapshot_dir=SNAPSHOT_DIR,
        spot_csv=SPOT_CSV,
        vix_csv=VIX_CSV,
        initial_capital=CAPITAL,
    )
    return result


def fmt_inr(x: float) -> str:
    return f"₹{x:>+12,.2f}"


def pct_of_capital(x: float) -> str:
    return f"{(x / CAPITAL) * 100:>+6.3f}%"


def get_metric(result: dict, *keys, default=0):
    """Look in result['metrics'] first, then top-level."""
    metrics = result.get("metrics", {})
    for k in keys:
        if k in metrics:
            return metrics[k]
        if k in result:
            return result[k]
    return default


async def main() -> int:
    n_files = len(list(Path(SNAPSHOT_DIR).glob("chain_*.csv")))
    print(f"\nReplay backtest across all chain-snapshot days (capital ₹{CAPITAL:,})")
    print(f"Snapshot dir: {SNAPSHOT_DIR} ({n_files} CSV files)")
    print(f"Spot CSV:     {SPOT_CSV}")
    print(f"VIX CSV:      {VIX_CSV}\n")

    results: dict[str, dict] = {}
    for name in STRATEGIES:
        print(f"  running {name:.<25}", end=" ", flush=True)
        try:
            r = await run_one(name)
            results[name] = r
            net = get_metric(r, "total_pnl", "net_pnl")
            trades = get_metric(r, "num_trades", "total_trades", "trades")
            print(f"{trades:>4} trades  net={fmt_inr(net)}")
        except Exception as e:
            print(f"FAILED: {type(e).__name__}: {e}")
            results[name] = {"error": str(e)}

    print()
    print("=" * 110)
    print(
        f"{'Strategy':<22} {'Days':>5} {'Trades':>7} {'Win%':>7} "
        f"{'Gross':>14} {'Charges':>13} {'Net P&L':>14} {'%Cap':>9} "
        f"{'Sharpe':>8} {'MaxDD%':>8}"
    )
    print("-" * 110)

    total_net = 0.0
    total_charges = 0.0
    total_trades = 0
    for name in STRATEGIES:
        r = results.get(name, {})
        if "error" in r:
            print(f"{name:<22} FAILED: {r['error']}")
            continue
        m = r.get("metrics", {})
        days = m.get("num_days", r.get("num_days", 0))
        trades = m.get("num_trades", 0)
        win_rate = m.get("win_rate", 0)
        net = m.get("total_pnl", 0)
        charges = m.get("total_charges", 0)
        gross = net + charges  # net_pnl already nets charges; reverse it for display
        sharpe = m.get("sharpe_ratio", 0)
        maxdd = m.get("max_drawdown_pct", 0)
        total_net += net
        total_charges += charges
        total_trades += trades
        print(
            f"{name:<22} {days:>5} {trades:>7} {win_rate:>6.1f}% "
            f"{fmt_inr(gross)} {fmt_inr(-charges)} {fmt_inr(net)} {pct_of_capital(net)} "
            f"{sharpe:>8.2f} {maxdd:>7.2f}%"
        )

    print("-" * 110)
    print(
        f"{'TOTAL (parallel)':<22} {'':>5} {total_trades:>7} {'':>7} "
        f"{'':>14} {fmt_inr(-total_charges)} {fmt_inr(total_net)} {pct_of_capital(total_net)}"
    )
    # Chain quality summary — pull from any successful result (all use the
    # same underlying chain dir, so the report is identical across strategies)
    quality_report: dict[str, str] | None = None
    for r in results.values():
        if "chain_quality" in r:
            quality_report = r["chain_quality"]
            break
    if quality_report:
        from collections import Counter
        status_counts = Counter(quality_report.values())
        print()
        print(
            "Chain quality: "
            + " ".join(f"{s}={n}" for s, n in sorted(status_counts.items()))
        )
        skipped = [d for d, s in quality_report.items()
                   if s in ("degraded", "weekend", "missing")]
        if skipped:
            print(f"Skipped days ({len(skipped)}): {', '.join(sorted(skipped))}")

    print()
    print("Notes:")
    print("- Each row is INDEPENDENT (own ₹10L capital). TOTAL assumes 5 separate pools.")
    print("- Spot reconstructed from chain via put-call parity at ATM (T≈0 approx).")
    print("- VIX prefers real India VIX feed; falls back to ATM-IV proxy where missing.")
    print("- Degraded / weekend chain days are skipped (Apr 17 quality gate).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
