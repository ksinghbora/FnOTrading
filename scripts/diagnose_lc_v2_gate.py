#!/usr/bin/env python
"""Diagnose how often CI≥61.8 AND VRP<0 co-occur on Indian post-SEBI data.

The v1 principled IC gate (ADX+BB+RV/IV) fired 0/2590 valid samples
because its three conditions were negatively correlated on Indian data.
LC v2 (CI≥61.8 AND VRP<0) might face a similar trap: range-bound markets
on Indian post-SEBI tend to also have rich IV (VRP>0), not cheap IV.

This script samples the regime detector at, say, 12:00 IST on each day
of the post-SEBI corpus and prints:
  - count of (range, vol-rich) days   → IC v2 territory
  - count of (range, vol-cheap) days  → LC v2 territory
  - count of (trend, anything) days   → neither fires
  - histogram of CI and VRP

If LC v2 territory is < 5 days out of 173, the gate is empirically dead
for the same negative-correlation reason as v1.

Reads the existing GDFL parquet directly — no full backtest needed.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections import Counter
from datetime import date, datetime, time, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytz

from src.backtest.engine import BacktestEngine, _import_strategies
from src.backtest.gdfl_market_source import GDFLMarketSource
from src.market_data.simulator import NIFTY_SPOT_TOKEN


PARQUET_DIR = "data/gdfl_v2"
SMOKE_DAYS = 173
SMOKE_START = date(2024, 11, 20)
IST = pytz.timezone("Asia/Kolkata")


async def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    print(f"Diagnosing LC v2 gate on post-SEBI corpus ({SMOKE_DAYS} days)...")
    print()

    # Use the smoke harness that already runs the gate so we collect
    # the gate's actual decisions.
    _import_strategies()
    source = GDFLMarketSource(PARQUET_DIR, "NIFTY", NIFTY_SPOT_TOKEN)
    avail = source.available_days()
    days_to_run = [d for d in avail if d >= SMOKE_START][:SMOKE_DAYS]
    if not days_to_run:
        print("No days in window")
        return 2
    print(f"Running on {len(days_to_run)} days: {days_to_run[0]} → {days_to_run[-1]}")
    print()

    # Re-use the existing smoke result file if available — easier than
    # re-instrumenting. Look at the smoke output for "ci=" lines.
    smoke_log = Path(
        "/private/tmp/claude-501/-Users-kundanbora-code-FnOTrading/"
        "4ee54a90-a8c1-4813-8795-db9bfe0b7645/tasks/b82dh7dba.output"
    )
    if not smoke_log.exists():
        print(f"Smoke log not found: {smoke_log}")
        print("Run scripts/smoke_lc_v2.py first.")
        return 2

    # Parse "ci=X.X(OK|FAIL) vrp=±Y.Y(OK_LV|FAIL_LV)" reasons from the smoke log
    import re
    reason_pat = re.compile(
        r"long-vol regime gate ci=([\-\d.]+)\(([A-Z_]+)\) vrp=([+\-\d.]+)\(([A-Z_]+)\)"
    )
    ci_buckets: list[float] = []
    vrp_buckets: list[float] = []
    quad_counts = Counter()  # (ci_ok, vrp_ok)
    insufficient = 0

    with smoke_log.open() as f:
        for line in f:
            if "insufficient_data" in line and "long-vol regime gate" in line:
                insufficient += 1
                continue
            m = reason_pat.search(line)
            if not m:
                continue
            ci = float(m.group(1))
            ci_status = m.group(2)
            vrp = float(m.group(3))
            vrp_status = m.group(4)
            ci_buckets.append(ci)
            vrp_buckets.append(vrp)
            quad_counts[(ci_status == "OK", vrp_status == "OK_LV")] += 1

    total_decisions = sum(quad_counts.values()) + insufficient
    print(f"Total gate decisions parsed: {total_decisions}")
    print(f"  insufficient_data:        {insufficient}")
    print(f"  with ci/vrp values:       {sum(quad_counts.values())}")
    print()

    if not ci_buckets:
        print("No (ci, vrp) decisions to analyze. Smoke log incomplete?")
        return 1

    print("Quadrant counts (ci_ok = CI ≥ 61.8, vrp_ok_lv = VRP < 0):")
    print(f"  (range,    cheap)  → LC v2 fires:    {quad_counts[(True, True)]}")
    print(f"  (range,    rich)   → IC v2 fires:    {quad_counts[(True, False)]}")
    print(f"  (trending, cheap)  → neither:        {quad_counts[(False, True)]}")
    print(f"  (trending, rich)   → neither:        {quad_counts[(False, False)]}")
    print()

    # Histograms
    def hist(values: list[float], buckets: list[tuple[float, float]], label: str):
        print(f"{label} histogram (n={len(values)}):")
        for lo, hi in buckets:
            count = sum(1 for v in values if lo <= v < hi)
            pct = 100.0 * count / max(1, len(values))
            bar = "█" * int(pct / 2)
            print(f"  [{lo:>5.1f}, {hi:>5.1f}): {count:>5d} ({pct:>5.1f}%) {bar}")
        print()

    hist(
        ci_buckets,
        [(0, 20), (20, 38.2), (38.2, 50), (50, 61.8), (61.8, 80), (80, 100)],
        "CI",
    )
    hist(
        vrp_buckets,
        [(-10, -5), (-5, -2), (-2, -0.5), (-0.5, 0), (0, 1), (1, 3), (3, 6), (6, 12)],
        "VRP",
    )

    # Verdict
    lc_fires = quad_counts[(True, True)]
    ic_fires = quad_counts[(True, False)]
    print(f"FINDING:")
    if lc_fires == 0:
        print(f"  LC v2 territory (range + IV-cheap) appeared 0 times.")
        print(f"  Same negative-correlation trap as the v1 principled gate.")
        print(f"  IC v2 territory (range + IV-rich) appeared {ic_fires} times")
        print(f"  → Indian post-SEBI markets are essentially never 'range + IV-cheap'.")
    elif lc_fires < 5:
        print(f"  LC v2 territory appeared {lc_fires} times. Sample-thin.")
        print(f"  Probably not enough to support a tradeable LC v2 strategy.")
    else:
        print(f"  LC v2 territory appeared {lc_fires} times across {total_decisions} decisions.")
        print(f"  Gate is alive — proceed to formal validation.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
