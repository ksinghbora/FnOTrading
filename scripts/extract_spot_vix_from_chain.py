"""Reconstruct spot + VIX time-series from recorded option-chain CSVs.

Why:
  The recorded chain CSVs (data/chain_snapshots/chain_YYYY-MM-DD.csv) extend
  past the end of our minute-level spot/VIX feeds. To replay the full window
  we need a spot value and a VIX value for every minute the chain has data.

Spot reconstruction:
  Put-call parity at the ATM strike:
      S ≈ K + (C - P) + K * (1 - exp(-r*T))
  For weekly NIFTY (T < 7d, r=6%), the discount term is < 0.12% — we ignore
  it and use S ≈ K + C_ltp - P_ltp at the ATM strike (where ATM is the strike
  with the smallest |C - P|).

VIX reconstruction (Apr 17 audit fix):
  Real India VIX (data/india_vix_minute.csv) is the source of truth and is
  ALWAYS preferred when its timestamp covers the chain minute.

  Real India VIX is the model-free volatility index NSE publishes — computed
  by integrating implied variance across many strikes, not just the ATM.
  Cross-checking the Mar 25 09:15 chain bar:
    Real India VIX:   24.74
    ATM-IV proxy:     41.44   (+68 % bias on weekly options near expiry)
  ATM-IV is biased high on weeklies because the ATM straddle picks up the
  expiry-day gamma premium that VIX averages away across far-OTM strikes.

  When real VIX is missing for a chain minute, we fall back to the ATM-IV
  proxy and tag the row in the new `vix_source` column ("real" or "proxy").
  Downstream consumers (replay engine, dashboards) can decide whether to
  trust proxy minutes or skip them.

  We do NOT carry-forward `last_vix` across day boundaries (stale weekend
  values used to leak into Monday's open). Within a day, carry-forward is
  capped at MAX_CARRY_MINUTES so a long quote gap doesn't pin VIX to a
  pre-event reading.

Quarantine-safety:
  The script only globs `CHAIN_DIR/chain_*.csv` (non-recursive), so the
  `_quarantine/` subdirectory containing weekend simulator data is
  automatically excluded. Do not change that to `rglob` or `**/`.

Output:
  data/nifty_spot_minute_chain.csv (same schema as nifty_spot_minute.csv)
  data/india_vix_minute_chain.csv  (schema + new `source` column: real|proxy)

Usage:
    uv run python scripts/extract_spot_vix_from_chain.py
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

CHAIN_DIR = Path("data/chain_snapshots")
EXISTING_VIX = Path("data/india_vix_minute.csv")

# India VIX is the only volatility index NSE publishes for index options —
# both NIFTY and BANKNIFTY use it. So the VIX file is shared across runs;
# only the spot output changes per underlying.
DEFAULT_VIX_OUT = Path("data/india_vix_minute_chain.csv")

# Spot output is per underlying.
SPOT_OUT_BY_UNDERLYING = {
    "NIFTY": Path("data/nifty_spot_minute_chain.csv"),
    "BANKNIFTY": Path("data/banknifty_spot_minute_chain.csv"),
}

# Carry-forward cap: if a minute has no priceable ATM, we may reuse the
# previous minute's spot/VIX — but only for short gaps. Beyond this, the
# minute is dropped (better a hole than a stale value).
MAX_CARRY_MINUTES = 5


def load_real_vix(path: Path) -> dict[str, float]:
    """Load real India VIX into a {minute_iso: vix_close} map.

    minute_iso is the timestamp truncated to minute (YYYY-MM-DDTHH:MM)
    so that lookup against chain minute keys is direct.
    """
    if not path.exists():
        return {}
    out: dict[str, float] = {}
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = row.get("date", "")
            close = row.get("close", "")
            if not ts or not close:
                continue
            try:
                v = float(close)
            except ValueError:
                continue
            if v <= 0:
                continue
            out[ts[:16]] = v
    return out


def reconstruct_minute(rows_for_minute: list[dict]) -> tuple[float | None, float | None]:
    """Given all chain rows at one timestamp, return (spot, atm_iv_proxy).

    Pairs CE+PE per (strike, expiry), picks the nearest expiry, then ATM by
    smallest |C-P|, computes spot via put-call parity, and reads ATM IV.
    Returns (None, None) when no priceable pair exists.
    """
    pairs: dict[tuple[str, float], dict[str, dict]] = defaultdict(dict)
    for row in rows_for_minute:
        key = (row["expiry"], float(row["strike"]))
        pairs[key][row["option_type"]] = row

    complete = {
        k: v for k, v in pairs.items()
        if "CE" in v and "PE" in v
        and float(v["CE"]["ltp"]) > 0 and float(v["PE"]["ltp"]) > 0
    }
    if not complete:
        return None, None

    nearest_expiry = min(k[0] for k in complete)
    same_expiry = {k: v for k, v in complete.items() if k[0] == nearest_expiry}

    def diff(item):
        _, legs = item
        return abs(float(legs["CE"]["ltp"]) - float(legs["PE"]["ltp"]))

    (atm_expiry, atm_strike), atm_legs = min(same_expiry.items(), key=diff)

    c = float(atm_legs["CE"]["ltp"])
    p = float(atm_legs["PE"]["ltp"])
    spot = atm_strike + c - p

    iv_ce = float(atm_legs["CE"].get("iv", 0))
    iv_pe = float(atm_legs["PE"].get("iv", 0))
    ivs = [v for v in (iv_ce, iv_pe) if v > 0]
    vix_proxy = (sum(ivs) / len(ivs)) * 100 if ivs else None

    return spot, vix_proxy


def process_chain_file(
    filepath: Path,
    real_vix: dict[str, float],
    target_underlying: str,
) -> tuple[list[dict], list[dict], dict[str, int]]:
    """Process one chain_YYYY-MM-DD.csv → (spot_rows, vix_rows, stats)."""
    by_minute: dict[str, list[dict]] = defaultdict(list)
    with open(filepath) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("underlying") != target_underlying:
                continue
            minute_iso = row["time"][:16]
            by_minute[minute_iso].append(row)

    spot_rows: list[dict] = []
    vix_rows: list[dict] = []
    stats = {"real": 0, "proxy": 0, "carry": 0, "dropped": 0}

    last_spot: float | None = None
    last_proxy: float | None = None
    last_minute_idx: int | None = None  # ordinal of last successful minute

    sorted_minutes = sorted(by_minute.keys())
    for idx, minute in enumerate(sorted_minutes):
        spot, proxy = reconstruct_minute(by_minute[minute])

        # Spot fallback (carry-forward, capped)
        if spot is None:
            if (
                last_spot is not None
                and last_minute_idx is not None
                and (idx - last_minute_idx) <= MAX_CARRY_MINUTES
            ):
                spot = last_spot
                stats["carry"] += 1
            else:
                stats["dropped"] += 1
                continue

        # Build canonical timestamp
        sample_ts = by_minute[minute][0]["time"]
        date_part, rest = sample_ts.split("T")
        hh, mm = rest[:2], rest[3:5]
        tz_idx = max(rest.rfind("+"), rest.rfind("-"))
        tz = rest[tz_idx:] if tz_idx > 0 else "+05:30"
        ts = f"{date_part}T{hh}:{mm}:00{tz}"

        spot_r = round(spot, 2)
        spot_rows.append({
            "date": ts,
            "open": spot_r, "high": spot_r, "low": spot_r, "close": spot_r,
            "volume": 0, "oi": 0,
        })

        # VIX: real first, then proxy, then carry — each tagged in `source`.
        real = real_vix.get(minute)
        if real is not None:
            vix_val, source = real, "real"
            stats["real"] += 1
        elif proxy is not None:
            vix_val, source = proxy, "proxy"
            stats["proxy"] += 1
            last_proxy = proxy
        elif (
            last_proxy is not None
            and last_minute_idx is not None
            and (idx - last_minute_idx) <= MAX_CARRY_MINUTES
        ):
            vix_val, source = last_proxy, "proxy_carry"
            stats["carry"] += 1
        else:
            vix_val, source = None, None

        if vix_val is not None:
            v_r = round(vix_val, 2)
            vix_rows.append({
                "date": ts,
                "open": v_r, "high": v_r, "low": v_r, "close": v_r,
                "volume": 0, "oi": 0,
                "source": source,
            })

        last_spot = spot
        last_minute_idx = idx

    return spot_rows, vix_rows, stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--underlying", default="NIFTY",
        choices=sorted(SPOT_OUT_BY_UNDERLYING.keys()),
        help="Which underlying's spot to extract (VIX is always India VIX, shared)",
    )
    parser.add_argument(
        "--vix-out", default=str(DEFAULT_VIX_OUT),
        help=(
            "Override VIX output path. WARNING: the default path is shared "
            "between underlyings, so a BANKNIFTY run will overwrite the "
            "NIFTY-derived VIX file. Pass --vix-out=/dev/null when running "
            "BANKNIFTY-only after a NIFTY run, OR re-run NIFTY afterwards."
        ),
    )
    args = parser.parse_args()

    target_underlying = args.underlying
    out_spot = SPOT_OUT_BY_UNDERLYING[target_underlying]
    out_vix = Path(args.vix_out)

    real_vix = load_real_vix(EXISTING_VIX)
    print(f"Loaded {len(real_vix)} real India VIX minute bars from {EXISTING_VIX}")
    print(f"Target underlying: {target_underlying}")

    spot_rows: list[dict] = []
    vix_rows: list[dict] = []
    totals = {"real": 0, "proxy": 0, "carry": 0, "dropped": 0}

    chain_files = sorted(CHAIN_DIR.glob("chain_*.csv"))
    print(f"Processing {len(chain_files)} chain files from {CHAIN_DIR} "
          f"(quarantine subdir excluded by non-recursive glob)")

    for filepath in chain_files:
        day = filepath.stem.replace("chain_", "")
        s, v, stats = process_chain_file(filepath, real_vix, target_underlying)
        spot_rows.extend(s)
        vix_rows.extend(v)
        for k in totals:
            totals[k] += stats[k]
        print(
            f"  {day}: {len(s):>4} spot, {len(v):>4} vix "
            f"[real={stats['real']} proxy={stats['proxy']} "
            f"carry={stats['carry']} dropped={stats['dropped']}]"
        )

    with open(out_spot, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "volume", "oi"])
        w.writeheader()
        w.writerows(spot_rows)

    with open(out_vix, "w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["date", "open", "high", "low", "close", "volume", "oi", "source"],
        )
        w.writeheader()
        w.writerows(vix_rows)

    print(f"\nWrote {len(spot_rows)} spot rows → {out_spot}")
    print(f"Wrote {len(vix_rows)} vix rows  → {out_vix}")
    print(
        f"VIX source mix: real={totals['real']} proxy={totals['proxy']} "
        f"carry={totals['carry']} dropped_minutes={totals['dropped']}"
    )
    if totals["proxy"] > 0:
        print(
            "  ! Proxy rows are ATM-IV * 100 — biased ~50-70% high on weekly options.\n"
            "    Replay engine should consume only `source==real` rows for VIX-gated logic."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
