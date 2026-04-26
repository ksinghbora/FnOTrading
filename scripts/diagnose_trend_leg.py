"""Per-leg attribution within high_vix and trending regimes.

The Apr 25 P1.5 validation revealed the trend leg loses -7.38 Sharpe in
its designed "trending" regime. Before any fix we need to know:

1. How much of the trending-bucket loss is trend-leg vs premium-leg?
2. What entry-time features separate winning trend trades from losing ones?

Loads all decisions CSVs in the train+val window (2024-09-02 → 2024-12-31),
applies the stratifier ``bucket_row`` to assign regime labels, then
breaks each regime down by leg.

Usage::

    uv run python scripts/diagnose_trend_leg.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtest.validation.regime import bucket_row, load_event_dates


DECISIONS_DIR = Path("data/decisions")
TRAIN_START = date(2024, 9, 2)
VAL_END = date(2024, 12, 31)


def _load_window(start: date, end: date) -> pd.DataFrame:
    """Pair ENTER+EXIT rows from decisions CSVs into trade-rows.

    Each entered trade in the harness writes two adjacent rows: an ENTER
    row with entry-time features and a following EXIT row with the
    realized pnl + exit_reason. We zip them so each output row carries
    entry-time features (vix, move_from_open_pct, dte, is_expiry, score
    components) AND outcome_pnl in one place.
    """
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
        # Pair ENTER+EXIT. The harness writes them adjacent and in order
        # within the same strategy_id+leg, but we use a defensive zip
        # (cumcount per strategy_id+leg) so any future schema variation
        # doesn't silently mispair.
        df["_pair_idx"] = df.groupby(["strategy_id", "leg", "decision"]).cumcount()
        enters = df[df["decision"] == "ENTER"].copy()
        exits = df[df["decision"] == "EXIT"].copy()
        merged = enters.merge(
            exits[["strategy_id", "leg", "_pair_idx", "outcome_pnl", "exit_reason", "held_minutes"]],
            on=["strategy_id", "leg", "_pair_idx"],
            suffixes=("", "_exit"),
            how="inner",
        )
        # Override outcome_pnl with the exit row's value (ENTER rows have
        # outcome_pnl=NaN by construction).
        merged["outcome_pnl"] = merged["outcome_pnl_exit"]
        merged.drop(columns=["outcome_pnl_exit"], inplace=True)
        frames.append(merged)
    if not frames:
        raise SystemExit(f"No decisions in {start}..{end} under {DECISIONS_DIR}")
    return pd.concat(frames, ignore_index=True)


def _compute_labels(row: pd.Series, event_dates: dict) -> list[str]:
    """Mirror bucket_row signature on a per-decision row."""
    # bucket_row needs: vix, move_from_open_pct, dte, is_expiry, date
    sub = pd.Series(
        {
            "vix": row.get("vix", np.nan),
            "move_from_open_pct": row.get("move_from_open_pct", np.nan),
            "dte": row.get("dte", np.nan),
            "is_expiry": bool(row.get("is_expiry", False)),
            "date": row["date"],
        }
    )
    return bucket_row(sub, event_dates)


def _sharpe(pnl: pd.Series) -> float:
    if len(pnl) < 2 or pnl.std(ddof=1) == 0:
        return 0.0
    # Per-trade Sharpe scaled by sqrt(N) — same convention as harness
    return float(pnl.mean() / pnl.std(ddof=1) * np.sqrt(len(pnl)))


def _summarize(df: pd.DataFrame, label: str) -> None:
    n = len(df)
    if n == 0:
        print(f"  {label}: n=0")
        return
    pnl = df["outcome_pnl"].astype(float)
    total = pnl.sum()
    avg = pnl.mean()
    sh = _sharpe(pnl)
    wr = (pnl > 0).mean() * 100
    print(
        f"  {label}: n={n:5d}  total={total:>12.0f}  avg={avg:>+8.1f}  "
        f"Sharpe={sh:>+6.2f}  win%={wr:5.1f}"
    )


def main() -> None:
    print(f"Loading decisions {TRAIN_START} → {VAL_END}...")
    df = _load_window(TRAIN_START, VAL_END)
    print(f"  loaded {len(df):,} decision rows from {df['date'].nunique()} days")

    # ``df`` is already paired ENTER+EXIT; outcome_pnl now sits on every
    # row that resolved. Drop any orphan ENTERs (open at file boundary).
    entered = df[df["outcome_pnl"].notna()].copy()
    print(f"  {len(entered):,} entered+resolved trades")

    print("\nLeg distribution:")
    print(entered["leg"].value_counts().to_string())

    print("\nApplying stratifier labels (bucket_row)...")
    event_dates = load_event_dates()
    entered["labels"] = entered.apply(
        lambda r: _compute_labels(r, event_dates), axis=1
    )

    # Cross-cut by every regime that bucket_row can produce.
    REGIMES = [
        "high_vix",
        "mid_vix",
        "low_vix",
        "expiry_week",
        "event_day",
        "trending",
        "range_bound",
    ]

    print("\n══ Per-leg breakdown by regime ══\n")
    for regime in REGIMES:
        sub = entered[entered["labels"].apply(lambda lst: regime in lst)]
        if sub.empty:
            continue
        print(f"[{regime}]  n_total={len(sub)}")
        _summarize(sub, "ALL")
        for leg in sorted(sub["leg"].dropna().unique()):
            _summarize(sub[sub["leg"] == leg], f"  leg={leg:<8}")
        print()

    # Deep-dive: trend leg in 'trending' regime
    print("\n══ TREND leg in 'trending' regime — feature breakdown ══\n")
    tt = entered[
        (entered["leg"] == "trend")
        & (entered["labels"].apply(lambda lst: "trending" in lst))
    ].copy()
    if tt.empty:
        print("  no trend-leg trades in trending regime")
        return

    print(f"Total: {len(tt)} trades, total_pnl={tt['outcome_pnl'].sum():.0f}")
    print()

    # Feature splits
    splits = {
        "oi_confirmed": [(False, "oi_confirmed=False"), (True, "oi_confirmed=True")],
        "score_clamp_hit": [
            (False, "score_clamp_hit=False"),
            (True, "score_clamp_hit=True"),
        ],
        "banknifty_confirming": [
            (False, "BN_confirming=False"),
            (True, "BN_confirming=True"),
        ],
    }
    for col, options in splits.items():
        if col not in tt.columns:
            continue
        print(f"Split by {col}:")
        for val, lbl in options:
            sub = tt[tt[col] == val]
            _summarize(sub, lbl)
        print()

    # Continuous bins
    bin_specs = {
        "trend_duration_minutes": [(0, 30), (30, 45), (45, 60), (60, 120), (120, 9999)],
        "breakout_strength": [(0, 0.3), (0.3, 0.5), (0.5, 0.8), (0.8, 1.5), (1.5, 99)],
        "vix": [(0, 12), (12, 15), (15, 18), (18, 22), (22, 100)],
        "hour": [(9, 11), (11, 12), (12, 13), (13, 14), (14, 16)],
        "rule_score": [(0, 50), (50, 60), (60, 70), (70, 80), (80, 999)],
    }
    for col, edges in bin_specs.items():
        if col not in tt.columns:
            continue
        print(f"Split by {col}:")
        for lo, hi in edges:
            sub = tt[(tt[col] >= lo) & (tt[col] < hi)]
            _summarize(sub, f"[{lo:>5}, {hi:>5})")
        print()

    # Score components
    print("Mean score components for trend-leg trades in 'trending':")
    score_cols = [c for c in tt.columns if c.startswith("score_f")]
    if score_cols:
        winners = tt[tt["outcome_pnl"] > 0]
        losers = tt[tt["outcome_pnl"] < 0]
        for col in score_cols:
            w = winners[col].mean() if not winners.empty else 0
            l = losers[col].mean() if not losers.empty else 0
            print(f"  {col:>18}: winners={w:>+6.2f}  losers={l:>+6.2f}  delta={w-l:>+6.2f}")
        print(f"  n_winners={len(winners)}  n_losers={len(losers)}")

    print()


if __name__ == "__main__":
    main()
