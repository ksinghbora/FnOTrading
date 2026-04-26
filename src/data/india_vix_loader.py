"""India VIX daily-close loader.

Roadmap F3 (Apr 23 dual expert review). The existing GDFL pipeline derives a
VIX *proxy* as `ATM_IV × 100` on the reconstruction expiry
(`src/backtest/gdfl_loader.py::_reconstruct_spot_and_vix`). That is the wrong
methodology — NSE publishes India VIX as a **variance-weighted integral over
OTM puts and calls at 30-DTE constant-maturity**, not a single ATM IV read.
On real NIFTY option chains the two numbers diverge by 2-4 vol points on
event days, which flips regime labels used for scoring and filtering.

This loader reads a CSV of real India VIX closes (either resampled from the
existing minute feed or fetched daily from NSE archives) and exposes a
`get_vix(date)` method. Callers (GDFL loader, replay engine, regime detector)
should prefer this when available and fall back to the ATM-IV proxy only
when the real value is missing for a given date.

File search order:
    1. ``data/india_vix_daily.csv``           (preferred — pre-resampled daily)
    2. ``data/india_vix_minute.csv``          (resampled on load)
    3. ``data/india_vix_historical.csv``     (alternative — daily fetch from NSE)
    4. first CSV provided via constructor path

CSV schema expected (minimum):
    * daily file: ``date, close``   (or ``Date, Close``)
    * minute file: ``date, close``  where ``date`` is an ISO-8601 timestamp;
      resample takes the *last close per calendar day*.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


DEFAULT_SEARCH_FILES: tuple[str, ...] = (
    "india_vix_daily.csv",
    "india_vix_minute.csv",
    "india_vix_historical.csv",
)


class IndiaVIXLoader:
    """Load daily India VIX closes from a local CSV snapshot.

    The loader is lazy: the CSV is read on first call to :meth:`get_vix` so
    that construction does not fail if the file is missing (callers decide
    whether to fall back to a proxy).

    Attributes:
        data_dir: Root directory searched for VIX CSV files.
        csv_path: The CSV that was actually loaded (set on first read).
        _series: ``pd.Series`` mapping ``date`` → close VIX, sorted ascending.
    """

    def __init__(
        self,
        data_dir: Path | str = "data",
        csv_path: Path | str | None = None,
    ) -> None:
        self.data_dir: Path = Path(data_dir)
        self._explicit_path: Path | None = Path(csv_path) if csv_path else None
        self.csv_path: Path | None = None
        self._series: pd.Series | None = None
        self._loaded: bool = False

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def get_vix(self, target_date: date) -> float | None:
        """Return the India VIX close for ``target_date`` or ``None``.

        ``None`` signals "no real data available" so callers can fall back
        to the ATM-IV proxy or skip the day. Weekend/holiday dates return
        ``None`` (no close published).
        """
        if not self._loaded:
            self._load()
        if self._series is None or self._series.empty:
            return None
        try:
            value = self._series.get(pd.Timestamp(target_date).normalize())
        except (TypeError, ValueError):
            return None
        if value is None or pd.isna(value):
            return None
        return float(value)

    def available_dates(self) -> list[date]:
        """Return the list of dates for which a real VIX close is loaded."""
        if not self._loaded:
            self._load()
        if self._series is None or self._series.empty:
            return []
        return [ts.date() for ts in self._series.index]

    # ------------------------------------------------------------------ #
    # Internal loading
    # ------------------------------------------------------------------ #
    def _resolve_path(self) -> Path | None:
        if self._explicit_path is not None:
            return self._explicit_path if self._explicit_path.exists() else None
        for name in DEFAULT_SEARCH_FILES:
            candidate = self.data_dir / name
            if candidate.exists():
                return candidate
        return None

    def _load(self) -> None:
        self._loaded = True
        path = self._resolve_path()
        if path is None:
            logger.warning(
                "[VIX_LOADER] No India VIX CSV found in %s (searched %s); "
                "callers will fall back to proxy.",
                self.data_dir,
                list(DEFAULT_SEARCH_FILES),
            )
            return
        self.csv_path = path
        try:
            df = pd.read_csv(path)
        except Exception as exc:  # pragma: no cover - IO failures are rare
            logger.error("[VIX_LOADER] Failed reading %s: %s", path, exc)
            return

        # Normalise columns lower-case so we accept both NSE's mixed case
        # (``Date,Close``) and our minute-CSV schema (``date,close``).
        df.columns = [c.strip().lower() for c in df.columns]
        if "date" not in df.columns:
            logger.error(
                "[VIX_LOADER] %s missing required 'date' column (have %s)",
                path, list(df.columns),
            )
            return

        close_col = None
        for candidate in ("close", "vix", "value", "india_vix"):
            if candidate in df.columns:
                close_col = candidate
                break
        if close_col is None:
            logger.error(
                "[VIX_LOADER] %s missing a close/value column (have %s)",
                path, list(df.columns),
            )
            return

        df["date"] = pd.to_datetime(df["date"], errors="coerce", utc=False)
        # Some rows may be NaT if source has headers in the body.
        df = df.dropna(subset=["date"])
        if df.empty:
            logger.warning("[VIX_LOADER] %s yielded no parseable rows", path)
            return

        # Drop timezone info to keep the index comparable with bare dates.
        if df["date"].dt.tz is not None:
            df["date"] = df["date"].dt.tz_convert(None)

        # If we're on a minute-level file, resample to last close per day.
        # Heuristic: more than one row per calendar day → minute data.
        calendar = df["date"].dt.normalize()
        if calendar.duplicated().any():
            logger.info(
                "[VIX_LOADER] %s looks like minute-level data — resampling to "
                "last close per day.", path.name,
            )
            df = (
                df.sort_values("date")
                  .groupby(calendar, as_index=True)[close_col]
                  .last()
            )
            series = df.astype("float64")
        else:
            series = (
                df.set_index(calendar)[close_col]
                  .sort_index()
                  .astype("float64")
            )

        series = series.dropna()
        # Keep a bare DatetimeIndex (no time component) so .get(date) works.
        series.index = pd.DatetimeIndex(series.index).normalize()
        series = series[~series.index.duplicated(keep="last")]
        self._series = series
        logger.info(
            "[VIX_LOADER] Loaded %d daily closes from %s (range %s → %s)",
            len(series), path.name,
            series.index.min().date(), series.index.max().date(),
        )


def resample_minute_to_daily(
    minute_csv: Path | str,
    out_csv: Path | str,
) -> int:
    """One-shot helper: resample ``india_vix_minute.csv`` to a daily file.

    Writes ``date, close`` with one row per trading day (last minute close).
    Returns the number of rows written. Safe to run repeatedly — output is
    idempotent.
    """
    minute_path = Path(minute_csv)
    out_path = Path(out_csv)
    df = pd.read_csv(minute_path)
    df.columns = [c.strip().lower() for c in df.columns]
    if "date" not in df.columns or "close" not in df.columns:
        raise ValueError(
            f"{minute_path} must have 'date' and 'close' columns "
            f"(got {list(df.columns)})"
        )
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date")
    calendar = df["date"].dt.normalize()
    daily = df.groupby(calendar)["close"].last()
    daily = daily.dropna()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    daily.to_csv(out_path, header=["close"], index_label="date")
    logger.info(
        "[VIX_LOADER] Wrote %d daily closes to %s", len(daily), out_path,
    )
    return len(daily)


__all__ = ["IndiaVIXLoader", "resample_minute_to_daily", "DEFAULT_SEARCH_FILES"]
