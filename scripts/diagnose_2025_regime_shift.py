"""Strictly DESCRIPTIVE diagnosis of the 2025 regime shift.

The Apr 25 wider validation revealed three consecutive losing
walk-forward windows from Mar-Jun 2025, while the strategy was
profitable in Jan-Apr 2025. This script tests **5 pre-specified
hypotheses** about what changed, using only the data already collected.

Anti-overfitting discipline:
    - Pre-stated hypotheses only — no exploratory feature mining.
    - Monthly aggregation where possible (coarse bins) so we don't
      learn from per-day noise.
    - DESCRIPTIVE output only — no parameter recommendations, no
      "best" thresholds, no auto-tuning. The script reports what is;
      decisions about what to do with the findings happen separately.
    - The 50-day holdout (2025-06-25 → 2025-09-05) is NOT touched.
      Any structural change we eventually propose gets exactly one
      shot at the holdout, before vs after.

Hypotheses (each tested independently):
    H1. VIX distribution shifted in 2025 — strategy got more exposure
        to its weakest bucket (mid_vix).
    H2. Strategy entered more aggressively in 2025 — score thresholds
        tuned on 2024 fired too easily on 2025 conditions.
    H3. Per-day P&L mean / variance shifted across months — locate
        when the regime change happens.
    H4. Top losing days share an identifiable feature (event date,
        VIX spike, expiry-week clustering, time-of-day).
    H5. Execution quality degraded — fill spreads / slippage widened
        in 2025 vs 2024.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))


DECISIONS_DIR = Path("data/decisions")
PARQUET_DIR = Path("data/gdfl_snapshots")

# Same train+val window as the wider validation run.
WINDOW_START = date(2024, 9, 2)
WINDOW_END = date(2025, 6, 25)

# WF window cutoff. Windows 0-2 (test ends ≤ 2025-04-11) were positive;
# windows 3-5 (test starts ≥ 2025-03-20) were negative. We bin by month
# rather than by exact WF window so we don't tune to the WF split.
EARLY_END = date(2025, 3, 1)   # everything ≤ this is "early"
LATE_START = date(2025, 4, 1)  # everything ≥ this is "late"
# March 2025 is the transition month; we report it separately so the
# binning isn't choosing a split point by hindsight.


def _load_decisions(start: date, end: date) -> pd.DataFrame:
    """Load all decision rows in the window (post-Bug-4-fix: 1× per row)."""
    from datetime import timedelta
    frames = []
    cur = start
    while cur <= end:
        path = DECISIONS_DIR / f"decisions_{cur.isoformat()}.csv"
        if path.exists():
            df = pd.read_csv(path)
            df["date"] = cur
            frames.append(df)
        cur = cur + timedelta(days=1)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _bucket(d: date) -> str:
    if d <= EARLY_END:
        return "EARLY (≤Feb 2025)"
    if d >= LATE_START:
        return "LATE  (≥Apr 2025)"
    return "MAR  (Mar 2025)"


def _month_key(d: date) -> str:
    return d.strftime("%Y-%m")


def _vix_bucket(vix: float) -> str:
    """Same thresholds as bucket_row in regime.py."""
    if vix > 15:
        return "high_vix"
    if vix >= 13:
        return "mid_vix"
    return "low_vix"


def main() -> None:
    print(f"Window: {WINDOW_START} → {WINDOW_END}")
    print(f"Anti-overfitting: monthly coarse bins; pre-stated hypotheses; holdout untouched")
    print()

    df = _load_decisions(WINDOW_START, WINDOW_END)
    if df.empty:
        print("ERROR: no decisions loaded; full-window run hasn't completed yet?")
        return

    df["bucket"] = df["date"].apply(_bucket)
    df["month"] = df["date"].apply(_month_key)
    df["vix_bucket"] = df["vix"].apply(_vix_bucket)

    enters = df[df["decision"].str.upper() == "ENTER"].copy()
    exits = df[df["decision"].str.upper() == "EXIT"].copy()
    print(f"Loaded {len(df)} rows: {len(enters)} ENTERs, {len(exits)} EXITs across "
          f"{df['date'].nunique()} trading days.")
    print()

    # ── H1: VIX distribution at ENTRY-time ───────────────────────────
    print("══ H1: VIX distribution at entry-time (entry-time vix from decisions log) ══")
    vix_table = (
        enters.groupby("bucket")["vix_bucket"]
        .value_counts(normalize=True)
        .unstack(fill_value=0.0)
        .round(3)
    )
    print(vix_table.to_string())
    print()
    # Also: average VIX
    avg_vix = enters.groupby("bucket")["vix"].agg(["mean", "median", "std", "count"]).round(2)
    print("Mean / median / stdev / count of VIX at entry, by bucket:")
    print(avg_vix.to_string())
    print()

    # ── H2: entry frequency + score distribution ─────────────────────
    print("══ H2: Entry frequency and score distribution by month ══")
    monthly = (
        enters.groupby("month").agg(
            n_entries=("date", "count"),
            avg_rule_score=("rule_score", "mean"),
            avg_final_score=("final_score", "mean"),
            avg_threshold=("threshold", "mean"),
        ).round(2)
    )
    monthly["score_minus_threshold"] = (monthly["avg_final_score"] - monthly["avg_threshold"]).round(2)
    print(monthly.to_string())
    print()

    # ── H3: Per-day P&L by month ─────────────────────────────────────
    print("══ H3: Daily P&L distribution by month ══")
    daily_pnl = (
        exits.groupby("date")["outcome_pnl"]
        .apply(lambda s: pd.to_numeric(s, errors="coerce").fillna(0.0).sum())
    )
    daily_pnl.index = pd.to_datetime(daily_pnl.index)
    monthly_pnl = (
        daily_pnl.groupby(daily_pnl.index.strftime("%Y-%m"))
        .agg(["sum", "mean", "median", "std", "count"])
        .round(0)
    )
    monthly_pnl["pos_pct"] = (
        daily_pnl.groupby(daily_pnl.index.strftime("%Y-%m"))
        .apply(lambda s: float((s > 0).sum() / max(len(s), 1) * 100))
        .round(1)
    )
    print(monthly_pnl.to_string())
    print()

    # Identify the inflection: cumulative pnl over time
    cum = daily_pnl.cumsum()
    peak_d = cum.idxmax()
    trough_d = cum.idxmin()
    print(f"Cumulative P&L peak: ₹{cum.max():,.0f} on {peak_d.date()}")
    print(f"Cumulative P&L trough: ₹{cum.min():,.0f} on {trough_d.date()}")
    print(f"Net at end of window: ₹{cum.iloc[-1]:,.0f}")
    print()

    # ── H4: Top losing days context ──────────────────────────────────
    print("══ H4: Top 10 losing days with entry context ══")
    losing_days = daily_pnl[daily_pnl < 0].sort_values()
    rows = []
    for ts in losing_days.head(10).index:
        d = ts.date()
        day_enters = enters[enters["date"] == d]
        if day_enters.empty:
            continue
        # Average / median entry-time VIX, move_from_open, score
        rows.append({
            "date": d.isoformat(),
            "weekday": d.strftime("%a"),
            "pnl": int(daily_pnl[ts]),
            "n_entries": len(day_enters),
            "avg_vix": round(day_enters["vix"].mean(), 2),
            "avg_move": round(day_enters["move_from_open_pct"].mean(), 2),
            "avg_score": round(day_enters["rule_score"].mean(), 1),
            "is_expiry_count": int(day_enters["is_expiry"].sum()),
        })
    print(pd.DataFrame(rows).to_string(index=False))
    print()

    # ── H5: Execution quality (entry premium / spread proxy) ─────────
    # Decisions log doesn't carry spread directly, but entry_premium and
    # quantity together with outcome_pnl give a coarse fill-cost proxy:
    # round-trip cost / round-trip premium. We can't infer slippage
    # cleanly without broker._trades, so this is best-effort and
    # reported as a SECONDARY hypothesis.
    print("══ H5: Round-trip P&L per ₹ of premium collected (decay efficiency proxy) ══")
    # Pair ENTER+EXIT by cumcount within (strategy_id, leg, decision)
    enters_p = enters.copy()
    exits_p = exits.copy()
    for tbl in (enters_p, exits_p):
        tbl["pair_idx"] = tbl.groupby(["strategy_id", "leg"]).cumcount()
    paired = enters_p.merge(
        exits_p[["strategy_id", "leg", "pair_idx", "outcome_pnl"]],
        on=["strategy_id", "leg", "pair_idx"],
        suffixes=("", "_exit"),
        how="inner",
    )
    paired["outcome_pnl"] = paired["outcome_pnl_exit"]
    paired["entry_premium"] = pd.to_numeric(paired["entry_premium"], errors="coerce").fillna(0.0)
    paired["pnl_per_premium"] = (
        paired["outcome_pnl"] / paired["entry_premium"].replace(0, np.nan)
    )
    eff = (
        paired.groupby("bucket")
        .agg(
            n_trades=("outcome_pnl", "count"),
            avg_premium=("entry_premium", "mean"),
            avg_pnl=("outcome_pnl", "mean"),
            avg_pnl_per_premium=("pnl_per_premium", "mean"),
            median_pnl_per_premium=("pnl_per_premium", "median"),
        ).round(3)
    )
    print(eff.to_string())
    print()
    print(
        "Interpretation: pnl_per_premium drops if the strategy's edge per ₹ "
        "of risk shrunk in 2025 — proxy for either spread widening, slower "
        "decay capture, or worse exit timing."
    )
    print()

    # ── Summary block ────────────────────────────────────────────────
    print("══ SUMMARY (descriptive, no recommendations) ══")
    print(f"  EARLY bucket (≤Feb 2025): {(df['bucket']=='EARLY (≤Feb 2025)').sum()} rows")
    print(f"  MAR    bucket (Mar 2025): {(df['bucket']=='MAR  (Mar 2025)').sum()} rows")
    print(f"  LATE   bucket (≥Apr 2025): {(df['bucket']=='LATE  (≥Apr 2025)').sum()} rows")
    print()
    print("Next step is for the human to read the tables and decide whether the")
    print("regime shift is (a) external (no fix), (b) addressable via a single")
    print("structural change, or (c) reason to retire the strategy. Any change")
    print("then gets ONE shot at the holdout window for verification.")


if __name__ == "__main__":
    main()
