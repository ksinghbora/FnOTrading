"""CLI wrapper around :func:`src.backtest.day_replay.replay_day`.

Replays one trading day with frozen-day-of params, prints a one-line
human summary to stderr and the full JSON DayReplayResult to stdout.

The stdout/stderr split is intentional: pipe stdout into ``jq`` or save
it to a file, and you still see the human summary in your terminal.

Usage
-----

    # Replay 2026-04-15 with the portfolio strategy
    uv run python scripts/replay_day.py --date 2026-04-15

    # Replay every clean day in the recorded corpus, save full results
    for d in $(ls data/chain_snapshots/chain_*.csv | grep -oE '\\d{4}-\\d{2}-\\d{2}'); do
        uv run python scripts/replay_day.py --date "$d" \\
            --strategy portfolio \\
            > "data/replays/$d.json"
    done

    # Same date, different seed — should produce identical hash
    uv run python scripts/replay_day.py --date 2026-04-15 --seed 42

Exit codes
----------
  0  replay completed (may include "skipped: degraded" days — see result)
  1  engine returned an error (no chain CSV, no spot data, etc)
  2  CLI argument or environment problem

See ``docs/DATA_RELIABILITY_PLAN.md`` §7 for the design.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

# Make `src` importable when running this script via `uv run`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# CRITICAL: same shadow-mode override as scripts/replay_23days.py — with
# PAPER_TRADING=true, portfolio_strategy.py turns "score below threshold"
# into "enter anyway" for data-collection. Replay should measure properly-
# gated behavior, so force this OFF before importing strategy modules.
os.environ.setdefault("PAPER_TRADING", "false")

from src.backtest.day_replay import (  # noqa: E402
    DEFAULT_CHAIN_DIR,
    DEFAULT_RUNS_LOG,
    DEFAULT_SNAPSHOT_ROOT,
    DEFAULT_SPOT_CSV,
    DEFAULT_VIX_CSV,
    DayReplayResult,
    replay_day,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--date", required=True, type=date.fromisoformat,
        help="Trading day to replay (YYYY-MM-DD)",
    )
    p.add_argument(
        "--strategy", default="portfolio",
        help="Registered strategy name (default: portfolio)",
    )
    p.add_argument(
        "--underlying", default="NIFTY",
        help="Underlying instrument (default: NIFTY)",
    )
    p.add_argument(
        "--seed", type=int, default=0,
        help="RNG seed pinned for determinism (default: 0)",
    )
    p.add_argument(
        "--snapshot-root", type=Path, default=DEFAULT_SNAPSHOT_ROOT,
        help=f"Frozen-config root (default: {DEFAULT_SNAPSHOT_ROOT})",
    )
    p.add_argument(
        "--chain-dir", type=Path, default=DEFAULT_CHAIN_DIR,
        help=f"Chain snapshot CSV directory (default: {DEFAULT_CHAIN_DIR})",
    )
    p.add_argument(
        "--spot-csv", type=Path, default=DEFAULT_SPOT_CSV,
        help=f"Spot minute CSV (default: {DEFAULT_SPOT_CSV})",
    )
    p.add_argument(
        "--vix-csv", type=Path, default=DEFAULT_VIX_CSV,
        help=f"India VIX minute CSV (default: {DEFAULT_VIX_CSV})",
    )
    p.add_argument(
        "--initial-capital", type=float, default=1_000_000,
        help="Starting capital for the replay (default: ₹10 lakh)",
    )
    p.add_argument(
        "--no-runs-log", action="store_true",
        help="Skip appending to data/replay_runs.jsonl audit trail",
    )
    p.add_argument(
        "--accept-degraded", action="store_true",
        help="Don't skip days flagged DEGRADED by audit_chain_quality",
    )
    p.add_argument(
        "--reject-partial", action="store_true",
        help="Skip days flagged PARTIAL (LTP-only, no bid/ask)",
    )
    p.add_argument(
        "--quiet", action="store_true",
        help="Suppress INFO logging on stderr (errors still shown)",
    )
    return p.parse_args()


def _print_summary(result: DayReplayResult) -> None:
    """One-line human summary to stderr — for the operator watching the run."""
    if result.error:
        sys.stderr.write(
            f"[REPLAY] {result.date} {result.strategy}/{result.underlying} "
            f"FAILED: {result.error}\n"
        )
        return
    sys.stderr.write(
        f"[REPLAY] {result.date} {result.strategy}/{result.underlying} "
        f"pnl={result.total_pnl:+,.0f} "
        f"snap_cov={result.snapshot_coverage_pct:.0f}% "
        f"params={result.params_source} "
        f"hash={result.replay_hash[:12]} "
        f"code={result.code_sha[:8] if result.code_sha else 'unknown'}\n"
    )


async def _amain(args: argparse.Namespace) -> int:
    runs_log = None if args.no_runs_log else DEFAULT_RUNS_LOG
    result = await replay_day(
        target_date=args.date,
        strategy_name=args.strategy,
        underlying=args.underlying,
        seed=args.seed,
        snapshot_root=args.snapshot_root,
        chain_dir=args.chain_dir,
        spot_csv=args.spot_csv,
        vix_csv=args.vix_csv,
        initial_capital=args.initial_capital,
        runs_log=runs_log,
        skip_degraded=not args.accept_degraded,
        accept_partial=not args.reject_partial,
    )

    _print_summary(result)
    # Full result on stdout — pipeable to jq, redirectable to file
    json.dump(asdict(result), sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")
    return 1 if result.error else 0


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
