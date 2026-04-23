"""Extract GDFL daily tick zips from the master archive into per-day parquet.

Streams one day at a time: pulls the inner day-zip from the outer archive,
reads all per-contract CSVs, aggregates to 1-min bars, reconstructs spot
and a VIX proxy, writes a compressed parquet, deletes the temp zip.

Usage:
    uv run python scripts/extract_gdfl.py \
        --archive ~/Downloads/GDFL_master.zip \
        --underlying NIFTY \
        --from 2026-02-01 --to 2026-02-27 \
        --out data/gdfl_snapshots/
"""

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtest.gdfl_loader import extract_and_write_day
from src.core.clock import MarketClock


def parse_args():
    p = argparse.ArgumentParser(description="Extract GDFL daily tick data to parquet")
    p.add_argument("--archive", required=True, help="Path to GDFL master zip")
    p.add_argument("--underlying", default="NIFTY", choices=["NIFTY", "BANKNIFTY"])
    p.add_argument("--from", dest="from_date", required=True, help="YYYY-MM-DD")
    p.add_argument("--to", dest="to_date", required=True, help="YYYY-MM-DD")
    p.add_argument("--out", default="data/gdfl_snapshots/", help="Output dir")
    p.add_argument("--max-strikes", type=int, default=20,
                   help="Keep ATM ± N strikes (default 20)")
    p.add_argument("--max-expiries", type=int, default=2,
                   help="Keep nearest N expiries (default 2)")
    p.add_argument("--overwrite", action="store_true",
                   help="Re-extract days that already have parquet")
    return p.parse_args()


def iter_trading_days(start: date, end: date) -> list[date]:
    clock = MarketClock()
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5 and not clock.is_trading_holiday(d):
            days.append(d)
        d += timedelta(days=1)
    return days


def main():
    args = parse_args()

    log_dir = Path("data/backtest_logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"gdfl_extract_{date.today().isoformat()}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.FileHandler(log_file, mode="a"), logging.StreamHandler()],
    )
    print(f"Logs: {log_file}")

    archive = Path(args.archive).expanduser()
    if not archive.exists():
        sys.exit(f"Archive not found: {archive}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    start = date.fromisoformat(args.from_date)
    end = date.fromisoformat(args.to_date)
    days = iter_trading_days(start, end)
    logging.info(f"Trading days in range: {len(days)} ({start} → {end})")

    for i, day in enumerate(days, 1):
        out_file = out_dir / f"gdfl_{args.underlying.lower()}_{day.isoformat()}.parquet"
        if out_file.exists() and not args.overwrite:
            logging.info(f"[{i}/{len(days)}] Skip (exists): {out_file.name}")
            continue
        logging.info(f"[{i}/{len(days)}] Extracting {day} {args.underlying}")
        try:
            extract_and_write_day(
                archive, args.underlying, day, out_dir,
                max_strikes_per_side=args.max_strikes,
                max_expiries=args.max_expiries,
            )
        except Exception as e:
            logging.error(f"[{i}/{len(days)}] Failed for {day}: {e}", exc_info=True)

    logging.info("Done.")


if __name__ == "__main__":
    main()
