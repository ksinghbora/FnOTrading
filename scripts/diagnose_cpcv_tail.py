"""Diagnose the bottom 5 CPCV paths to identify the source of tail risk.

The Apr 25 2026 validation reported:
    CPCV: median +1.06, p05 = **-0.67** (FAIL — needs > 0)

That's 4-5 paths out of 45 with Sharpe < 0. Are they:
    (a) random noise from a small sample → wider window will fix it
    (b) clustered around a specific bad week → strategy has a real
        weakness on that regime, fixable
    (c) clustered around a specific test fold being EXCLUDED (so a
        usually-protective day isn't in train) → strategy benefits
        disproportionately from a single day

We answer this by reproducing the deterministic CPCV split, joining
each path's train_dates against the full-window run's per-day P&L
(reconstructed from data/decisions/*.csv), and ranking paths by Sharpe.

NOTE: this approach uses GROSS pnl from the decisions log
(``outcome_pnl`` on EXIT rows = sell-buy × qty, no charges). The
harness uses NET pnl from ``calculate_metrics(broker._trades)`` which
deducts brokerage / STT / GST. The two differ by a near-constant per
trade so the path RANKING by Sharpe is preserved, even if absolute
Sharpe values shift by ~0.1-0.2.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtest.gdfl_market_source import GDFLMarketSource
from src.backtest.validation.cpcv import CombinatorialPurgedCV
from src.backtest.validation.splits import StrategySplit, SplitLoader
from src.market_data.simulator import NIFTY_SPOT_TOKEN

# Same args as the validation run that produced the FAIL verdict.
TRAIN_END = date(2024, 11, 29)
VAL_END = date(2024, 12, 31)
HOLDOUT_END = date(2025, 1, 31)
PARQUET_DIR = Path("data/gdfl_snapshots")
DECISIONS_DIR = Path("data/decisions")
N_FOLDS = 10
N_TEST_FOLDS = 2
MAX_PATHS = 50
SEED = 42


def _load_daily_pnl(start: date, end: date) -> pd.Series:
    """Sum EXIT-row outcome_pnl per day from the decisions CSV.

    The full-window validation run wrote one set of decisions per day in
    the validation window. Bug 4 fix ensures no duplication. We sum
    outcome_pnl per date to produce a daily P&L series.
    """
    rows: list[tuple[date, float]] = []
    cur = start
    while cur <= end:
        path = DECISIONS_DIR / f"decisions_{cur.isoformat()}.csv"
        if path.exists():
            df = pd.read_csv(path)
            exits = df[df["decision"].str.upper() == "EXIT"]
            pnl = pd.to_numeric(exits["outcome_pnl"], errors="coerce").fillna(0.0).sum()
        else:
            pnl = 0.0
        rows.append((cur, float(pnl)))
        cur += timedelta(days=1)

    s = pd.Series({r[0]: r[1] for r in rows})
    s.index.name = "date"
    s.name = "daily_pnl"
    return s


def _path_sharpe(daily_pnl: pd.Series, train_dates: list[date]) -> tuple[float, float, int]:
    """Annualised Sharpe + total pnl + non-zero day count for a path.

    Filter the daily P&L series to only the path's train dates.
    Annualised Sharpe = (mean / std) * sqrt(252) over those days.
    """
    sub = daily_pnl.loc[[d for d in train_dates if d in daily_pnl.index]]
    if len(sub) < 2:
        return 0.0, float(sub.sum()) if len(sub) else 0.0, 0
    std = float(sub.std(ddof=1))
    if std == 0.0:
        return 0.0, float(sub.sum()), int((sub != 0).sum())
    sharpe = float(sub.mean() / std * np.sqrt(252))
    return sharpe, float(sub.sum()), int((sub != 0).sum())


def main() -> None:
    print(f"Loading combined train+val days for {TRAIN_END} ↔ {VAL_END}...")
    split = StrategySplit(
        train_end=TRAIN_END, val_end=VAL_END, holdout_end=HOLDOUT_END,
    )
    source = GDFLMarketSource(PARQUET_DIR, "NIFTY", NIFTY_SPOT_TOKEN)
    loader = SplitLoader(
        split, strategy="portfolio",
        decisions_dir=DECISIONS_DIR, gdfl_source=source,
    )
    combined_all = loader.train_and_val_days()
    combined = loader.available_gdfl_days(combined_all)
    print(f"  combined window: {len(combined)} GDFL days "
          f"({combined[0]} → {combined[-1]})")

    print(f"\nLoading daily P&L from decisions...")
    daily_pnl = _load_daily_pnl(combined[0], combined[-1])
    nz = (daily_pnl != 0).sum()
    print(f"  {len(daily_pnl)} calendar days, {nz} non-zero P&L days, "
          f"total = ₹{daily_pnl.sum():,.0f}")

    print(f"\nReproducing CPCV split (seed={SEED}, n_folds={N_FOLDS}, "
          f"n_test_folds={N_TEST_FOLDS}, max_paths={MAX_PATHS})...")
    cv = CombinatorialPurgedCV(
        n_folds=N_FOLDS, n_test_folds=N_TEST_FOLDS,
        max_paths=MAX_PATHS, seed=SEED,
    )
    paths = list(cv.split(combined))
    print(f"  {len(paths)} paths generated")

    rows = []
    for path_id, (train_idx, test_idx, fold_ids) in enumerate(paths):
        train_dates = [combined[i] for i in train_idx]
        test_dates = [combined[i] for i in test_idx]
        sharpe, pnl, nz_days = _path_sharpe(daily_pnl, train_dates)
        rows.append({
            "path_id": path_id,
            "fold_ids": fold_ids,
            "train_n": len(train_dates),
            "test_n": len(test_dates),
            "train_first": train_dates[0],
            "train_last": train_dates[-1],
            "test_first": test_dates[0] if test_dates else None,
            "test_last": test_dates[-1] if test_dates else None,
            "test_dates": test_dates,
            "sharpe": sharpe,
            "total_pnl": pnl,
            "nz_days": nz_days,
        })

    paths_df = pd.DataFrame(rows).sort_values("sharpe").reset_index(drop=True)

    # Distribution snapshot
    print(f"\nPer-path Sharpe distribution:")
    print(f"  mean   = {paths_df['sharpe'].mean():+.3f}")
    print(f"  median = {paths_df['sharpe'].median():+.3f}")
    print(f"  p05    = {paths_df['sharpe'].quantile(0.05):+.3f}")
    print(f"  p95    = {paths_df['sharpe'].quantile(0.95):+.3f}")

    print(f"\n══ BOTTOM 5 paths (worst Sharpe) ══")
    bottom = paths_df.head(5)
    for _, r in bottom.iterrows():
        print(
            f"  path_id={r['path_id']:2d}  fold_ids={r['fold_ids']}  "
            f"sharpe={r['sharpe']:+6.3f}  pnl=₹{r['total_pnl']:>+8.0f}  "
            f"train={r['train_first']}..{r['train_last']} (n={r['train_n']})  "
            f"TEST={r['test_first']}..{r['test_last']} (n={r['test_n']})"
        )

    print(f"\n══ TOP 5 paths (best Sharpe) ══")
    top = paths_df.tail(5)
    for _, r in top.iterrows():
        print(
            f"  path_id={r['path_id']:2d}  fold_ids={r['fold_ids']}  "
            f"sharpe={r['sharpe']:+6.3f}  pnl=₹{r['total_pnl']:>+8.0f}  "
            f"train={r['train_first']}..{r['train_last']} (n={r['train_n']})  "
            f"TEST={r['test_first']}..{r['test_last']} (n={r['test_n']})"
        )

    # Common patterns in bottom 5
    print(f"\n══ Tail-risk attribution: which days drive the bottom 5 down? ══")

    # Big losing days in the full window (sorted by absolute loss)
    losers = daily_pnl[daily_pnl < 0].sort_values()
    print(f"\nTop 10 losing days in full window:")
    for d, p in losers.head(10).items():
        print(f"  {d.isoformat()} ({d.strftime('%a')}): ₹{p:>+10.0f}")

    # Big winning days
    winners = daily_pnl[daily_pnl > 0].sort_values(ascending=False)
    print(f"\nTop 10 winning days in full window:")
    for d, p in winners.head(10).items():
        print(f"  {d.isoformat()} ({d.strftime('%a')}): ₹{p:>+10.0f}")

    # Hypothesis: do the bottom paths share specific train days that are
    # net-negative? Compute, for each day, what fraction of bottom-5
    # paths included that day in their train set.
    print(f"\nDays in BOTTOM 5 paths' train sets (sorted by frequency, "
          f"showing days with daily P&L):")
    bottom_dates: list[date] = []
    for _, r in bottom.iterrows():
        train_idx = next(p[0] for i, p in enumerate(paths) if i == r["path_id"])
        bottom_dates.extend([combined[i] for i in train_idx])
    bottom_freq = pd.Series(bottom_dates).value_counts()

    # Inversely: which days are NEVER in bottom-5's train? Those are
    # protective days the bottom paths are missing.
    bottom_missing = [d for d in combined if d not in set(bottom_dates)]
    print(f"  Days excluded from ALL bottom 5 paths' train (n={len(bottom_missing)}):")
    for d in bottom_missing[:15]:
        pnl = daily_pnl.get(d, 0.0)
        print(f"    {d.isoformat()} ({d.strftime('%a')}): ₹{pnl:>+8.0f}")
    if len(bottom_missing) > 15:
        print(f"    ... +{len(bottom_missing) - 15} more")

    # Symmetric: days that are in EVERY bottom-5 path's train (so always
    # included → these days definitely contribute to bottom paths' badness)
    bottom_in_all = bottom_freq[bottom_freq == len(bottom)]
    print(f"\n  Days in ALL 5 bottom paths' train sets ({len(bottom_in_all)}):")
    bot_total = sum(daily_pnl.get(d, 0.0) for d in bottom_in_all.index)
    print(f"    Total P&L on these days: ₹{bot_total:+,.0f}")

    # Test fold (excluded) days — what's their P&L?
    print(f"\n══ TEST-fold (excluded) day P&L per bottom path ══")
    print(f"(Bottom paths exclude these days from training. If excluded "
          f"days are net POSITIVE, the bottom path lost the protection of "
          f"those days. If net NEGATIVE, the bottom path benefited from "
          f"excluding them but still lost — i.e. its included days were "
          f"the real problem.)")
    for _, r in bottom.iterrows():
        test_dates = r["test_dates"]
        test_pnl = sum(daily_pnl.get(d, 0.0) for d in test_dates)
        print(
            f"  path_id={r['path_id']:2d}: "
            f"test_dates={test_dates[0]}..{test_dates[-1]} "
            f"(n={len(test_dates)}) "
            f"test_pnl=₹{test_pnl:>+8.0f}"
        )


if __name__ == "__main__":
    main()
