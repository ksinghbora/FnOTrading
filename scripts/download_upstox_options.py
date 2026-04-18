#!/usr/bin/env python3
"""Download historical NIFTY option chain data from Upstox Expired Instruments API.

Uses the Upstox Expired Historical Candle Data API to fetch per-minute OHLCV + OI
for expired NIFTY weekly options. Saves in our chain snapshot CSV format for use
with ReplayBacktestEngine — same format as data/chain_snapshots/.

Requirements:
    - Upstox account with Plus subscription (for expired instruments API access)
    - pip install upstox-python-sdk

Setup:
    1. Open Upstox account: https://upstox.com/open-account/
    2. Subscribe to Upstox Plus
    3. Create API app: https://developer.upstox.com/
    4. Add to .env:
         UPSTOX_API_KEY=your_api_key
         UPSTOX_API_SECRET=your_api_secret
         UPSTOX_REDIRECT_URI=your_redirect_uri
         UPSTOX_ACCESS_TOKEN=your_access_token  # refreshed daily

Usage:
    # Download Oct 2025 - Mar 2026 (max 6-month lookback)
    uv run python scripts/download_upstox_options.py --start 2025-10-01 --end 2026-03-24

    # Download specific range
    uv run python scripts/download_upstox_options.py --start 2025-11-01 --end 2026-02-28

    # Get auth URL to obtain access token
    uv run python scripts/download_upstox_options.py --auth

    # Check available expiries
    uv run python scripts/download_upstox_options.py --list-expiries

API Docs:
    https://upstox.com/developer/api-documentation/expired-instruments/
    https://upstox.com/developer/api-documentation/get-expired-historical-candle-data/

Note on lookback:
    Upstox retains 6 months of expired instrument data. Running this today (Apr 2026)
    gives access to ~Oct 2025 onwards. Run again each month to stay within window.
"""

import argparse
import csv
import io
import json
import logging
import math
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

UPSTOX_BASE_URL = "https://api.upstox.com/v2"
STRIKE_STEP = 50           # NIFTY strike step
NUM_STRIKES_EACH_SIDE = 15  # ATM ± 15 strikes = 30 strikes total
RATE_LIMIT_SLEEP = 0.3      # 300ms between requests (~3 req/sec)


# ─── .env loader ──────────────────────────────────────────────────────────────

def load_env() -> dict:
    env = {}
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if v.strip():
                    env[k.strip()] = v.strip()
                    os.environ[k.strip()] = v.strip()
    return env


def update_env(key: str, value: str):
    """Update a single key in .env file."""
    env_file = Path(__file__).parent.parent / ".env"
    content = env_file.read_text() if env_file.exists() else ""
    lines = content.splitlines()

    updated = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
            lines[i] = f"{key}={value}"
            updated = True
            break

    if not updated:
        lines.append(f"{key}={value}")

    env_file.write_text("\n".join(lines) + "\n")
    os.environ[key] = value


# ─── Auth helpers ─────────────────────────────────────────────────────────────

def get_auth_url(api_key: str, redirect_uri: str) -> str:
    params = urlencode({
        "response_type": "code",
        "client_id": api_key,
        "redirect_uri": redirect_uri,
    })
    return f"https://api.upstox.com/v2/login/authorization/dialog?{params}"


def exchange_code_for_token(api_key: str, api_secret: str, redirect_uri: str, code: str) -> str:
    """Exchange authorization code for access token."""
    resp = requests.post(
        f"{UPSTOX_BASE_URL}/login/authorization/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": api_key,
            "client_secret": api_secret,
            "redirect_uri": redirect_uri,
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ─── Upstox API wrappers ──────────────────────────────────────────────────────

class UpstoxClient:
    def __init__(self, access_token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        })

    def _get(self, endpoint: str, params: dict | None = None) -> dict:
        url = f"{UPSTOX_BASE_URL}{endpoint}"
        resp = self.session.get(url, params=params)
        if resp.status_code == 401:
            raise RuntimeError("Upstox token expired. Re-run with --auth to get a new token.")
        if resp.status_code == 403:
            raise RuntimeError(
                "Upstox Plus subscription required for expired instruments API.\n"
                "Subscribe at: https://upstox.com/pricing/"
            )
        resp.raise_for_status()
        return resp.json()

    def get_expiries(self, underlying: str = "NIFTY") -> list[date]:
        """Get all available expired expiry dates for NIFTY options."""
        data = self._get(
            f"/expired-instruments/option-contracts/expiries",
            params={"underlying_key": f"NSE_INDEX|{underlying} 50" if underlying == "NIFTY" else f"NSE_INDEX|{underlying}"},
        )
        expiries = []
        for exp_str in data.get("data", {}).get("expiries", []):
            try:
                expiries.append(date.fromisoformat(exp_str))
            except ValueError:
                pass
        return sorted(expiries)

    def get_expired_contracts(self, expiry: date, underlying: str = "NIFTY") -> list[dict]:
        """Get all expired option contracts for a given expiry date."""
        underlying_key = f"NSE_INDEX|{underlying} 50" if underlying == "NIFTY" else f"NSE_INDEX|{underlying}"
        data = self._get(
            f"/expired-instruments/option-contracts",
            params={
                "underlying_key": underlying_key,
                "expiry_date": expiry.isoformat(),
            },
        )
        return data.get("data", {}).get("instruments", [])

    def get_candles(
        self,
        instrument_key: str,
        from_date: date,
        to_date: date,
        interval: str = "1minute",
    ) -> list[list]:
        """Get historical candles for an expired instrument.

        Returns list of [timestamp, open, high, low, close, volume, oi]
        """
        endpoint = (
            f"/expired-instruments/historical-candle"
            f"/{instrument_key}/{interval}"
            f"/{to_date.isoformat()}/{from_date.isoformat()}"
        )
        data = self._get(endpoint)
        return data.get("data", {}).get("candles", [])


# ─── BSM Greeks (post-process since Upstox doesn't provide) ──────────────────

def compute_iv_and_greeks(
    option_price: float, spot: float, strike: float, dte_days: float,
    rate: float = 0.065, option_type: str = "CE",
) -> tuple[float, float, float, float, float]:
    """Compute IV, delta, gamma, theta, vega using Black-Scholes."""
    import math

    if option_price <= 0 or spot <= 0 or strike <= 0 or dte_days <= 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0

    T = max(dte_days / 365.0, 1 / (365 * 24 * 60))  # Avoid zero
    is_call = option_type.upper() == "CE"

    def bs_price(sigma):
        d1 = (math.log(spot / strike) + (rate + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        nd1 = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
        nd2 = 0.5 * (1 + math.erf(d2 / math.sqrt(2)))
        if is_call:
            return spot * nd1 - strike * math.exp(-rate * T) * nd2
        else:
            return strike * math.exp(-rate * T) * (1 - nd2) - spot * (1 - nd1)

    # Newton-Raphson IV solve
    sigma = 0.2  # initial guess
    for _ in range(100):
        price = bs_price(sigma)
        d1 = (math.log(spot / strike) + (rate + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        vega_raw = spot * math.sqrt(T) * math.exp(-0.5 * d1**2) / math.sqrt(2 * math.pi)
        if abs(vega_raw) < 1e-10:
            break
        sigma -= (price - option_price) / vega_raw
        sigma = max(0.001, min(sigma, 20.0))
        if abs(bs_price(sigma) - option_price) < 0.01:
            break

    iv = sigma

    # Greeks
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv**2) * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)
    nd1 = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    phi_d1 = math.exp(-0.5 * d1**2) / math.sqrt(2 * math.pi)

    if is_call:
        delta = nd1
    else:
        delta = nd1 - 1.0

    gamma = phi_d1 / (spot * iv * math.sqrt(T))
    theta = (
        -(spot * phi_d1 * iv) / (2 * math.sqrt(T))
        - rate * strike * math.exp(-rate * T) * (0.5 * (1 + math.erf(d2 / math.sqrt(2))) if is_call else 0.5 * (1 - math.erf(d2 / math.sqrt(2))))
    ) / 365
    vega = spot * math.sqrt(T) * phi_d1 / 100  # per 1% vol

    return round(iv, 4), round(delta, 4), round(gamma, 6), round(theta, 4), round(vega, 4)


# ─── Spot price loader (for ATM + Greeks) ─────────────────────────────────────

def load_spot_by_minute(spot_csv: str) -> dict[str, float]:
    """Load spot prices keyed by 'YYYY-MM-DDTHH:MM'."""
    result = {}
    p = Path(spot_csv)
    if not p.exists():
        return result
    with open(p) as f:
        for row in csv.DictReader(f):
            dt = row.get("date", "")[:16]  # "YYYY-MM-DDTHH:MM"
            close = row.get("close", 0)
            if dt and close:
                result[dt] = float(close)
    return result


# ─── Chain snapshot CSV format ────────────────────────────────────────────────

CSV_HEADER = [
    "time", "underlying", "expiry", "strike", "option_type",
    "ltp", "iv", "delta", "gamma", "theta", "vega",
    "oi", "volume", "bid", "ask",
]


def get_trading_days(start: date, end: date) -> list[date]:
    """Get weekdays excluding weekends (approximate — no holiday list needed here)."""
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


# ─── Main downloader ──────────────────────────────────────────────────────────

def download_expiry(
    client: UpstoxClient,
    expiry: date,
    trading_days: list[date],
    spot_by_minute: dict[str, float],
    output_dir: Path,
    underlying: str = "NIFTY",
) -> int:
    """Download all option chains for a given expiry, writing per-day CSV files.

    Returns total rows written.
    """
    # Find which trading days fall within this expiry's range
    # (from previous expiry + 1 day to this expiry date)
    days_for_expiry = [d for d in trading_days if d <= expiry]

    # Get all contracts for this expiry
    print(f"  Fetching contracts for expiry {expiry}...")
    contracts = client.get_expired_contracts(expiry, underlying)
    if not contracts:
        print(f"  No contracts found for {expiry}")
        return 0

    # Build strike → {CE: key, PE: key} map
    # Instrument key format from Upstox expired API
    strike_map: dict[float, dict[str, str]] = {}
    for c in contracts:
        strike = float(c.get("strike_price", 0))
        opt_type = c.get("option_type", "")  # "CE" or "PE"
        inst_key = c.get("instrument_key", "")
        if strike > 0 and opt_type and inst_key:
            if strike not in strike_map:
                strike_map[strike] = {}
            strike_map[strike][opt_type] = inst_key

    print(f"  Found {len(contracts)} contracts, {len(strike_map)} unique strikes for {expiry}")

    # For each trading day covered by this expiry, open a CSV file
    # Per-day data: {day_date: {(HH:MM, strike, opt_type): row}}
    day_writers: dict[date, list[list]] = {d: [] for d in days_for_expiry}

    total_rows = 0

    for strike, opt_keys in sorted(strike_map.items()):
        for opt_type, inst_key in opt_keys.items():
            if not days_for_expiry:
                continue

            start_dl = days_for_expiry[0]
            end_dl = days_for_expiry[-1]

            try:
                candles = client.get_candles(inst_key, start_dl, end_dl, "1minute")
                time.sleep(RATE_LIMIT_SLEEP)
            except Exception as e:
                logger.warning(f"    Error for {strike}{opt_type}: {e}")
                time.sleep(2)
                continue

            for candle in candles:
                if len(candle) < 6:
                    continue
                ts_str, open_, high, low, close, volume = candle[:6]
                oi = candle[6] if len(candle) > 6 else 0

                if close <= 0:
                    continue

                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    # Convert to IST
                    import pytz
                    ist = pytz.timezone("Asia/Kolkata")
                    ts_ist = ts.astimezone(ist)
                    day = ts_ist.date()
                    minute_key = ts_ist.strftime("%Y-%m-%dT%H:%M")
                    ts_iso = ts_ist.isoformat()
                except Exception:
                    continue

                if day not in day_writers:
                    continue

                # Market hours only (9:15 - 15:30 IST)
                t = ts_ist.time()
                from datetime import time as dtime
                if t < dtime(9, 15) or t > dtime(15, 30):
                    continue

                # Compute IV + Greeks
                spot = spot_by_minute.get(minute_key, 0.0)
                dte_days = max((expiry - day).days, 0)
                ltp = float(close)

                if spot > 0 and ltp > 0:
                    iv, delta, gamma, theta, vega = compute_iv_and_greeks(
                        ltp, spot, float(strike), dte_days, option_type=opt_type
                    )
                else:
                    iv = delta = gamma = theta = vega = 0.0

                row = [
                    ts_iso, underlying, expiry.isoformat(), strike, opt_type,
                    round(ltp, 2),
                    iv, delta, gamma, theta, vega,
                    int(oi), int(volume),
                    0.0, 0.0,  # bid/ask not provided by Upstox candles
                ]
                day_writers[day].append(row)
                total_rows += 1

    # Write per-day CSV files
    for day, rows in day_writers.items():
        if not rows:
            continue
        output_file = output_dir / f"chain_{day.isoformat()}.csv"
        file_exists = output_file.exists()

        # If file exists, append (for multiple expiries on same day)
        mode = "a" if file_exists else "w"
        with open(output_file, mode, newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(CSV_HEADER)
            writer.writerows(rows)

        print(f"    {day}: wrote {len(rows)} rows to {output_file.name}")

    return total_rows


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    load_env()
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Download expired NIFTY options data from Upstox")
    parser.add_argument("--start", type=str, help="Start date YYYY-MM-DD (max 6 months ago)")
    parser.add_argument("--end", type=str, help="End date YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--underlying", default="NIFTY", help="Underlying (default: NIFTY)")
    parser.add_argument("--output-dir", default="data/upstox_chain",
                        help="Output directory (default: data/upstox_chain)")
    parser.add_argument("--spot-csv", default="data/nifty_spot_minute.csv",
                        help="Spot price CSV for IV/greeks computation")
    parser.add_argument("--auth", action="store_true",
                        help="Generate auth URL and exchange code for access token")
    parser.add_argument("--auth-code", type=str,
                        help="Exchange this auth code for access token")
    parser.add_argument("--list-expiries", action="store_true",
                        help="List available expired expiry dates and exit")
    args = parser.parse_args()

    api_key = os.environ.get("UPSTOX_API_KEY", "")
    api_secret = os.environ.get("UPSTOX_API_SECRET", "")
    redirect_uri = os.environ.get("UPSTOX_REDIRECT_URI", "https://127.0.0.1")
    access_token = os.environ.get("UPSTOX_ACCESS_TOKEN", "")

    # ── Auth flow ────────────────────────────────────────────────────────────

    if args.auth:
        if not api_key:
            print("ERROR: Add UPSTOX_API_KEY to .env first")
            print("Get it from: https://developer.upstox.com/")
            sys.exit(1)
        url = get_auth_url(api_key, redirect_uri)
        print(f"\nOpen this URL in your browser to log in to Upstox:")
        print(f"\n  {url}\n")
        print("After login, you'll be redirected to your redirect URI with ?code=XXXXXX")
        print("Copy the 'code' value and run:")
        print(f"  uv run python scripts/download_upstox_options.py --auth-code YOUR_CODE")
        return

    if args.auth_code:
        if not api_key or not api_secret:
            print("ERROR: Add UPSTOX_API_KEY and UPSTOX_API_SECRET to .env first")
            sys.exit(1)
        token = exchange_code_for_token(api_key, api_secret, redirect_uri, args.auth_code)
        update_env("UPSTOX_ACCESS_TOKEN", token)
        print(f"Access token saved to .env: {token[:20]}...")
        return

    if not access_token:
        print("No UPSTOX_ACCESS_TOKEN in .env. Run with --auth first.")
        sys.exit(1)

    client = UpstoxClient(access_token)

    # ── List expiries ─────────────────────────────────────────────────────────

    if args.list_expiries:
        print(f"Fetching available expired NIFTY expiries...")
        expiries = client.get_expiries(args.underlying)
        print(f"Found {len(expiries)} expired expiry dates:")
        for exp in expiries:
            print(f"  {exp} ({exp.strftime('%A')})")
        return

    # ── Download ──────────────────────────────────────────────────────────────

    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    if args.start:
        start = date.fromisoformat(args.start)
    else:
        start = end - timedelta(days=180)

    # Warn if start is beyond 6-month lookback
    cutoff = date.today() - timedelta(days=185)
    if start < cutoff:
        print(f"WARNING: {start} may be beyond Upstox's 6-month lookback window.")
        print(f"         Earliest reliable start: ~{cutoff}")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nUpstox Options Downloader")
    print(f"  Underlying : {args.underlying}")
    print(f"  Period     : {start} to {end}")
    print(f"  Output     : {output_dir}/")
    print(f"  Format     : chain_YYYY-MM-DD.csv (ReplayBacktestEngine compatible)")
    print()

    # Load spot prices for greeks
    spot_by_minute = load_spot_by_minute(args.spot_csv)
    print(f"Loaded {len(spot_by_minute):,} spot price points for Greeks computation")

    # Get available expiries in range
    print(f"Fetching available expiries...")
    all_expiries = client.get_expiries(args.underlying)
    expiries_in_range = [e for e in all_expiries if start <= e <= end]
    print(f"Found {len(expiries_in_range)} expiries in range: "
          f"{expiries_in_range[0] if expiries_in_range else 'none'} → "
          f"{expiries_in_range[-1] if expiries_in_range else 'none'}")

    if not expiries_in_range:
        print("No expiries found in range. Check if date range is within 6-month lookback.")
        return

    # Get all trading days in range
    trading_days = get_trading_days(start, end)
    print(f"Trading days in range: {len(trading_days)}")
    print()

    # Download each expiry
    total_rows = 0
    for i, expiry in enumerate(expiries_in_range):
        print(f"[{i+1}/{len(expiries_in_range)}] Expiry {expiry}:")
        rows = download_expiry(client, expiry, trading_days, spot_by_minute, output_dir, args.underlying)
        total_rows += rows

    # Summary
    files = sorted(output_dir.glob("chain_*.csv"))
    print(f"\n{'='*60}")
    print(f"  Download complete!")
    print(f"  Total rows : {total_rows:,}")
    print(f"  Days saved : {len(files)}")
    print(f"  Date range : {files[0].stem if files else '-'} → {files[-1].stem if files else '-'}")
    print(f"  Output     : {output_dir}/")
    print(f"  Compatible : ReplayBacktestEngine (data_source=snapshot)")
    print(f"{'='*60}")
    print()
    print("Next: Run chain replay with this data:")
    print(f"  python /tmp/run_chain_replay.py  (update snapshot_dir to {output_dir}/)")


if __name__ == "__main__":
    main()
