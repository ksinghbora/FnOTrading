"""Iron Condor deep-dive — when does it work, when doesn't it?

The wide-baseline re-cost showed Iron Condor standalone has +1.06
Sharpe net of cost (n=27, +₹267/trade mean). That's a PASS but the
sample is borderline (n=27 << 120 for 95% CI on Sharpe).

Strictly DESCRIPTIVE analysis (no tuning):
1. When did the 27 IC trades fire? (date, day-of-week, time-of-day)
2. What VIX / spot conditions?
3. What were winners vs losers' entry features?
4. Could a regime filter (VIX cap, day-of-week) have improved Sharpe?
   We DESCRIBE, don't propose — overfitting guard.

Discipline
----------
Pure description. No "best" cutoffs picked. No rule recommendations.
Just: here's what worked, here's what didn't. Operator decides the
implications in the morning.
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.truthup_recost_wide_baseline import (
    _load_paired_trades, _compute_trade_charges,
)


def _per_trade_sharpe(s: pd.Series) -> float:
    if len(s) < 2 or s.std(ddof=1) == 0:
        return 0.0
    return float(s.mean() / s.std(ddof=1) * np.sqrt(len(s)))


def main() -> None:
    print("=" * 70)
    print("Iron Condor Deep-Dive (descriptive only, no tuning)")
    print("=" * 70)
    print()

    print("Loading + costing wide_baseline trades...")
    trades = _load_paired_trades()
    for t in trades:
        t.total_charges = _compute_trade_charges(t)
        t.net_pnl = t.outcome_pnl - float(t.total_charges)
    ic = [t for t in trades if t.mode == "iron_condor"]
    print(f"  Total trades: {len(trades)}")
    print(f"  IC trades: {len(ic)}")
    print()

    df = pd.DataFrame([
        {
            "date": t.trade_date,
            "weekday": t.trade_date.strftime("%a"),
            "month": t.trade_date.strftime("%Y-%m"),
            "hour": t.entry_ts.hour,
            "vix": t.vix,
            "spot": t.spot,
            "entry_premium": t.entry_premium,
            "outcome_pnl": t.outcome_pnl,
            "charges": float(t.total_charges),
            "net_pnl": t.net_pnl,
            "exit_reason": t.exit_reason,
            "held_minutes": t.held_minutes,
        }
        for t in ic
    ])

    print("─" * 70)
    print("Headline (IC standalone):")
    print(f"  n={len(df)}, mean net=₹{df['net_pnl'].mean():.0f}, "
          f"median net=₹{df['net_pnl'].median():.0f}")
    print(f"  Sharpe (per-trade × √N) = {_per_trade_sharpe(df['net_pnl']):+.3f}")
    print(f"  Win rate (net): {(df['net_pnl']>0).mean()*100:.1f}%")
    print(f"  CI on mean (95%): ₹{df['net_pnl'].mean() - 2*df['net_pnl'].std()/np.sqrt(len(df)):.0f} "
          f"to ₹{df['net_pnl'].mean() + 2*df['net_pnl'].std()/np.sqrt(len(df)):.0f}")
    print()

    print("─" * 70)
    print("By weekday:")
    by_dow = df.groupby("weekday").agg(
        n=("net_pnl", "count"),
        net_total=("net_pnl", "sum"),
        net_mean=("net_pnl", "mean"),
        sharpe=("net_pnl", _per_trade_sharpe),
        win_rate=("net_pnl", lambda s: (s > 0).mean() * 100),
    ).round(2)
    print(by_dow.to_string())
    print()

    print("─" * 70)
    print("By VIX bucket (entry):")
    df["vix_bucket"] = pd.cut(
        df["vix"],
        bins=[0, 12, 14, 16, 18, 20, 100],
        labels=["<12", "12-14", "14-16", "16-18", "18-20", "20+"],
    )
    by_vix = df.groupby("vix_bucket", observed=False).agg(
        n=("net_pnl", "count"),
        net_total=("net_pnl", "sum"),
        net_mean=("net_pnl", "mean"),
        sharpe=("net_pnl", _per_trade_sharpe),
        win_rate=("net_pnl", lambda s: (s > 0).mean() * 100),
    ).round(2)
    print(by_vix.to_string())
    print()

    print("─" * 70)
    print("By month:")
    by_month = df.groupby("month").agg(
        n=("net_pnl", "count"),
        net_total=("net_pnl", "sum"),
        net_mean=("net_pnl", "mean"),
    ).round(2)
    print(by_month.to_string())
    print()

    print("─" * 70)
    print("Top 5 winners (gross) and Top 5 losers:")
    top = df.nlargest(5, "net_pnl")
    bot = df.nsmallest(5, "net_pnl")
    print("Top 5:")
    print(top[["date", "weekday", "vix", "entry_premium", "outcome_pnl",
               "net_pnl", "exit_reason"]].to_string(index=False))
    print()
    print("Bottom 5:")
    print(bot[["date", "weekday", "vix", "entry_premium", "outcome_pnl",
               "net_pnl", "exit_reason"]].to_string(index=False))
    print()

    print("─" * 70)
    print("Exit reason distribution (IC):")
    print(df.groupby("exit_reason").agg(
        n=("net_pnl", "count"),
        net_total=("net_pnl", "sum"),
        net_mean=("net_pnl", "mean"),
    ).round(2).to_string())
    print()

    # Save the IC table for the morning briefing
    out_path = Path("reports/phase3_pre/ic_deep_dive.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    print(f"✓ IC trade table saved: {out_path}")


if __name__ == "__main__":
    main()
