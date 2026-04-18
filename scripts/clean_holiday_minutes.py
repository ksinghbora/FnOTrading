"""Strip non-trading-day rows from chain-paired minute CSVs.

The chain-paired spot/VIX CSVs (`data/nifty_spot_minute_chain.csv`,
`data/india_vix_minute_chain.csv`) are written by the same daemon that
records option chains. When the daemon runs on a closed-market day, it
keeps emitting minute rows with the last-known close repeated — 376 flat
rows for a holiday is the typical signature.

Sibling broom to `scripts/quarantine_holiday_chains.py`. That script
moves whole orphan chain CSVs to _quarantine/. This one operates row-by-
row inside the long-running paired CSVs because they aggregate every day
into a single file, so we can't quarantine the whole file.

What it does
------------
For each chain-paired CSV:
  1. Read all rows.
  2. Bucket by date.
  3. For each date, ask MarketClock.is_trading_holiday(). If True, drop
     all rows for that date.
  4. Also drop any day whose close-price range is exactly 0.0 over ≥30
     minutes — that's the daemon's "stale repeat" signature, even on
     dates the holiday calendar somehow misses (calendar updates lag).
  5. Back up the dirty file to `<name>.dirty-<ISO timestamp>.csv` once,
     write the cleaned rows back to the original path atomically.

Idempotent: re-running on a clean file is a no-op (no backup written).

The historical-source CSVs (`data/nifty_spot_minute.csv`,
`data/india_vix_minute.csv`) are NOT touched — those are gold-standard
exchange data, not daemon output. Verified clean by inspection (Apr 18).
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.clock import MarketClock  # noqa: E402

DEFAULT_TARGETS = [
    ROOT / "data" / "nifty_spot_minute_chain.csv",
    ROOT / "data" / "india_vix_minute_chain.csv",
]
FLAT_MIN_ROWS = 30  # Below this, "all-equal close" might just be a quiet hour.


def _bucket_by_day(rows: list[dict]) -> dict[date, list[dict]]:
    out: dict[date, list[dict]] = defaultdict(list)
    for r in rows:
        try:
            d = datetime.fromisoformat(r["date"]).date()
        except (ValueError, KeyError):
            continue
        out[d].append(r)
    return out


def _is_polluted(d: date, day_rows: list[dict], clock: MarketClock) -> tuple[bool, str]:
    """Return (drop?, reason)."""
    if clock.is_trading_holiday(d):
        return True, f"holiday/{d.strftime('%a')}"
    if len(day_rows) >= FLAT_MIN_ROWS:
        try:
            closes = [float(r["close"]) for r in day_rows]
        except (ValueError, KeyError):
            return False, ""
        if closes and (max(closes) - min(closes)) < 0.01:
            return True, f"flat-{len(day_rows)}-rows"
    return False, ""


def _clean_file(path: Path, clock: MarketClock, dry_run: bool) -> dict:
    if not path.exists():
        return {"path": path, "skipped": True, "reason": "missing"}
    with open(path) as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    if not rows or not fieldnames:
        return {"path": path, "skipped": True, "reason": "empty"}

    by_day = _bucket_by_day(rows)
    drop_days: list[tuple[date, str, int]] = []
    keep_rows: list[dict] = []
    for d in sorted(by_day):
        day_rows = by_day[d]
        drop, reason = _is_polluted(d, day_rows, clock)
        if drop:
            drop_days.append((d, reason, len(day_rows)))
        else:
            keep_rows.extend(day_rows)

    if not drop_days:
        return {"path": path, "skipped": True, "reason": "already-clean",
                "rows": len(rows), "days": len(by_day)}

    if dry_run:
        return {"path": path, "skipped": False, "dry_run": True,
                "drop_days": drop_days, "rows_before": len(rows),
                "rows_after": len(keep_rows)}

    # Backup once. If a .dirty-* file already exists from a prior run we
    # leave it — the original may have been reconstructed since then so
    # this run's "dirty" baseline is different.
    ts = datetime.now().strftime("%Y-%m-%dT%H%M%S")
    backup = path.with_name(f"{path.stem}.dirty-{ts}.csv")
    shutil.copy2(path, backup)

    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(keep_rows)
    tmp.replace(path)

    return {"path": path, "skipped": False, "dry_run": False,
            "drop_days": drop_days, "rows_before": len(rows),
            "rows_after": len(keep_rows), "backup": backup}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would be dropped, write nothing")
    p.add_argument("paths", nargs="*",
                   help="CSV paths to clean (default: chain-paired spot+VIX)")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    targets = [Path(p) for p in args.paths] if args.paths else DEFAULT_TARGETS
    clock = MarketClock()

    any_changed = False
    for path in targets:
        result = _clean_file(path, clock, args.dry_run)
        rel = path.relative_to(ROOT) if path.is_absolute() and ROOT in path.parents else path
        if result.get("skipped"):
            print(f"{rel}: skipped ({result['reason']})")
            continue
        any_changed = True
        drop_summary = ", ".join(
            f"{d} ({reason}, {n} rows)" for d, reason, n in result["drop_days"]
        )
        print(f"{rel}:")
        print(f"  rows {result['rows_before']:,} → {result['rows_after']:,} "
              f"(-{result['rows_before'] - result['rows_after']:,})")
        print(f"  dropped days: {drop_summary}")
        if not args.dry_run:
            print(f"  backup → {result['backup'].name}")

    if not any_changed:
        print("\nNothing to clean. All targets already free of holiday/flat rows.")
    elif args.dry_run:
        print("\n[DRY-RUN] Re-run without --dry-run to apply.")
    else:
        print("\nDone. Re-run any A/B replay to confirm metrics still resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
