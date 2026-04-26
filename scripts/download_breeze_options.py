#!/usr/bin/env python3
"""Download historical NIFTY option chain data from ICICI Breeze API.

Converts to our chain snapshot CSV format for use with ReplayBacktestEngine.

Setup:
    1. Open free ICICI Direct demat account
    2. Enable API: https://api.icicidirect.com/apiuser/home
    3. Get API key + secret
    4. Add to .env: BREEZE_API_KEY, BREEZE_API_SECRET, BREEZE_SESSION_TOKEN

Usage:
    uv run python scripts/download_breeze_options.py --days 30
    uv run python scripts/download_breeze_options.py --start 2025-09-01 --end 2026-03-25
    uv run python scripts/download_breeze_options.py --days 90 --underlying BANKNIFTY
"""

import argparse
import csv
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pyotp

sys.path.insert(0, str(Path(__file__).parent.parent))

from breeze_connect import BreezeConnect

logger = logging.getLogger(__name__)

# NIFTY expiry: Tuesday (weekly), strike step: 50
STRIKE_STEP = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50}
NUM_STRIKES_EACH_SIDE = 20  # ±20 strikes from ATM


def load_env():
    """Load .env file."""
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                if value.strip():
                    os.environ[key.strip()] = value.strip()


def get_trading_days(start: date, end: date) -> list[date]:
    """Get trading days (exclude weekends and known holidays)."""
    from src.core.constants import NSE_HOLIDAYS_2026

    holidays_2026 = {date(2026, m, d) for m, d in NSE_HOLIDAYS_2026}

    days = []
    current = start
    while current <= end:
        if current.weekday() < 5 and current not in holidays_2026:
            days.append(current)
        current += timedelta(days=1)
    return days


def get_expiry_for_date(trading_date: date, underlying: str = "NIFTY") -> list[date]:
    """Find candidate expiry dates for the given trading date.

    Returns up to 3 Tuesday expiries to try (nearest + next 2 weeks),
    since the exact expiry depends on holidays and the caller should
    test which one has data.
    """
    # NIFTY weekly expiry is Tuesday
    d = trading_date
    while d.weekday() != 1:  # 1 = Tuesday
        d += timedelta(days=1)

    # Return this Tuesday + next 2 as candidates
    return [d, d + timedelta(weeks=1), d + timedelta(weeks=2)]


def get_spot_close(trading_date: date, spot_csv: str = "data/nifty_spot_minute.csv") -> float:
    """Get approximate spot close for a date from spot CSV."""
    csv_path = Path(spot_csv)
    if not csv_path.exists():
        return 0

    last_close = 0
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            dt = datetime.fromisoformat(row["date"])
            if dt.date() == trading_date:
                last_close = float(row["close"])
            elif dt.date() > trading_date and last_close > 0:
                break
    return last_close


def download_day(
    breeze: BreezeConnect,
    trading_date: date,
    underlying: str = "NIFTY",
    output_dir: Path = Path("data/breeze_chain"),
) -> int:
    """Download option chain data for a single trading day.

    Fetches 1-minute OHLCV + OI for all strikes around ATM.
    Saves in our chain snapshot CSV format.
    """
    output_file = output_dir / f"chain_{trading_date.isoformat()}.csv"
    if output_file.exists():
        row_count = sum(1 for _ in open(output_file)) - 1
        if row_count > 100:
            logger.info(f"  {trading_date}: already downloaded ({row_count} rows), skipping")
            return 0

    # Get spot close for ATM estimation
    spot = get_spot_close(trading_date)
    if spot <= 0:
        # Try to get from Breeze
        try:
            hist = breeze.get_historical_data_v2(
                interval="1minute",
                from_date=f"{trading_date}T09:15:00.000Z",
                to_date=f"{trading_date}T09:20:00.000Z",
                stock_code=underlying,
                exchange_code="NSE",
                product_type="cash",
            )
            if hist and hist.get("Success"):
                spot = float(hist["Success"][0]["close"])
        except Exception:
            pass

    if spot <= 0:
        logger.warning(f"  {trading_date}: no spot price available, skipping")
        return 0

    step = STRIKE_STEP.get(underlying, 50)
    atm = round(spot / step) * step
    candidate_expiries = get_expiry_for_date(trading_date, underlying)

    # Find the correct expiry by testing which one has data
    expiry = None
    for exp in candidate_expiries:
        test = breeze.get_historical_data_v2(
            interval="1minute",
            from_date=f"{trading_date}T09:15:00.000Z",
            to_date=f"{trading_date}T09:20:00.000Z",
            stock_code=underlying,
            exchange_code="NFO",
            product_type="options",
            expiry_date=f"{exp}T07:00:00.000Z",
            strike_price=str(int(atm)),
            right="call",
        )
        if test and test.get("Success"):
            expiry = exp
            break
        time.sleep(0.3)

    if not expiry:
        logger.warning(f"  {trading_date}: no valid expiry found, skipping")
        return 0

    strikes = [atm + i * step for i in range(-NUM_STRIKES_EACH_SIDE, NUM_STRIKES_EACH_SIDE + 1)]

    logger.info(f"  {trading_date}: spot={spot:.0f} ATM={atm} expiry={expiry} strikes={len(strikes)}")

    rows_written = 0
    with open(output_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "time", "underlying", "expiry", "strike", "option_type",
            "ltp", "iv", "delta", "gamma", "theta", "vega",
            "oi", "volume", "bid_price", "ask_price",
        ])

        for option_type, right_val in [("CE", "call"), ("PE", "put")]:
            for strike in strikes:
                try:
                    # Breeze API: fetch 1-minute candles for this strike
                    expiry_str = f"{expiry}T07:00:00.000Z"
                    hist = breeze.get_historical_data_v2(
                        interval="1minute",
                        from_date=f"{trading_date}T09:15:00.000Z",
                        to_date=f"{trading_date}T15:30:00.000Z",
                        stock_code=underlying,
                        exchange_code="NFO",
                        product_type="options",
                        expiry_date=expiry_str,
                        strike_price=str(int(strike)),
                        right=right_val,
                    )

                    success = hist.get("Success") if hist else None
                    if rows_written == 0:
                        logger.info(f"    DEBUG {strike}{option_type}: hist type={type(hist)}, Success type={type(success)}, len={len(success) if success else 'None'}")
                    if not success:
                        continue

                    for candle in success:
                        ts = candle.get("datetime", "")
                        close_price = float(candle.get("close", 0))
                        volume = int(candle.get("volume", 0))
                        oi = int(candle.get("open_interest", 0))

                        if close_price <= 0:
                            continue

                        # Breeze doesn't provide greeks — write zeros, compute later
                        writer.writerow([
                            ts, underlying, expiry.isoformat(), strike, option_type,
                            close_price,  # ltp = candle close
                            0.0,          # iv — compute later from BS
                            0.0, 0.0, 0.0, 0.0,  # delta, gamma, theta, vega
                            oi, volume,
                            0.0, 0.0,     # bid/ask not available
                        ])
                        rows_written += 1

                    # Rate limiting — Breeze allows ~5 requests/sec
                    time.sleep(0.25)

                except Exception as e:
                    logger.warning(f"    Error fetching {underlying} {strike}{option_type} {trading_date}: {e}")
                    time.sleep(1)  # Back off on error

    logger.info(f"  {trading_date}: wrote {rows_written} rows to {output_file.name}")
    return rows_written


def compute_greeks_for_file(filepath: Path, spot_csv: str = "data/nifty_spot_minute.csv"):
    """Post-process: compute IV and greeks from option prices using BS model."""
    from src.options.greeks import compute_greeks
    from src.options.iv import compute_iv

    RISK_FREE_RATE = 0.065  # 6.5% India 10Y

    rows = []
    with open(filepath) as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    if not rows:
        return

    # Load spot prices for IV computation
    spot_by_minute = {}
    csv_path = Path(spot_csv)
    if csv_path.exists():
        with open(csv_path) as f:
            for r in csv.DictReader(f):
                dt = datetime.fromisoformat(r["date"])
                key = dt.strftime("%Y-%m-%dT%H:%M")
                spot_by_minute[key] = float(r["close"])

    updated = 0
    for row in rows:
        try:
            ts = row["time"]
            minute_key = ts[:16]  # "YYYY-MM-DDTHH:MM"
            spot = spot_by_minute.get(minute_key, 0)
            if spot <= 0:
                continue

            ltp = float(row["ltp"])
            strike = float(row["strike"])
            expiry = date.fromisoformat(row["expiry"])
            opt_type = row["option_type"]

            # Time to expiry in years
            trade_date = datetime.fromisoformat(ts).date()
            dte = (expiry - trade_date).days
            T = max(dte / 365.0, 1 / 365.0)

            if ltp > 0 and spot > 0:
                iv = compute_iv(ltp, spot, strike, T, RISK_FREE_RATE, opt_type)
                if iv and iv > 0:
                    greeks = compute_greeks(spot, strike, T, RISK_FREE_RATE, iv, opt_type)
                    row["iv"] = f"{iv:.4f}"
                    row["delta"] = f"{greeks.delta:.4f}"
                    row["gamma"] = f"{greeks.gamma:.6f}"
                    row["theta"] = f"{greeks.theta:.4f}"
                    row["vega"] = f"{greeks.vega:.4f}"
                    updated += 1
        except Exception:
            continue

    # Write back
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    logger.info(f"  Computed greeks for {updated}/{len(rows)} rows in {filepath.name}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # Suppress noisy Breeze SDK logging
    logging.getLogger("breeze_connect").setLevel(logging.ERROR)
    load_env()

    parser = argparse.ArgumentParser(description="Download NIFTY option data from Breeze API")
    parser.add_argument("--days", type=int, default=30, help="Number of days to download")
    parser.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument("--underlying", default="NIFTY", help="Underlying (NIFTY/BANKNIFTY)")
    parser.add_argument("--no-greeks", action="store_true", help="Skip greeks computation")
    args = parser.parse_args()

    api_key = os.environ.get("BREEZE_API_KEY", "")
    api_secret = os.environ.get("BREEZE_API_SECRET", "")
    session_token = os.environ.get("BREEZE_SESSION_TOKEN", "")

    if not api_key or not api_secret:
        print("Missing BREEZE_API_KEY or BREEZE_API_SECRET in .env")
        print("Get them from: https://api.icicidirect.com/apiuser/home")
        sys.exit(1)

    # Connect to Breeze
    breeze = BreezeConnect(api_key=api_key)

    if session_token:
        breeze.generate_session(api_secret=api_secret, session_token=session_token)
    else:
        print("Missing BREEZE_SESSION_TOKEN in .env")
        print("Login at: https://api.icicidirect.com/apiuser/login")
        print("After login, you'll get a session token in the redirect URL")
        sys.exit(1)

    print(f"Connected to Breeze API")

    # Determine date range
    if args.start and args.end:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
    else:
        end = date.today()
        start = end - timedelta(days=args.days)

    trading_days = get_trading_days(start, end)
    print(f"Downloading {args.underlying} options: {start} to {end} ({len(trading_days)} trading days)")

    output_dir = Path("data/breeze_chain")
    output_dir.mkdir(parents=True, exist_ok=True)

    total_rows = 0
    for i, day in enumerate(trading_days):
        print(f"\n[{i+1}/{len(trading_days)}] ", end="")
        rows = download_day(breeze, day, args.underlying, output_dir)
        total_rows += rows

        # Compute greeks after download
        if rows > 0 and not args.no_greeks:
            chain_file = output_dir / f"chain_{day.isoformat()}.csv"
            compute_greeks_for_file(chain_file)

    print(f"\n{'=' * 60}")
    print(f"  Download complete!")
    print(f"  Total rows: {total_rows:,}")
    print(f"  Trading days: {len(trading_days)}")
    print(f"  Output: {output_dir}/")
    print(f"  Format: Compatible with ReplayBacktestEngine")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
