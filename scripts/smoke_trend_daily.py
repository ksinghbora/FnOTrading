#!/usr/bin/env python
"""Daily-bar trend-following smoke on NIFTY (multi-day holds).

The intraday Donchian smoke (smoke_trend_v1.py) showed gross Sharpe
+0.17 max at 5-min timeframe — below the +0.5 tradeable threshold.
This script tests whether moving to DAILY bars (and holding positions
overnight) recovers a meaningful edge by escaping the intraday
microstructure noise.

Signal:
  Entry long:  daily close > 20-day high of last 21 daily closes
  Entry short: symmetric
  Filters:     VIX (daily mean) in [12, 22], ATR(14)/spot above floor
  Exit:        ATR(14) trailing stop at 2× ATR, OR reverse on
               opposite breakout, OR max-hold N days

Position is HELD OVERNIGHT — the multi-day variant the pivot doc
flagged as a v2 extension. No intraday time-stops.

Cost model:
  1 bp round-trip (futures) — same as intraday smoke

Usage:
    uv run python scripts/smoke_trend_daily.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd


SPOT_CSV = Path("data/nifty_spot_minute.csv")
VIX_CSV = Path("data/india_vix_minute.csv")

DONCHIAN_LOOKBACK = 20
ATR_PERIOD = 14
ATR_FLOOR_PCT = 0.5         # Daily-bar appropriate (typical NIFTY daily ATR ~1-2% of spot)
ATR_STOP_MULT = 2.0
VIX_MIN = 12.0
VIX_MAX = 22.0
MAX_HOLD_DAYS = 30          # Hard time stop in days

COST_BPS_ROUNDTRIP = 1.0
LOT_SIZE = 75
LOTS = 1


def load_daily() -> pd.DataFrame:
    """Resample minute bars to daily OHLC + VIX daily-mean."""
    spot = pd.read_csv(SPOT_CSV, parse_dates=["date"])
    vix = pd.read_csv(VIX_CSV, parse_dates=["date"])
    spot = spot.set_index("date").sort_index()
    vix = vix.set_index("date").sort_index()

    daily_spot = spot[["open", "high", "low", "close"]].resample("1D").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
    }).dropna()
    daily_vix = vix[["close"]].rename(columns={"close": "vix"}).resample("1D").mean().dropna()
    df = daily_spot.join(daily_vix, how="left")
    df["vix"] = df["vix"].ffill()
    return df.dropna()


def compute_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def compute_donchian(df: pd.DataFrame, lookback: int = DONCHIAN_LOOKBACK):
    high_n = df["high"].shift(1).rolling(lookback).max()
    low_n = df["low"].shift(1).rolling(lookback).min()
    return high_n, low_n


def run_backtest(df: pd.DataFrame) -> dict:
    df = df.copy()
    df["atr"] = compute_atr(df)
    high_n, low_n = compute_donchian(df)
    df["donchian_high"] = high_n
    df["donchian_low"] = low_n

    trades: list[dict] = []
    position = 0
    entry_px = 0.0
    entry_idx = None
    peak_favorable = 0.0
    entry_atr = 0.0
    days_held = 0

    for i, row in enumerate(df.itertuples()):
        ts = row.Index
        close = row.close
        atr = row.atr
        vix = row.vix
        donch_hi = row.donchian_high
        donch_lo = row.donchian_low

        # ─── EXITS first ──────────────────────────────────────────────
        if position != 0:
            days_held += 1
            # Hard max-hold
            if days_held >= MAX_HOLD_DAYS:
                _close_trade(trades, position, entry_px, close, entry_idx, ts, "max_hold")
                position = 0
                continue

            if position > 0:
                peak_favorable = max(peak_favorable, close)
                trail = peak_favorable - ATR_STOP_MULT * entry_atr
                if close <= trail:
                    _close_trade(trades, position, entry_px, close, entry_idx, ts, "trail_stop")
                    position = 0
                    continue
                if not np.isnan(donch_lo) and close < donch_lo:
                    _close_trade(trades, position, entry_px, close, entry_idx, ts, "reverse_short")
                    position = 0
            else:
                peak_favorable = min(peak_favorable, close)
                trail = peak_favorable + ATR_STOP_MULT * entry_atr
                if close >= trail:
                    _close_trade(trades, position, entry_px, close, entry_idx, ts, "trail_stop")
                    position = 0
                    continue
                if not np.isnan(donch_hi) and close > donch_hi:
                    _close_trade(trades, position, entry_px, close, entry_idx, ts, "reverse_long")
                    position = 0

        # ─── ENTRIES ──────────────────────────────────────────────────
        if position == 0:
            if (
                np.isnan(atr) or np.isnan(donch_hi) or np.isnan(donch_lo) or close <= 0
                or vix < VIX_MIN or vix > VIX_MAX
                or (atr / close * 100) < ATR_FLOOR_PCT
            ):
                continue
            if close > donch_hi:
                position = 1
                entry_px = close
                entry_idx = ts
                peak_favorable = close
                entry_atr = atr
                days_held = 0
            elif close < donch_lo:
                position = -1
                entry_px = close
                entry_idx = ts
                peak_favorable = close
                entry_atr = atr
                days_held = 0

    if position != 0:
        last = df.iloc[-1]
        _close_trade(trades, position, entry_px, last["close"], entry_idx, df.index[-1], "eof")

    if not trades:
        return {"n": 0, "verdict": "NO TRADES"}

    tdf = pd.DataFrame(trades)
    tdf["pnl_pts"] = tdf["exit_px"] - tdf["entry_px"]
    tdf.loc[tdf["side"] == -1, "pnl_pts"] *= -1
    tdf["pnl_gross"] = tdf["pnl_pts"] * (LOTS * LOT_SIZE)
    tdf["cost"] = (tdf["entry_px"] + tdf["exit_px"]) * (LOTS * LOT_SIZE) * (COST_BPS_ROUNDTRIP / 10000)
    tdf["pnl_net"] = tdf["pnl_gross"] - tdf["cost"]
    tdf["d"] = tdf["entry_ts"].apply(lambda x: pd.Timestamp(x).date())

    # Daily aggregation: a trade can span many days; allocate to entry day
    daily_gross = tdf.groupby("d")["pnl_gross"].sum()
    daily_net = tdf.groupby("d")["pnl_net"].sum()
    # Reindex to all calendar days for proper Sharpe denominator
    if len(daily_gross) > 1:
        all_days = pd.date_range(daily_gross.index.min(), daily_gross.index.max(), freq="B")
        daily_gross = daily_gross.reindex(all_days.date, fill_value=0)
        daily_net = daily_net.reindex(all_days.date, fill_value=0)

    def sharpe(s: pd.Series) -> float:
        if len(s) < 2 or s.std() == 0:
            return 0.0
        return float(s.mean() / s.std() * np.sqrt(252))

    return {
        "n": len(tdf),
        "wins_gross": int((tdf["pnl_gross"] > 0).sum()),
        "win_rate_gross": float((tdf["pnl_gross"] > 0).mean() * 100),
        "win_rate_net": float((tdf["pnl_net"] > 0).mean() * 100),
        "total_gross": float(tdf["pnl_gross"].sum()),
        "total_cost": float(tdf["cost"].sum()),
        "total_net": float(tdf["pnl_net"].sum()),
        "best_gross": float(tdf["pnl_gross"].max()),
        "worst_gross": float(tdf["pnl_gross"].min()),
        "mean_gross": float(tdf["pnl_gross"].mean()),
        "mean_net": float(tdf["pnl_net"].mean()),
        "sharpe_gross": sharpe(daily_gross),
        "sharpe_net": sharpe(daily_net),
        "avg_hold_days": float(
            (tdf["exit_ts"] - tdf["entry_ts"]).apply(lambda d: d.total_seconds() / 86400).mean()
        ),
        "exit_reasons": tdf["exit_reason"].value_counts().to_dict(),
    }


def _close_trade(trades, position, entry_px, close, entry_ts, exit_ts, reason):
    trades.append({
        "side": position,
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "entry_px": entry_px,
        "exit_px": close,
        "exit_reason": reason,
    })


def main() -> int:
    if not SPOT_CSV.exists():
        print(f"ERROR: {SPOT_CSV} not found.")
        return 2

    print("Loading + resampling to daily bars...")
    df = load_daily()
    print(f"  Daily bars: {len(df)}")
    print(f"  Range: {df.index[0].date()} → {df.index[-1].date()}")
    print()

    res = run_backtest(df)

    print("=" * 60)
    print("Trend daily — NIFTY 20-day Donchian, multi-day hold")
    print("=" * 60)
    print(f"  Trades:           {res.get('n', 0)}")
    print(f"  Avg hold days:    {res.get('avg_hold_days', 0):.1f}")
    print()
    print(f"  GROSS (no cost):")
    print(f"    Total P&L:      Rs {res.get('total_gross', 0):>12,.0f}")
    print(f"    Mean / trade:   Rs {res.get('mean_gross', 0):>12,.1f}")
    print(f"    Best:           Rs {res.get('best_gross', 0):>12,.0f}")
    print(f"    Worst:          Rs {res.get('worst_gross', 0):>12,.0f}")
    print(f"    Win rate:       {res.get('win_rate_gross', 0):>11.1f}%")
    print(f"    Sharpe (daily): {res.get('sharpe_gross', 0):>11.2f}")
    print()
    print(f"  NET (1 bp round-trip cost):")
    print(f"    Total P&L:      Rs {res.get('total_net', 0):>12,.0f}")
    print(f"    Total cost:     Rs {res.get('total_cost', 0):>12,.0f}")
    print(f"    Mean / trade:   Rs {res.get('mean_net', 0):>12,.1f}")
    print(f"    Win rate:       {res.get('win_rate_net', 0):>11.1f}%")
    print(f"    Sharpe (daily): {res.get('sharpe_net', 0):>11.2f}")
    print()
    print(f"  Exit reason mix:")
    for reason, count in res.get("exit_reasons", {}).items():
        print(f"    {reason:>15s}: {count}")
    print()

    sn = res.get("sharpe_net", 0)
    sg = res.get("sharpe_gross", 0)
    if sn > 0.5:
        print("VERDICT: ✅ NET Sharpe > 0.5 — real edge, formalize")
    elif sn > 0.1:
        print("VERDICT: ⚠️  NET Sharpe in [0.1, 0.5] — marginal")
    elif sg > 0.5 and sn < 0.1:
        print("VERDICT: ⚠️  GROSS edge but cost wall eats it")
    else:
        print("VERDICT: ❌ No meaningful edge")
    return 0


if __name__ == "__main__":
    sys.exit(main())
