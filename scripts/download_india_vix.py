"""Download India VIX daily closes from NSE archives.

Two modes:

1. **Resample existing minute file (default)** — walk through
   ``data/india_vix_minute.csv`` and take the last close per trading day.
   This runs offline and is the preferred path while the minute file is
   already in the repo.

2. **Fetch NSE archive (``--source nse``)** — NSE publishes a daily volatility
   report at
   ``https://archives.nseindia.com/archives/nsccl/volt/CM_VOLT_DDMMYYYY.csv``
   (renamed to ``FOVOLT_DDMMYYYY.csv`` post-2024, layout unchanged). The file
   includes the India VIX close for each symbol. This mode walks the date
   range, downloads each file, and extracts ``IndiaVIX`` + ``Close``.

Both modes write a two-column CSV ``data/india_vix_daily.csv`` with schema
``date, close``.

Usage::

    uv run python -m scripts.download_india_vix                    # resample
    uv run python -m scripts.download_india_vix --source nse \\
        --from 2024-01-01 --to 2026-04-23
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import requests
except ImportError:  # requests is already in the project, but be defensive
    requests = None  # type: ignore[assignment]

from src.data.india_vix_loader import resample_minute_to_daily

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("download_india_vix")


NSE_URL_TEMPLATE = (
    "https://archives.nseindia.com/archives/nsccl/volt/CM_VOLT_{ddmmyyyy}.csv"
)
NSE_URL_ALT = (
    "https://archives.nseindia.com/archives/nsccl/volt/FOVOLT_{ddmmyyyy}.csv"
)


def _daterange(start: date, end: date):
    cur = start
    while cur <= end:
        if cur.weekday() < 5:  # weekdays only
            yield cur
        cur = cur + timedelta(days=1)


def _fetch_nse_volatility_row(target: date) -> float | None:
    """Fetch a single NSE CM_VOLT file and return the IndiaVIX close."""
    if requests is None:
        raise RuntimeError("requests is not installed; pip/uv add requests")
    ddmmyyyy = target.strftime("%d%m%Y")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
        ),
        "Accept": "text/csv,application/csv,*/*",
    }
    for url_tpl in (NSE_URL_TEMPLATE, NSE_URL_ALT):
        url = url_tpl.format(ddmmyyyy=ddmmyyyy)
        try:
            resp = requests.get(url, headers=headers, timeout=10)
        except Exception as exc:
            logger.warning("GET %s failed: %s", url, exc)
            continue
        if resp.status_code != 200 or not resp.text.strip():
            continue
        # Header columns vary by year — scan for a row whose symbol matches.
        for raw_line in resp.text.splitlines():
            parts = [p.strip() for p in raw_line.split(",")]
            if not parts:
                continue
            # Common layouts put the symbol in column 1 and close in the last
            # or one-before-last numeric column. Match loosely.
            if parts[0].lower() in ("indiavix", "india_vix", "india vix"):
                for field in reversed(parts):
                    try:
                        return float(field)
                    except ValueError:
                        continue
    return None


def _download_nse_range(start: date, end: date, out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[date, float] = {}
    if out_path.exists():
        import csv
        with out_path.open("r", newline="") as fh:
            reader = csv.reader(fh)
            headers = next(reader, None)
            for row in reader:
                if len(row) < 2:
                    continue
                try:
                    d = datetime.fromisoformat(row[0]).date()
                    existing[d] = float(row[1])
                except Exception:
                    continue
    new_rows = 0
    for day in _daterange(start, end):
        if day in existing:
            continue
        val = _fetch_nse_volatility_row(day)
        if val is not None:
            existing[day] = val
            new_rows += 1
            logger.info("NSE %s -> %.2f", day, val)
        else:
            logger.debug("NSE %s -> no row", day)
        time.sleep(0.5)  # polite to NSE
    # Re-write the file sorted by date
    rows = sorted(existing.items())
    with out_path.open("w") as fh:
        fh.write("date,close\n")
        for d, v in rows:
            fh.write(f"{d.isoformat()},{v:.4f}\n")
    logger.info("Wrote %d rows (%d new) to %s", len(rows), new_rows, out_path)
    return new_rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", choices=("minute", "nse"), default="minute",
        help="'minute' resamples data/india_vix_minute.csv (default); "
             "'nse' fetches daily NSE archives over the provided range.",
    )
    parser.add_argument(
        "--minute-csv", default="data/india_vix_minute.csv",
        help="Input minute CSV when source=minute",
    )
    parser.add_argument(
        "--out", default="data/india_vix_daily.csv",
        help="Output CSV path",
    )
    parser.add_argument("--from", dest="start", help="Start date YYYY-MM-DD (NSE mode)")
    parser.add_argument("--to", dest="end", help="End date YYYY-MM-DD (NSE mode)")
    args = parser.parse_args(argv)

    out_path = Path(args.out)

    if args.source == "minute":
        mp = Path(args.minute_csv)
        if not mp.exists():
            logger.error("Minute CSV not found: %s", mp)
            return 2
        n = resample_minute_to_daily(mp, out_path)
        logger.info("Wrote %d daily rows from %s -> %s", n, mp, out_path)
        return 0

    # NSE mode
    if not args.start or not args.end:
        parser.error("--from and --to are required in NSE mode")
    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    new_rows = _download_nse_range(start, end, out_path)
    logger.info("NSE download complete — %d new rows", new_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
