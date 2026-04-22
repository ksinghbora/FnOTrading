"""GDFL tick data loader — extracts daily option-chain snapshots from raw zips.

GDFL delivers per-contract tick CSVs packed as:
    outer.zip → Nifty(Options)/YYYY/MMM_YYYY/GFDLNFO_TICK_DDMMYYYY.zip
                → GFDLNFO_TICK_DDMMYYYY/<SYMBOL>.NFO.csv

Each per-contract CSV has columns:
    Ticker, Date, Time, LTP, BuyPrice, BuyQty, SellPrice, SellQty, LTQ, OpenInterest

This module:
  1. Parses contract metadata from filenames (NIFTY02MAR2623600CE.NFO.csv).
  2. Streams CSVs from the nested day-zip without full extraction.
  3. Aggregates ticks into per-minute bars (last LTP/bid/ask/OI, summed volume).
  4. Reconstructs spot via put-call parity and builds a VIX proxy (30d ATM IV).
  5. Writes a parquet per day with denormalized spot/vix on every row.

The output parquet feeds GDFLMarketSource for the backtest engine.
"""

import logging
import math
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytz

from src.core.constants import RISK_FREE_RATE
from src.options.iv import compute_iv

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# NIFTY02MAR2623600CE or BANKNIFTY27MAY2548500PE
SYMBOL_RE = re.compile(r"^(NIFTY|BANKNIFTY)(\d{2})([A-Z]{3})(\d{2})(\d+)(CE|PE)$")

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
MINUTES_PER_DAY = 375  # 9:15 to 15:30


@dataclass(frozen=True)
class ContractMeta:
    underlying: str
    expiry: date
    strike: float
    option_type: str  # "CE" | "PE"


def parse_symbol(filename: str) -> ContractMeta | None:
    """Parse a GDFL CSV filename into structured contract metadata.

    >>> parse_symbol("NIFTY02MAR2623600CE.NFO.csv")
    ContractMeta(underlying='NIFTY', expiry=date(2026,3,2), strike=23600.0, option_type='CE')
    """
    stem = filename
    for suffix in (".NFO.csv", ".csv"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    m = SYMBOL_RE.match(stem)
    if not m:
        return None
    ul, dd, mmm, yy, strike, ot = m.groups()
    try:
        expiry = date(2000 + int(yy), MONTH_MAP[mmm], int(dd))
    except (KeyError, ValueError):
        return None
    return ContractMeta(
        underlying=ul,
        expiry=expiry,
        strike=float(strike),
        option_type=ot,
    )


def _resample_contract_to_minutes(df: pd.DataFrame, trade_date: date) -> pd.DataFrame:
    """Collapse tick rows to one row per minute (09:15–15:30)."""
    # Parse timestamp
    ts = pd.to_datetime(
        df["Date"] + " " + df["Time"],
        format="%d/%m/%Y %H:%M:%S",
        errors="coerce",
    )
    df = df.assign(_ts=ts).dropna(subset=["_ts"])
    df = df[df["_ts"].dt.date == trade_date]
    if df.empty:
        return df.iloc[0:0]

    df = df.set_index("_ts")
    # Last tick within each minute bucket (bid, ask, ltp, oi)
    agg_last = df[["LTP", "BuyPrice", "SellPrice", "OpenInterest"]].resample("1min").last()
    # Summed within-minute volume (LTQ is traded quantity per tick)
    agg_sum = df[["LTQ"]].resample("1min").sum()
    out = pd.concat([agg_last, agg_sum], axis=1)

    # Restrict to market hours and forward-fill gaps within the day
    start_ts = datetime.combine(trade_date, MARKET_OPEN)
    end_ts = datetime.combine(trade_date, MARKET_CLOSE)
    full_idx = pd.date_range(start_ts, end_ts, freq="1min", inclusive="left")
    out = out.reindex(full_idx)
    out[["LTP", "BuyPrice", "SellPrice", "OpenInterest"]] = (
        out[["LTP", "BuyPrice", "SellPrice", "OpenInterest"]].ffill()
    )
    out["LTQ"] = out["LTQ"].fillna(0)
    out.index.name = "time"
    return out.reset_index()


def _load_day_contracts(
    day_zip_path: Path,
    underlying: str,
    trade_date: date,
    max_expiries: int = 2,
) -> dict[ContractMeta, pd.DataFrame]:
    """Read every matching contract CSV from the daily zip, resampled to 1-min.

    The day zip is actually *doubly* nested: the outer zip is the 18-month archive,
    but `day_zip_path` points at the per-day inner zip extracted from it.
    """
    contracts: dict[ContractMeta, pd.DataFrame] = {}

    with zipfile.ZipFile(day_zip_path) as zf:
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        metas: list[tuple[str, ContractMeta]] = []
        for n in names:
            fname = n.rsplit("/", 1)[-1]
            meta = parse_symbol(fname)
            if meta is None or meta.underlying != underlying:
                continue
            if meta.expiry < trade_date:
                continue  # Already-expired contracts shouldn't appear but be safe
            metas.append((n, meta))

        if not metas:
            return contracts

        # Keep only nearest N expiries
        expiries = sorted({m.expiry for _, m in metas})
        keep_expiries = set(expiries[:max_expiries])
        metas = [(n, m) for n, m in metas if m.expiry in keep_expiries]

        logger.info(
            f"[GDFL] {trade_date} {underlying}: loading {len(metas)} contracts "
            f"across expiries {sorted(keep_expiries)}"
        )

        for n, meta in metas:
            with zf.open(n) as f:
                try:
                    df = pd.read_csv(f, usecols=[
                        "Date", "Time", "LTP", "BuyPrice", "SellPrice", "LTQ", "OpenInterest",
                    ])
                except Exception as e:
                    logger.debug(f"[GDFL] Skipping {n}: {e}")
                    continue
            minute_df = _resample_contract_to_minutes(df, trade_date)
            if not minute_df.empty:
                contracts[meta] = minute_df

    return contracts


def _build_strike_frames(
    contracts: dict[ContractMeta, pd.DataFrame],
    target_expiry: date,
) -> dict[float, pd.DataFrame]:
    """Combine CE and PE contracts of a single expiry into per-strike frames."""
    strike_frames: dict[float, pd.DataFrame] = {}
    for meta, df in contracts.items():
        if meta.expiry != target_expiry:
            continue
        sub = df.set_index("time")[["LTP", "BuyPrice", "SellPrice"]]
        sub = sub.rename(columns={
            "LTP": f"{meta.option_type}_ltp",
            "BuyPrice": f"{meta.option_type}_bid",
            "SellPrice": f"{meta.option_type}_ask",
        })
        if meta.strike in strike_frames:
            strike_frames[meta.strike] = strike_frames[meta.strike].join(sub, how="outer")
        else:
            strike_frames[meta.strike] = sub
    return strike_frames


def _pick_reconstruction_expiry(expiries: list[date], trade_date: date) -> date:
    """Pick the expiry to use for spot/VIX reconstruction.

    Skip expiries with ≤3 calendar days to expiry (T~0 makes IV and parity
    unstable as time value collapses). Fall back to nearest if no far expiry.
    """
    for exp in expiries:
        if (exp - trade_date).days >= 4:
            return exp
    return expiries[0]  # fallback: only a very-near expiry available


def _reconstruct_spot_and_vix(
    contracts: dict[ContractMeta, pd.DataFrame],
    trade_date: date,
) -> pd.DataFrame:
    """Derive per-minute spot and VIX proxy from the option chain.

    Uses a "reconstruction expiry" that is at least 4 DTE; T~0 expiry days
    produce numerically unstable IV and parity (time value collapses and
    microstructure noise dominates).

    Spot: put-call parity median over top-5 nearest-to-ATM strikes:
        S = K + (C_mid - P_mid) * exp(r * T)

    VIX proxy: ATM IV × 100, clamped to [5, 80] to reject numerical outliers.
    """
    if not contracts:
        return pd.DataFrame()

    expiries = sorted({m.expiry for m in contracts})
    recon_expiry = _pick_reconstruction_expiry(expiries, trade_date)

    strike_frames = _build_strike_frames(contracts, recon_expiry)
    if not strike_frames:
        raise RuntimeError(f"No strikes on {trade_date} for expiry {recon_expiry}")

    minute_idx = sorted({ts for df in strike_frames.values() for ts in df.index})
    minute_idx = pd.DatetimeIndex(minute_idx)

    ce_mid = pd.DataFrame(index=minute_idx)
    pe_mid = pd.DataFrame(index=minute_idx)
    for k, sf in strike_frames.items():
        if "CE_bid" in sf and "CE_ask" in sf:
            ce_mid[k] = ((sf["CE_bid"] + sf["CE_ask"]) / 2).reindex(minute_idx)
        if "PE_bid" in sf and "PE_ask" in sf:
            pe_mid[k] = ((sf["PE_bid"] + sf["PE_ask"]) / 2).reindex(minute_idx)

    strikes = sorted(set(ce_mid.columns) & set(pe_mid.columns))
    if not strikes:
        raise RuntimeError(f"No strikes with both CE and PE on {trade_date}")
    ce = ce_mid[strikes].values  # (T, K)
    pe = pe_mid[strikes].values
    diff = ce - pe
    K_arr = np.array(strikes, dtype=float)

    # Select top-5 strikes per minute whose |C-P| is smallest (closest to ATM)
    abs_diff = np.abs(diff)
    abs_diff = np.where(np.isnan(abs_diff), np.inf, abs_diff)
    k_top = min(5, abs_diff.shape[1])
    top_idx = np.argpartition(abs_diff, k_top - 1, axis=1)[:, :k_top]

    # Days to expiry (decays intraday)
    dte_days = (recon_expiry - trade_date).days
    T_vec = np.array([
        max(1 / (365 * 24), dte_days / 365
            - (ts.hour * 60 + ts.minute - 9 * 60 - 15) / (MINUTES_PER_DAY * 365))
        for ts in minute_idx
    ])

    disc = np.exp(RISK_FREE_RATE * T_vec)[:, None]
    S_per_strike = K_arr[None, :] + diff * disc

    # Median of top-5 per minute (robust to single-strike outliers)
    row_idx = np.arange(S_per_strike.shape[0])[:, None]
    spot_estimates = S_per_strike[row_idx, top_idx]
    spot = np.nanmedian(spot_estimates, axis=1)

    # Reject minutes where spot jumped >2% from rolling median (stale ticks)
    spot_series = pd.Series(spot, index=minute_idx)
    rolling_med = spot_series.rolling(window=15, min_periods=3, center=True).median()
    bad = (spot_series - rolling_med).abs() / rolling_med > 0.02
    spot_series[bad] = np.nan
    spot_series = spot_series.ffill().bfill()
    spot = spot_series.values

    # VIX proxy: ATM IV on reconstruction expiry
    vix_proxy = np.full_like(spot, np.nan)
    for i in range(len(minute_idx)):
        s = spot[i]
        if not np.isfinite(s) or s <= 0:
            continue
        atm_k_idx = int(np.argmin(np.abs(K_arr - s)))
        atm_k = K_arr[atm_k_idx]
        c = ce[i, atm_k_idx]
        p = pe[i, atm_k_idx]
        if not (np.isfinite(c) and np.isfinite(p) and c > 0 and p > 0):
            continue
        iv_ce = compute_iv(float(c), float(s), float(atm_k), float(T_vec[i]),
                           RISK_FREE_RATE, "CE")
        iv_pe = compute_iv(float(p), float(s), float(atm_k), float(T_vec[i]),
                           RISK_FREE_RATE, "PE")
        ivs = [v for v in (iv_ce, iv_pe) if v is not None and 0.03 < v < 1.0]
        if ivs:
            vix_proxy[i] = float(np.mean(ivs)) * 100.0

    # Clamp to realistic India VIX range [5, 80], forward-fill gaps
    vix_series = pd.Series(vix_proxy, index=minute_idx)
    vix_series = vix_series.where((vix_series >= 5) & (vix_series <= 80))
    vix_series = vix_series.ffill().bfill()

    logger.info(
        f"[GDFL] {trade_date}: recon_expiry={recon_expiry} (DTE={dte_days}), "
        f"spot_range=[{np.nanmin(spot):.0f},{np.nanmax(spot):.0f}], "
        f"vix_range=[{vix_series.min():.1f},{vix_series.max():.1f}]"
    )

    return pd.DataFrame({
        "time": minute_idx,
        "spot": spot,
        "vix": vix_series.values,
    })


def build_minute_snapshots(
    day_zip_path: Path,
    underlying: str,
    trade_date: date,
    max_strikes_per_side: int = 20,
    max_expiries: int = 2,
) -> pd.DataFrame:
    """Build a long-format per-minute snapshot DataFrame for a single trading day.

    Output columns:
        time, expiry, strike, option_type, ltp, bid, ask, oi, volume, spot, vix
    """
    contracts = _load_day_contracts(day_zip_path, underlying, trade_date, max_expiries)
    if not contracts:
        return pd.DataFrame()

    spot_df = _reconstruct_spot_and_vix(contracts, trade_date)
    if spot_df.empty:
        return pd.DataFrame()

    spot_df = spot_df.set_index("time")

    # Filter strikes to ATM ± N using median spot for the day
    day_spot_median = float(np.nanmedian(spot_df["spot"].values))
    step = 50.0 if underlying == "NIFTY" else 100.0
    day_atm = round(day_spot_median / step) * step
    keep_min = day_atm - max_strikes_per_side * step
    keep_max = day_atm + max_strikes_per_side * step

    logger.info(
        f"[GDFL] {trade_date} {underlying}: spot_median={day_spot_median:.0f} "
        f"atm={day_atm:.0f} strike_range=[{keep_min:.0f}, {keep_max:.0f}]"
    )

    # Build long-format rows
    rows = []
    for meta, df in contracts.items():
        if not (keep_min <= meta.strike <= keep_max):
            continue
        sub = df.copy()
        sub["expiry"] = meta.expiry
        sub["strike"] = meta.strike
        sub["option_type"] = meta.option_type
        sub = sub.rename(columns={
            "LTP": "ltp",
            "BuyPrice": "bid",
            "SellPrice": "ask",
            "OpenInterest": "oi",
            "LTQ": "volume",
        })
        rows.append(sub[[
            "time", "expiry", "strike", "option_type",
            "ltp", "bid", "ask", "oi", "volume",
        ]])

    if not rows:
        return pd.DataFrame()

    long_df = pd.concat(rows, ignore_index=True)

    # Join spot/vix per minute
    long_df = long_df.merge(
        spot_df.reset_index(),
        on="time",
        how="left",
    )

    # Localize to IST
    long_df["time"] = long_df["time"].dt.tz_localize(IST)

    # Clean types
    long_df["oi"] = long_df["oi"].fillna(0).astype("int64")
    long_df["volume"] = long_df["volume"].fillna(0).astype("int64")
    for col in ("ltp", "bid", "ask", "spot", "vix"):
        long_df[col] = long_df[col].astype("float64")

    return long_df


def write_parquet(df: pd.DataFrame, out_path: Path) -> None:
    """Write snapshot DataFrame to parquet with compression."""
    if df.empty:
        logger.warning(f"[GDFL] Empty snapshot, skipping write to {out_path}")
        return
    out_path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, out_path, compression="zstd", compression_level=3)
    size_mb = out_path.stat().st_size / 1024 / 1024
    logger.info(f"[GDFL] Wrote {out_path} ({len(df):,} rows, {size_mb:.1f} MB)")


def extract_and_write_day(
    outer_zip_path: Path,
    underlying: str,
    trade_date: date,
    out_dir: Path,
    max_strikes_per_side: int = 20,
    max_expiries: int = 2,
) -> Path | None:
    """End-to-end: pull one day from the master archive and write its parquet.

    Streams the inner day-zip from the outer archive via a temp file,
    processes it, then deletes the temp. Keeps transient disk small.
    """
    import tempfile

    year = trade_date.year
    mon_upper = trade_date.strftime("%b").upper()
    ul_folder = "Nifty(Options)" if underlying == "NIFTY" else "Banknifty(Options)"
    inner_name = (
        f"{ul_folder}/{year}/{mon_upper}_{year}/"
        f"GFDLNFO_TICK_{trade_date.strftime('%d%m%Y')}.zip"
    )

    with zipfile.ZipFile(outer_zip_path) as outer:
        if inner_name not in outer.namelist():
            logger.warning(f"[GDFL] Missing {inner_name} in archive")
            return None
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            with outer.open(inner_name) as f:
                tmp.write(f.read())

    try:
        df = build_minute_snapshots(
            tmp_path, underlying, trade_date,
            max_strikes_per_side=max_strikes_per_side,
            max_expiries=max_expiries,
        )
        if df.empty:
            logger.warning(f"[GDFL] No data for {trade_date} {underlying}")
            return None
        out_file = out_dir / f"gdfl_{underlying.lower()}_{trade_date.isoformat()}.parquet"
        write_parquet(df, out_file)
        return out_file
    finally:
        tmp_path.unlink(missing_ok=True)
