"""Daily decision-log roll-up — compute per-strategy P&L from data/decisions/*.

Apr 18 plan-review companion. The shadow-mode runner gate (commit ee945ac)
makes it cheap to run challenger strategies alongside the live champion;
the data they produce is only useful if we can read it. This script
ingests `data/decisions/decisions_YYYY-MM-DD.csv` and produces a daily
P&L roll-up per strategy_id, distinguishing live champion from shadow
challengers.

Key facts about the decision log:
  • Each row is one ENTER / EXIT / SKIP / EVAL decision.
  • outcome_pnl is populated on EXIT rows only (entry P&L is unknown
    until exit). The strategy computes pnl from chain prices internally,
    so SHADOW strategies get hypothetical P&L for free — same code path
    as live, just no OMS routing.
  • Rows can duplicate when multiple replay runs append to the same file.
    Dedupe by (timestamp, strategy_id, leg, decision) before aggregating.

Usage:
    uv run python scripts/decision_rollup.py                  # last 30 days
    uv run python scripts/decision_rollup.py --days 60
    uv run python scripts/decision_rollup.py --start 2026-04-01 --end 2026-04-17
    uv run python scripts/decision_rollup.py --by-strategy    # totals per strategy
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DECISIONS_DIR = ROOT / "data" / "decisions"


@dataclass
class DayStrategyStats:
    day: date
    strategy_id: str
    n_evals: int = 0
    n_enters: int = 0
    n_exits: int = 0
    n_skips: int = 0
    pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    held_minutes_sum: int = 0
    by_exit_reason: dict[str, int] = None  # populated lazily

    def add_exit(self, pnl: float, reason: str, held: int) -> None:
        self.n_exits += 1
        self.pnl += pnl
        if pnl > 0:
            self.wins += 1
        elif pnl < 0:
            self.losses += 1
        self.held_minutes_sum += held
        if self.by_exit_reason is None:
            self.by_exit_reason = defaultdict(int)
        self.by_exit_reason[reason] += 1

    @property
    def win_rate(self) -> float:
        n = self.wins + self.losses
        return (self.wins / n * 100) if n else 0.0

    @property
    def avg_held(self) -> float:
        return (self.held_minutes_sum / self.n_exits) if self.n_exits else 0.0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--days", type=int, default=30,
                   help="lookback window when --start/--end omitted (default 30)")
    p.add_argument("--start", type=str, help="YYYY-MM-DD")
    p.add_argument("--end", type=str, help="YYYY-MM-DD")
    p.add_argument("--by-strategy", action="store_true",
                   help="Aggregate across days, one row per strategy")
    p.add_argument("--strategy", type=str, default=None,
                   help="Filter to one strategy_id (substring match)")
    p.add_argument("--decisions-dir", type=str, default=str(DECISIONS_DIR),
                   help="Override decision log directory")
    return p.parse_args()


def _date_range(args: argparse.Namespace) -> list[date]:
    if args.start and args.end:
        s = date.fromisoformat(args.start)
        e = date.fromisoformat(args.end)
    elif args.start:
        s = date.fromisoformat(args.start)
        e = date.today()
    else:
        e = date.today()
        s = e - timedelta(days=args.days)
    out = []
    cur = s
    while cur <= e:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def _load_day(path: Path) -> list[dict]:
    """Read one decision CSV. Dedupe by (ts, strategy, leg, decision)."""
    if not path.exists():
        return []
    seen: set[tuple] = set()
    rows: list[dict] = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for r in reader:
            key = (r.get("timestamp", ""), r.get("strategy_id", ""),
                   r.get("leg", ""), r.get("decision", ""))
            if key in seen:
                continue
            seen.add(key)
            rows.append(r)
    return rows


def _aggregate(rows: list[dict], day: date) -> dict[str, DayStrategyStats]:
    by_strat: dict[str, DayStrategyStats] = {}
    for r in rows:
        sid = r.get("strategy_id", "?")
        decision = r.get("decision", "")
        stat = by_strat.setdefault(sid, DayStrategyStats(day=day, strategy_id=sid))
        stat.n_evals += 1
        if decision == "ENTER":
            stat.n_enters += 1
        elif decision == "SKIP":
            stat.n_skips += 1
        elif decision == "EXIT":
            try:
                pnl = float(r.get("outcome_pnl", "") or 0.0)
            except ValueError:
                pnl = 0.0
            try:
                held = int(r.get("held_minutes", "") or 0)
            except ValueError:
                held = 0
            stat.add_exit(pnl, r.get("exit_reason", "?"), held)
    return by_strat


def _is_shadow(strategy_id: str) -> bool:
    """Heuristic: id ending in '_shadow' is a shadow challenger.
    Shadow strategies still log decisions but never routed orders."""
    return strategy_id.endswith("_shadow") or "_shadow_" in strategy_id


def _print_daily(per_day: dict[date, dict[str, DayStrategyStats]],
                 strategy_filter: str | None) -> None:
    days = sorted(per_day.keys())
    if not days:
        print("No decision logs found in window.")
        return

    print(f"\nDaily roll-up ({days[0]} → {days[-1]}, {len(days)} days)\n")
    print(f"{'date':<12} {'strategy':<24} {'role':<6} {'evals':>6} "
          f"{'ENTER':>6} {'EXIT':>5} {'wins':>4} {'loss':>4} {'win%':>5} "
          f"{'pnl':>11} {'avg_held':>8}")
    print("-" * 110)

    for day in days:
        for sid in sorted(per_day[day].keys()):
            if strategy_filter and strategy_filter not in sid:
                continue
            s = per_day[day][sid]
            role = "shadow" if _is_shadow(sid) else "live"
            print(f"{day.isoformat():<12} {sid:<24} {role:<6} "
                  f"{s.n_evals:>6} {s.n_enters:>6} {s.n_exits:>5} "
                  f"{s.wins:>4} {s.losses:>4} {s.win_rate:>4.0f}% "
                  f"₹{s.pnl:>+10,.0f} {s.avg_held:>7.0f}m")


def _print_by_strategy(per_day: dict[date, dict[str, DayStrategyStats]],
                       strategy_filter: str | None) -> None:
    """Sum across days, one row per strategy."""
    totals: dict[str, DayStrategyStats] = {}
    days_with_data: dict[str, set[date]] = defaultdict(set)
    for day, by_strat in per_day.items():
        for sid, s in by_strat.items():
            t = totals.setdefault(sid, DayStrategyStats(day=day, strategy_id=sid))
            t.n_evals += s.n_evals
            t.n_enters += s.n_enters
            t.n_exits += s.n_exits
            t.n_skips += s.n_skips
            t.pnl += s.pnl
            t.wins += s.wins
            t.losses += s.losses
            t.held_minutes_sum += s.held_minutes_sum
            days_with_data[sid].add(day)

    if not totals:
        print("No decision logs found in window.")
        return

    print(f"\nPer-strategy totals across window\n")
    print(f"{'strategy':<24} {'role':<6} {'days':>5} {'enter':>6} {'exit':>5} "
          f"{'wins':>4} {'loss':>4} {'win%':>5} {'pnl':>13} {'avg/day':>11} {'avg/trade':>11}")
    print("-" * 115)
    for sid in sorted(totals.keys()):
        if strategy_filter and strategy_filter not in sid:
            continue
        t = totals[sid]
        role = "shadow" if _is_shadow(sid) else "live"
        n_days = len(days_with_data[sid])
        per_day_pnl = t.pnl / n_days if n_days else 0.0
        per_trade_pnl = t.pnl / t.n_exits if t.n_exits else 0.0
        print(f"{sid:<24} {role:<6} {n_days:>5} {t.n_enters:>6} {t.n_exits:>5} "
              f"{t.wins:>4} {t.losses:>4} {t.win_rate:>4.0f}% "
              f"₹{t.pnl:>+12,.0f} ₹{per_day_pnl:>+10,.0f} ₹{per_trade_pnl:>+10,.0f}")
    print("-" * 115)
    print("\nNotes:")
    print("- 'shadow' strategies have id ending in '_shadow'. P&L is hypothetical")
    print("  (computed from chain prices in the strategy, no OMS routing).")
    print("- 'live' P&L is paper or real depending on PAPER_TRADING flag.")
    print("- avg/trade = total pnl / # exits, for compounding-naïve comparison.")


def main() -> int:
    args = _parse_args()
    decisions_dir = Path(args.decisions_dir)
    if not decisions_dir.exists():
        print(f"ERROR: decisions dir not found: {decisions_dir}", file=sys.stderr)
        return 1

    days = _date_range(args)
    per_day: dict[date, dict[str, DayStrategyStats]] = {}
    files_found = 0
    for d in days:
        path = decisions_dir / f"decisions_{d.isoformat()}.csv"
        rows = _load_day(path)
        if rows:
            files_found += 1
            per_day[d] = _aggregate(rows, d)

    if not per_day:
        print(f"No decision logs found in {decisions_dir} for window "
              f"{days[0]} → {days[-1]}", file=sys.stderr)
        return 1

    print(f"Loaded {files_found} day(s) of decisions from {decisions_dir}")

    if args.by_strategy:
        _print_by_strategy(per_day, args.strategy)
    else:
        _print_daily(per_day, args.strategy)

    return 0


if __name__ == "__main__":
    sys.exit(main())
