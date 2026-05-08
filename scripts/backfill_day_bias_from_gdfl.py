#!/usr/bin/env python
"""Backfill synthetic DayBias for the 173-day GDFL post-SEBI corpus.

The default backfill_day_bias.py reads from data/nifty_spot_minute.csv +
data/india_vix_minute.csv, which only cover the recent live-recording
window (Mar-Apr 2026). For the GDFL corpus (Nov 2024 → Feb 2026) we read
spot/VIX directly from the GDFL parquet files.

Reuses the same _bias_for_day rule from backfill_day_bias.py — VIX-based
synthetic, not an AI prediction. Used to test bias-integration plumbing
on the long GDFL backtest window.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backfill_day_bias import _bias_for_day  # noqa: E402


GDFL_DIR = Path("data/gdfl_v2")
OUT_DIR = Path("data/day_bias_backfill_gdfl")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    parquets = sorted(GDFL_DIR.glob("gdfl_nifty_*.parquet"))
    print(f"Found {len(parquets)} NIFTY parquet days in {GDFL_DIR}")

    if not parquets:
        print("ERROR: no GDFL parquets found", file=sys.stderr)
        return 1

    # Two-pass: load all days first to compute prev_close from prior day
    aggregates: dict[date, dict] = {}
    for pq in parquets:
        try:
            df = pd.read_parquet(pq, columns=["time", "spot", "vix"])
        except Exception as e:
            print(f"  ! {pq.name}: {e}", file=sys.stderr)
            continue
        if df.empty:
            continue
        # Filter to one row per minute (just take spot/VIX of first occurrence)
        df = df.dropna(subset=["spot", "vix"]).sort_values("time")
        if df.empty:
            continue
        first = df.iloc[0]
        last = df.iloc[-1]
        d = first["time"].date()
        aggregates[d] = {
            "spot_open": float(first["spot"]),
            "spot_close": float(last["spot"]),
            "vix_open": float(first["vix"]),
            "vix_close": float(last["vix"]),
        }

    days = sorted(aggregates.keys())
    if not days:
        print("ERROR: parsed 0 days from parquets", file=sys.stderr)
        return 1
    print(f"Window: {days[0]} → {days[-1]} ({len(days)} days)")

    # Backfill: for each day, fill prev_close from prior day's close
    written = 0
    counts = {"skip_premium": 0, "iron_condor": 0, "strangle": 0}
    prev_close: float | None = None
    for d in days:
        agg = aggregates[d]
        # _bias_for_day expects: open_spot, close_spot, prev_close_spot, open_vix, close_vix
        bias_input = {
            "open_spot": agg["spot_open"],
            "close_spot": agg["spot_close"],
            "prev_close_spot": prev_close if prev_close is not None else agg["spot_open"],
            "open_vix": agg["vix_open"],
            "close_vix": agg["vix_close"],
        }
        bias = _bias_for_day(d, bias_input)
        counts[bias["mode_bias"]] = counts.get(bias["mode_bias"], 0) + 1
        out_path = OUT_DIR / f"day_bias_{d.isoformat()}.json"
        out_path.write_text(json.dumps(bias, indent=2))
        written += 1
        prev_close = agg["spot_close"]

    print(f"Wrote {written} JSON files to {OUT_DIR}")
    print(f"  skip_premium: {counts.get('skip_premium', 0)} days  (VIX > 18)")
    print(f"  iron_condor:  {counts.get('iron_condor', 0)} days   (13 ≤ VIX ≤ 18)")
    print(f"  strangle:     {counts.get('strangle', 0)} days      (VIX < 13)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
