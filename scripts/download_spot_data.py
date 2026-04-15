"""Download NIFTY spot + India VIX minute data from Kite for backtesting.

Saves to CSV files in data/ directory. No database required.
Requires valid Kite access token (authenticate first).

Usage:
    uv run python scripts/download_spot_data.py
    uv run python scripts/download_spot_data.py --days 365
    uv run python scripts/download_spot_data.py --underlying BANKNIFTY --days 90
"""

import argparse
import asyncio
import csv
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from kiteconnect import KiteConnect


# Known instrument tokens for indices
SPOT_TOKENS = {
    "NIFTY": 256265,       # NIFTY 50 index
    "BANKNIFTY": 260105,   # NIFTY BANK index
    "FINNIFTY": 257801,    # NIFTY FIN SERVICE index
}
VIX_TOKEN = 264969  # INDIA VIX


def parse_args():
    parser = argparse.ArgumentParser(description="Download spot + VIX data for backtesting")
    parser.add_argument("--underlying", default="NIFTY", choices=["NIFTY", "BANKNIFTY", "FINNIFTY"])
    parser.add_argument("--days", type=int, default=180, help="Days of history (default: 180)")
    parser.add_argument("--interval", default="minute", help="minute, 5minute, 15minute, etc.")
    return parser.parse_args()


def get_kite() -> KiteConnect:
    """Create authenticated Kite client from .env or .kite_access_token."""
    api_key = os.environ.get("KITE_API_KEY", "")
    access_token = os.environ.get("KITE_ACCESS_TOKEN", "")

    # Try .env file
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("KITE_API_KEY="):
                api_key = line.split("=", 1)[1].strip()
            elif line.startswith("KITE_ACCESS_TOKEN="):
                access_token = line.split("=", 1)[1].strip()

    # Try token file
    token_file = Path(__file__).parent.parent / ".kite_access_token"
    if not access_token and token_file.exists():
        access_token = token_file.read_text().strip()

    if not api_key or not access_token:
        print("ERROR: KITE_API_KEY and KITE_ACCESS_TOKEN required.")
        print("Run the server and authenticate first, or set them in .env")
        sys.exit(1)

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite


def download_historical(
    kite: KiteConnect,
    instrument_token: int,
    name: str,
    from_date: datetime,
    to_date: datetime,
    interval: str = "minute",
) -> list[dict]:
    """Download historical data in chunks (Kite limits minute data to 60 days)."""
    chunk_days = 60 if interval == "minute" else 365
    all_data = []
    current_from = from_date

    while current_from < to_date:
        current_to = min(current_from + timedelta(days=chunk_days), to_date)
        try:
            data = kite.historical_data(
                instrument_token=instrument_token,
                from_date=current_from,
                to_date=current_to,
                interval=interval,
                oi=True,
            )
            all_data.extend(data)
            print(f"  {name}: {current_from.date()} to {current_to.date()}: {len(data)} candles")
        except Exception as e:
            print(f"  {name}: ERROR {current_from.date()} to {current_to.date()}: {e}")

        current_from = current_to + timedelta(days=1)
        time.sleep(0.5)  # Rate limit

    return all_data


def save_csv(data: list[dict], filepath: Path):
    """Save candle data to CSV."""
    if not data:
        print(f"  No data to save for {filepath}")
        return

    filepath.parent.mkdir(parents=True, exist_ok=True)

    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "volume", "oi"])
        writer.writeheader()
        for row in data:
            writer.writerow({
                "date": row["date"].isoformat() if hasattr(row["date"], "isoformat") else row["date"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row.get("volume", 0),
                "oi": row.get("oi", 0),
            })

    print(f"  Saved {len(data)} candles to {filepath}")


def main():
    args = parse_args()
    kite = get_kite()

    # Verify connection
    try:
        profile = kite.profile()
        print(f"Authenticated as: {profile.get('user_name', 'unknown')}")
    except Exception as e:
        print(f"ERROR: Kite authentication failed: {e}")
        print("Re-authenticate first.")
        sys.exit(1)

    to_date = datetime.now()
    from_date = to_date - timedelta(days=args.days)

    data_dir = Path(__file__).parent.parent / "data"
    spot_token = SPOT_TOKENS[args.underlying]

    print(f"\nDownloading {args.underlying} + VIX data")
    print(f"Period: {from_date.date()} to {to_date.date()} ({args.days} days)")
    print(f"Interval: {args.interval}")
    print()

    # 1. Download spot data
    print(f"1. Downloading {args.underlying} spot...")
    spot_data = download_historical(kite, spot_token, args.underlying, from_date, to_date, args.interval)
    spot_file = data_dir / f"{args.underlying.lower()}_spot_{args.interval}.csv"
    save_csv(spot_data, spot_file)

    # 2. Download VIX data
    print(f"\n2. Downloading India VIX...")
    vix_data = download_historical(kite, VIX_TOKEN, "VIX", from_date, to_date, args.interval)
    vix_file = data_dir / f"india_vix_{args.interval}.csv"
    save_csv(vix_data, vix_file)

    # Summary
    print(f"\n{'=' * 60}")
    print(f"  Download complete!")
    print(f"  Spot candles:  {len(spot_data)}")
    print(f"  VIX candles:   {len(vix_data)}")
    print(f"  Data dir:      {data_dir}")
    print(f"{'=' * 60}")

    # Trading day count
    if spot_data:
        dates = set()
        for d in spot_data:
            dt = d["date"]
            if hasattr(dt, "date"):
                dates.add(dt.date())
            else:
                dates.add(datetime.fromisoformat(str(dt)).date())
        print(f"  Trading days:  {len(dates)}")


if __name__ == "__main__":
    main()
