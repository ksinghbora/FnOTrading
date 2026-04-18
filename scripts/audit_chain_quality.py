"""Audit recorded chain CSVs and produce a per-day quality report.

Why this exists (Apr 17 audit):
  The 23-day chain replay produced obviously wrong P&L because half the
  days were silently degraded:
    - 6 weekend CSVs (NSE closed → simulator data)
    - Mar 26 had 100% zero bid/ask
    - Mar 31 had 100% zero IV
  None of this was visible from the replay output. This script makes it
  visible BEFORE the replay runs.

Per-day metrics:
  weekday        — Mon..Sun (NSE-trading day or not)
  rows           — total leg rows
  unique_minutes — count of distinct timestamps (proxy for snapshot count)
  priceable_pct  — rows with ltp>0 OR (bid>0 AND ask>0)
  iv_pct         — rows with iv>0
  bidask_pct     — rows with both bid>0 AND ask>0
  zero_bidask_pct — rows with both bid==0 AND ask==0
  status         — clean | partial | degraded | weekend

Status decision matrix:
  weekend        → "weekend"  (always quarantine)
  priceable < 50 → "degraded" (do not replay)
  iv < 20        → "degraded" (BS-derived greeks dead)
  bidask < 20    → "partial"  (LTP-only — fills will be optimistic)
  else           → "clean"

Usage:
    uv run python scripts/audit_chain_quality.py [--strict]
    --strict: exit 1 if any day is degraded (CI gate)
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

CHAIN_DIR = Path("data/chain_snapshots")

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class DayReport:
    day: str
    weekday: str
    rows: int
    unique_minutes: int
    priceable_pct: float
    iv_pct: float
    bidask_pct: float
    zero_bidask_pct: float
    status: str

    def fmt(self) -> str:
        return (
            f"  {self.day} ({self.weekday})  "
            f"rows={self.rows:>6} mins={self.unique_minutes:>4}  "
            f"price={self.priceable_pct:>5.1f}%  "
            f"iv={self.iv_pct:>5.1f}%  "
            f"bidask={self.bidask_pct:>5.1f}%  "
            f"zero_ba={self.zero_bidask_pct:>5.1f}%  "
            f"[{self.status}]"
        )


def classify(weekday_idx: int, priceable_pct: float, iv_pct: float, bidask_pct: float) -> str:
    if weekday_idx >= 5:
        return "weekend"
    if priceable_pct < 50:
        return "degraded"
    if iv_pct < 20:
        return "degraded"
    if bidask_pct < 20:
        return "partial"
    return "clean"


def audit_file(path: Path) -> DayReport:
    day_str = path.stem.replace("chain_", "")
    yyyy, mm, dd = day_str.split("-")
    d = date(int(yyyy), int(mm), int(dd))
    weekday = WEEKDAY_NAMES[d.weekday()]

    rows = 0
    unique_minutes: set[str] = set()
    priceable = 0
    iv_present = 0
    bidask_present = 0
    zero_bidask = 0

    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows += 1
            unique_minutes.add(row.get("time", "")[:16])

            try:
                ltp = float(row.get("ltp", "0") or 0)
                bid = float(row.get("bid_price", "0") or 0)
                ask = float(row.get("ask_price", "0") or 0)
                iv = float(row.get("iv", "0") or 0)
            except ValueError:
                continue

            if ltp > 0 or (bid > 0 and ask > 0):
                priceable += 1
            if iv > 0:
                iv_present += 1
            if bid > 0 and ask > 0:
                bidask_present += 1
            if bid == 0 and ask == 0:
                zero_bidask += 1

    if rows == 0:
        return DayReport(day_str, weekday, 0, 0, 0.0, 0.0, 0.0, 0.0, "degraded")

    return DayReport(
        day=day_str,
        weekday=weekday,
        rows=rows,
        unique_minutes=len(unique_minutes),
        priceable_pct=100.0 * priceable / rows,
        iv_pct=100.0 * iv_present / rows,
        bidask_pct=100.0 * bidask_present / rows,
        zero_bidask_pct=100.0 * zero_bidask / rows,
        status=classify(d.weekday(), 100.0 * priceable / rows,
                        100.0 * iv_present / rows, 100.0 * bidask_present / rows),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true",
                        help="Exit 1 if any non-weekend day is degraded.")
    parser.add_argument("--dir", default=str(CHAIN_DIR),
                        help="Chain snapshot directory (default: data/chain_snapshots)")
    args = parser.parse_args()

    chain_dir = Path(args.dir)
    files = sorted(chain_dir.glob("chain_*.csv"))
    if not files:
        print(f"No chain CSVs found in {chain_dir}", file=sys.stderr)
        return 1

    print(f"Auditing {len(files)} chain CSVs in {chain_dir}\n")

    by_status: dict[str, list[DayReport]] = defaultdict(list)
    for path in files:
        rep = audit_file(path)
        by_status[rep.status].append(rep)

    # Print per-status sections
    for status in ["clean", "partial", "degraded", "weekend"]:
        reports = by_status.get(status, [])
        if not reports:
            continue
        print(f"\n── {status.upper()} ({len(reports)}) ──")
        for r in reports:
            print(r.fmt())

    # Summary
    print("\n── SUMMARY ──")
    for status in ["clean", "partial", "degraded", "weekend"]:
        n = len(by_status.get(status, []))
        if n:
            print(f"  {status:>9}: {n}")

    # Strict mode gate
    bad = len(by_status.get("degraded", []))
    if args.strict and bad > 0:
        print(f"\nFAIL: {bad} degraded day(s) — see above.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
