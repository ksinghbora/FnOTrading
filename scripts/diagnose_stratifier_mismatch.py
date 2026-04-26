"""Test the hypothesis: harness stratifies by EXIT-row labels not ENTRY-row.

Per-regime stats from two viewpoints:
  A. Harness behavior — apply bucket_row to every (ENTER+EXIT) row.
  B. Entry-only — pair ENTER+EXIT, label by ENTER row, attach EXIT pnl.

If (A) and (B) disagree on sign of high_vix Sharpe, the regime gate at
runtime can't help because the harness is measuring exit-time regime.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtest.validation.regime import bucket_row, load_event_dates, stratify

DECISIONS_DIR = Path("data/decisions")
TRAIN_START = date(2024, 9, 2)
VAL_END = date(2024, 12, 31)


def _load_raw(start: date, end: date) -> pd.DataFrame:
    """Concat decisions CSVs verbatim — both ENTER and EXIT rows."""
    frames = []
    for path in sorted(DECISIONS_DIR.glob("decisions_*.csv")):
        try:
            d = date.fromisoformat(path.stem.split("_", 1)[1])
        except (ValueError, IndexError):
            continue
        if not (start <= d <= end):
            continue
        df = pd.read_csv(path)
        df["date"] = d
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _load_paired(start: date, end: date) -> pd.DataFrame:
    """Pair ENTER+EXIT rows; entry-time features + exit pnl."""
    frames = []
    for path in sorted(DECISIONS_DIR.glob("decisions_*.csv")):
        try:
            d = date.fromisoformat(path.stem.split("_", 1)[1])
        except (ValueError, IndexError):
            continue
        if not (start <= d <= end):
            continue
        df = pd.read_csv(path)
        df["date"] = d
        df["_pair_idx"] = df.groupby(["strategy_id", "leg", "decision"]).cumcount()
        enters = df[df["decision"] == "ENTER"].copy()
        exits = df[df["decision"] == "EXIT"].copy()
        merged = enters.merge(
            exits[["strategy_id", "leg", "_pair_idx", "outcome_pnl"]],
            on=["strategy_id", "leg", "_pair_idx"],
            suffixes=("", "_exit"),
            how="inner",
        )
        merged["outcome_pnl"] = merged["outcome_pnl_exit"]
        frames.append(merged)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _table(stats: dict) -> str:
    rows = []
    for r, s in stats.items():
        rows.append(
            f"  {r:<14} n={s.num_trades:>5}  total={s.total_pnl:>+12.0f}  "
            f"Sharpe={s.sharpe:>+7.4f}  win%={s.win_rate:>5.2f}  "
            f"PASS={'Y' if s.passed else 'N'}"
        )
    return "\n".join(rows)


def main() -> None:
    print(f"Window: {TRAIN_START} → {VAL_END}\n")

    # (A) Harness-style: feed ALL rows (ENTER+EXIT) through stratify.
    raw = _load_raw(TRAIN_START, VAL_END)
    print(f"[A] Harness-style (every row, EXIT bucketed by exit-time vix/move):")
    print(f"    raw rows: {len(raw):,}")
    a_stats = stratify(raw)
    print(_table(a_stats))

    print()

    # (B) Entry-only: pair ENTER+EXIT, label by ENTER row's features only.
    paired = _load_paired(TRAIN_START, VAL_END)
    # Drop the harness's outcome_pnl column duplicate from EXIT and use ours.
    paired = paired.drop(columns=["outcome_pnl_exit"], errors="ignore")
    print(f"[B] Entry-time labels (paired, ENTER-row vix/move/dte/event):")
    print(f"    paired trades: {len(paired):,}")
    b_stats = stratify(paired)
    print(_table(b_stats))

    print()
    print("─── Sign-flips between A and B ───")
    for r in a_stats:
        a, b = a_stats[r], b_stats[r]
        if (a.sharpe > 0) != (b.sharpe > 0) or (a.passed != b.passed):
            print(
                f"  {r}: A={a.sharpe:+.2f}/{a.passed} vs B={b.sharpe:+.2f}/{b.passed}  "
                f"(n_A={a.num_trades} n_B={b.num_trades})"
            )


if __name__ == "__main__":
    main()
