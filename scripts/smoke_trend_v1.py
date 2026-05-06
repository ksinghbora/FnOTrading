#!/usr/bin/env python
"""Trend-following smoke (v1) on NIFTY spot 1-min bars — futures-equivalent.

Standalone backtest harness. Bypasses the BacktestEngine entirely
because:
  - The trend signal is single-instrument (NIFTY only, no option chain)
  - We just want signal-edge measurement, not full execution simulation
  - Pandas is much faster than tick-by-tick replay

Signal mechanic (per PIVOT_DESIGN_trend_futures.md):
  Entry long:  close > 20-bar high of last 21 closes,
               AND VIX in [12, 22],
               AND ATR(14)/spot > 0.5%,
               AND time in [09:30, 14:30]
  Entry short: symmetric
  Exit:        ATR(14) trailing stop at 2× ATR
               OR time-stop at 14:45
               OR opposite breakout

Cost model:
  Pre-cost: clean signal Sharpe
  Post-cost: 1 bp slippage round-trip on entry+exit (futures-realistic)

Usage:
    uv run python scripts/smoke_trend_v1.py
"""

from __future__ import annotations

import sys
from datetime import time as dtime
from pathlib import Path

import numpy as np
import pandas as pd


SPOT_CSV = Path("data/nifty_spot_minute.csv")
VIX_CSV = Path("data/india_vix_minute.csv")

# Signal parameters
# NOTE on ATR_FLOOR_PCT: the pivot doc specified 0.5% of spot, but
# empirically NIFTY 1-min ATR(14) sits at ~0.035% median (max 0.42%).
# The 0.5% threshold was implicitly written for daily/5-min bars. On
# 1-min the right "minimum tradeable range" floor is ~0.05% (slightly
# above the 25th-percentile of post-SEBI ATR distribution). This is
# the only parameter that needed empirical recalibration; Donchian
# lookback + ATR_STOP_MULT + VIX band kept at literature defaults.
DONCHIAN_LOOKBACK = 20      # 20-bar high/low (uses last 21 closes)
ATR_PERIOD = 14             # Wilder's smoothing
ATR_FLOOR_PCT = 0.05        # Min ATR%/spot — recalibrated for 1-min bars
ATR_STOP_MULT = 2.0         # Trailing stop at 2× ATR
VIX_MIN = 12.0
VIX_MAX = 22.0
ENTRY_START = dtime(9, 30)  # No open-auction noise
ENTRY_END = dtime(14, 30)   # Last entry time
HARD_EXIT = dtime(14, 45)   # Square off intraday

# Cost model
COST_BPS_ROUNDTRIP = 1.0    # 1 basis point round-trip on futures
LOT_SIZE = 75               # NIFTY current lot
LOTS = 1


def load_data(resample: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load + align NIFTY spot and VIX minute bars.

    `resample`: pandas offset alias like "5min", "15min". When set,
    1-min bars are aggregated to the chosen timeframe (OHLC for spot,
    last for VIX). Higher timeframes reduce microstructure noise that
    dominates 1-min Donchian breakouts on NIFTY post-SEBI.
    """
    spot = pd.read_csv(SPOT_CSV, parse_dates=["date"])
    vix = pd.read_csv(VIX_CSV, parse_dates=["date"])
    spot = spot.set_index("date").sort_index()
    vix = vix.set_index("date").sort_index()
    spot = spot[["open", "high", "low", "close"]]
    vix = vix[["close"]].rename(columns={"close": "vix"})
    df = spot.join(vix, how="left")
    df["vix"] = df["vix"].ffill()
    df = df.dropna()

    if resample is not None:
        agg = {
            "open": "first", "high": "max", "low": "min", "close": "last",
            "vix": "last",
        }
        df = df.resample(resample, label="right", closed="right").agg(agg).dropna()

    return df, vix


def compute_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """Wilder's ATR — smoothed True Range over `period` bars."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    # Wilder's smoothing: alpha = 1/period, so ewm with adjust=False
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    return atr


def compute_donchian(df: pd.DataFrame, lookback: int = DONCHIAN_LOOKBACK):
    """Rolling 20-bar high/low (excluding the current bar — shift by 1)."""
    # `lookback` previous bars, NOT including current
    high_n = df["high"].shift(1).rolling(lookback).max()
    low_n = df["low"].shift(1).rolling(lookback).min()
    return high_n, low_n


def run_backtest(df: pd.DataFrame) -> dict:
    """Walk minute by minute; long/short on Donchian breakout; ATR trailing stop."""
    df = df.copy()
    df["atr"] = compute_atr(df)
    high_n, low_n = compute_donchian(df)
    df["donchian_high"] = high_n
    df["donchian_low"] = low_n

    df["t"] = df.index.time
    df["d"] = df.index.date

    trades: list[dict] = []
    position = 0          # +1 long, -1 short, 0 flat
    entry_px = 0.0
    entry_idx = None
    peak_favorable = 0.0  # for trailing stop
    entry_atr = 0.0       # ATR at entry, used for stop calculation

    for i, row in enumerate(df.itertuples()):
        ts = row.Index
        close = row.close
        atr = row.atr
        vix = row.vix
        donch_hi = row.donchian_high
        donch_lo = row.donchian_low
        t = ts.time()

        # ─── EXIT CHECKS (priority over entry on same bar) ────────────
        if position != 0:
            # Hard time stop
            if t >= HARD_EXIT:
                _close_trade(trades, position, entry_px, close, entry_idx, ts, "time_stop")
                position = 0
                continue

            # Trailing stop logic
            if position > 0:
                peak_favorable = max(peak_favorable, close)
                trail = peak_favorable - ATR_STOP_MULT * entry_atr
                if close <= trail:
                    _close_trade(trades, position, entry_px, close, entry_idx, ts, "trail_stop")
                    position = 0
                    continue
                # Reverse on opposite breakout
                if not np.isnan(donch_lo) and close < donch_lo:
                    _close_trade(trades, position, entry_px, close, entry_idx, ts, "reverse_short")
                    position = 0
                    # Fall through to maybe open a SHORT below
            else:  # short
                peak_favorable = min(peak_favorable, close)
                trail = peak_favorable + ATR_STOP_MULT * entry_atr
                if close >= trail:
                    _close_trade(trades, position, entry_px, close, entry_idx, ts, "trail_stop")
                    position = 0
                    continue
                if not np.isnan(donch_hi) and close > donch_hi:
                    _close_trade(trades, position, entry_px, close, entry_idx, ts, "reverse_long")
                    position = 0

        # ─── ENTRY CHECKS ─────────────────────────────────────────────
        if position == 0 and ENTRY_START <= t <= ENTRY_END:
            # All gates must pass
            if (
                np.isnan(atr) or np.isnan(donch_hi) or np.isnan(donch_lo) or close <= 0
                or vix < VIX_MIN or vix > VIX_MAX
                or (atr / close * 100) < ATR_FLOOR_PCT
            ):
                continue
            # Long breakout
            if close > donch_hi:
                position = 1
                entry_px = close
                entry_idx = ts
                peak_favorable = close
                entry_atr = atr
            elif close < donch_lo:
                position = -1
                entry_px = close
                entry_idx = ts
                peak_favorable = close
                entry_atr = atr

    # Force-close any open at end
    if position != 0:
        last = df.iloc[-1]
        _close_trade(trades, position, entry_px, last["close"], entry_idx, df.index[-1], "eof")

    # ─── Compute metrics ──────────────────────────────────────────────
    if not trades:
        return {"n": 0, "verdict": "NO TRADES"}

    tdf = pd.DataFrame(trades)
    # Per-trade PnL in spot points
    tdf["pnl_pts"] = tdf["exit_px"] - tdf["entry_px"]
    tdf.loc[tdf["side"] == -1, "pnl_pts"] *= -1
    # PnL in rupees (1 lot = 75 shares)
    tdf["pnl_gross"] = tdf["pnl_pts"] * (LOTS * LOT_SIZE)
    # Cost in rupees: bp on entry + bp on exit
    tdf["cost"] = (
        (tdf["entry_px"] + tdf["exit_px"]) * (LOTS * LOT_SIZE) * (COST_BPS_ROUNDTRIP / 10000)
    )
    tdf["pnl_net"] = tdf["pnl_gross"] - tdf["cost"]

    # Sharpe — daily aggregation
    tdf["d"] = tdf["entry_ts"].apply(lambda x: pd.Timestamp(x).date())
    daily_gross = tdf.groupby("d")["pnl_gross"].sum()
    daily_net = tdf.groupby("d")["pnl_net"].sum()

    def sharpe(s: pd.Series) -> float:
        if len(s) < 2 or s.std() == 0:
            return 0.0
        return float(s.mean() / s.std() * np.sqrt(252))

    return {
        "n": len(tdf),
        "wins_gross": int((tdf["pnl_gross"] > 0).sum()),
        "losses_gross": int((tdf["pnl_gross"] <= 0).sum()),
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
        "trading_days": len(daily_net),
        "trades_per_day": len(tdf) / max(1, len(daily_net)),
        "exit_reasons": tdf["exit_reason"].value_counts().to_dict(),
    }


def _close_trade(
    trades: list,
    position: int,
    entry_px: float,
    close: float,
    entry_ts,
    exit_ts,
    reason: str,
):
    trades.append({
        "side": position,
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "entry_px": entry_px,
        "exit_px": close,
        "exit_reason": reason,
    })


def main() -> int:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--timeframe", default="1min", help="1min | 5min | 15min | 30min")
    p.add_argument("--atr-floor-pct", type=float, default=None,
                   help="Override ATR_FLOOR_PCT (auto-scales with timeframe by default)")
    args = p.parse_args()

    if not SPOT_CSV.exists():
        print(f"ERROR: {SPOT_CSV} not found. Run scripts/download_spot_data.py first.")
        return 2

    resample = None if args.timeframe == "1min" else args.timeframe
    print(f"Loading data (timeframe={args.timeframe})...")
    df, _ = load_data(resample=resample)
    print(f"  Bars: {len(df):,}")
    print(f"  Range: {df.index[0]} → {df.index[-1]}")
    print(f"  Trading days: {df.index.normalize().nunique()}")

    # Auto-scale ATR floor with timeframe (NIFTY median ATR/spot at 1-min
    # ~0.035%; scales as sqrt(timeframe minutes))
    global ATR_FLOOR_PCT
    if args.atr_floor_pct is not None:
        ATR_FLOOR_PCT = args.atr_floor_pct
    else:
        scale = {"1min": 1.0, "5min": np.sqrt(5), "15min": np.sqrt(15), "30min": np.sqrt(30)}
        ATR_FLOOR_PCT = 0.05 * scale.get(args.timeframe, 1.0)
    print(f"  ATR_FLOOR_PCT: {ATR_FLOOR_PCT:.3f}%")
    print()
    print("Running backtest...")

    res = run_backtest(df)

    print()
    print("=" * 60)
    print("Trend v1 — NIFTY 1-min Donchian breakout (signal-only)")
    print("=" * 60)
    print(f"  Trades:           {res.get('n', 0)}")
    print(f"  Trading days:     {res.get('trading_days', 0)}")
    print(f"  Trades/day:       {res.get('trades_per_day', 0):.2f}")
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

    # Verdict per pivot doc thresholds
    sg = res.get("sharpe_gross", 0)
    sn = res.get("sharpe_net", 0)
    if sn > 0.5:
        print("VERDICT: ✅ NET Sharpe > 0.5 — real edge, move to formal validation")
    elif sn > 0.1:
        print("VERDICT: ⚠️  NET Sharpe in [0.1, 0.5] — marginal; consider v2 enhancements")
    elif sg > 0.5 and sn < 0.1:
        print("VERDICT: ⚠️  GROSS edge but cost wall eats it — typical post-SEBI options pattern")
        print("         For futures cost basis (1bp), this means signal genuinely has limited edge")
    else:
        print("VERDICT: ❌ Signal has no meaningful edge even pre-cost — pivot")
    return 0


if __name__ == "__main__":
    sys.exit(main())
