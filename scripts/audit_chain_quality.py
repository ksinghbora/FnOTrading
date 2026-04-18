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
  atm_jump_max  — max % shift in highest-OI strike between adjacent snapshots
                  (Apr 18 2026 — added after Apr 17 chain showed strike 21500
                  ATM at 11:19 then strike 22800 ATM at 11:21, an impossible
                  ~6% jump in 2 minutes. Real NIFTY can't move this fast
                  without a circuit-breaker halt, so >3% per minute = recorder
                  bug or feed glitch.)
  status         — clean | partial | degraded | corrupt | weekend

Status decision matrix:
  weekend         → "weekend"  (always quarantine)
  atm_jump > 3%   → "corrupt"  (ATM strike teleported between snapshots)
  priceable < 50  → "degraded" (do not replay)
  iv < 20         → "degraded" (BS-derived greeks dead)
  bidask < 20     → "partial"  (LTP-only — fills will be optimistic)
  else            → "clean"

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


# Maximum tolerated shift in the highest-OI strike between two adjacent
# snapshots. Calibration note (Apr 18 2026): the highest-OI strike is a
# rough ATM proxy that can legitimately drift 3-9% intraday as positions
# get rolled and the spot moves — those don't deserve a "corrupt" flag.
# But the actual recorder bug we're hunting (Apr 8, Apr 9, Apr 17, etc.)
# produces 50-140% jumps in a single minute, which is physically
# impossible without two completely unrelated chains being stitched
# together. A 10% gate catches all of those without false-positiving on
# normal OI rotation. See module docstring for the trigger story.
ATM_JUMP_CORRUPT_PCT = 10.0


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
    atm_jump_max_pct: float
    atm_jump_at: str  # "HH:MM→HH:MM" when the worst jump happened (or "")
    status: str

    def fmt(self) -> str:
        jump_str = f"jump={self.atm_jump_max_pct:>4.1f}%"
        if self.atm_jump_at:
            jump_str += f"@{self.atm_jump_at}"
        return (
            f"  {self.day} ({self.weekday})  "
            f"rows={self.rows:>6} mins={self.unique_minutes:>4}  "
            f"price={self.priceable_pct:>5.1f}%  "
            f"iv={self.iv_pct:>5.1f}%  "
            f"bidask={self.bidask_pct:>5.1f}%  "
            f"zero_ba={self.zero_bidask_pct:>5.1f}%  "
            f"{jump_str}  "
            f"[{self.status}]"
        )


def classify(
    weekday_idx: int,
    priceable_pct: float,
    iv_pct: float,
    bidask_pct: float,
    atm_jump_max_pct: float,
) -> str:
    if weekday_idx >= 5:
        return "weekend"
    # Corrupt check first — even if all priceability metrics look fine, an
    # ATM teleport means the underlying spot we'd derive is unreliable, so
    # any P&L from this day is meaningless. Quarantine before we waste a
    # replay run on it.
    if atm_jump_max_pct > ATM_JUMP_CORRUPT_PCT:
        return "corrupt"
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

    # Per-snapshot OI accumulator. Key: minute timestamp ("YYYY-MM-DDTHH:MM").
    # Value: dict[strike -> total OI (CE+PE)]. We use the highest-OI strike
    # in each snapshot as the ATM proxy. Max-OI is robust to a few zero/
    # missing legs, unlike "smallest |CE-PE| diff" which dies when half
    # the chain has ltp=0.
    oi_by_minute: dict[str, dict[float, float]] = defaultdict(
        lambda: defaultdict(float)
    )

    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows += 1
            ts_minute = row.get("time", "")[:16]
            unique_minutes.add(ts_minute)

            try:
                ltp = float(row.get("ltp", "0") or 0)
                bid = float(row.get("bid_price", "0") or 0)
                ask = float(row.get("ask_price", "0") or 0)
                iv = float(row.get("iv", "0") or 0)
                strike = float(row.get("strike", "0") or 0)
                oi = float(row.get("oi", "0") or 0)
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

            if strike > 0 and oi > 0 and ts_minute:
                oi_by_minute[ts_minute][strike] += oi

    if rows == 0:
        return DayReport(day_str, weekday, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, "", "degraded")

    # Compute the worst ATM-strike jump between adjacent snapshots. Two
    # snapshots are "adjacent" by sorted timestamp; we don't care about
    # wall-clock gap here because the recorder writes ~once a minute and
    # any hour-long gap during the day is itself a bug worth seeing.
    atm_by_minute: dict[str, float] = {}
    for ts, strike_oi in oi_by_minute.items():
        if strike_oi:
            # Highest-total-OI strike == best ATM proxy from raw chain.
            atm_by_minute[ts] = max(strike_oi.items(), key=lambda kv: kv[1])[0]

    sorted_ts = sorted(atm_by_minute.keys())
    worst_pct = 0.0
    worst_at = ""
    for prev, cur in zip(sorted_ts, sorted_ts[1:]):
        a, b = atm_by_minute[prev], atm_by_minute[cur]
        if a <= 0:
            continue
        jump_pct = abs(b - a) / a * 100.0
        if jump_pct > worst_pct:
            worst_pct = jump_pct
            worst_at = f"{prev[11:]}→{cur[11:]}"

    return DayReport(
        day=day_str,
        weekday=weekday,
        rows=rows,
        unique_minutes=len(unique_minutes),
        priceable_pct=100.0 * priceable / rows,
        iv_pct=100.0 * iv_present / rows,
        bidask_pct=100.0 * bidask_present / rows,
        zero_bidask_pct=100.0 * zero_bidask / rows,
        atm_jump_max_pct=worst_pct,
        atm_jump_at=worst_at,
        status=classify(
            d.weekday(),
            100.0 * priceable / rows,
            100.0 * iv_present / rows,
            100.0 * bidask_present / rows,
            worst_pct,
        ),
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
    for status in ["clean", "partial", "degraded", "corrupt", "weekend"]:
        reports = by_status.get(status, [])
        if not reports:
            continue
        print(f"\n── {status.upper()} ({len(reports)}) ──")
        for r in reports:
            print(r.fmt())

    # Summary
    print("\n── SUMMARY ──")
    for status in ["clean", "partial", "degraded", "corrupt", "weekend"]:
        n = len(by_status.get(status, []))
        if n:
            print(f"  {status:>9}: {n}")

    # Strict mode gate. Corrupt days fail just as hard as degraded ones —
    # we don't want a CI green-light when the recorder is teleporting ATM.
    bad = len(by_status.get("degraded", [])) + len(by_status.get("corrupt", []))
    if args.strict and bad > 0:
        print(f"\nFAIL: {bad} degraded/corrupt day(s) — see above.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
