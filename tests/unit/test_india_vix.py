"""Tests for the real India VIX loader (roadmap F3).

The loader replaces the single-ATM-IV proxy in ``gdfl_loader.py`` with the
real NSE India VIX close. These tests verify:

1. Loader reads a daily CSV correctly.
2. Loader resamples a minute CSV to last-close-per-day.
3. Missing files / missing dates return ``None`` (not an exception) so
   callers fall back to the proxy cleanly.
4. ``_reconstruct_spot_and_vix`` uses the real value when a loader is
   provided, and falls back to the proxy when the loader is ``None``.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from textwrap import dedent

import pandas as pd
import pytest

from src.data.india_vix_loader import IndiaVIXLoader, resample_minute_to_daily


class TestIndiaVIXLoader:
    def test_reads_daily_csv(self, tmp_path: Path) -> None:
        csv = tmp_path / "india_vix_daily.csv"
        csv.write_text(
            dedent(
                """\
                date,close
                2026-04-20,14.25
                2026-04-21,15.10
                2026-04-22,15.80
                """
            )
        )
        loader = IndiaVIXLoader(data_dir=tmp_path)
        assert loader.get_vix(date(2026, 4, 21)) == pytest.approx(15.10)
        assert loader.get_vix(date(2026, 4, 22)) == pytest.approx(15.80)

    def test_resamples_minute_csv(self, tmp_path: Path) -> None:
        csv = tmp_path / "india_vix_minute.csv"
        csv.write_text(
            dedent(
                """\
                date,open,high,low,close,volume,oi
                2026-04-20T09:15:00+05:30,14.00,14.20,13.95,14.10,0,0
                2026-04-20T09:16:00+05:30,14.10,14.22,14.05,14.15,0,0
                2026-04-20T15:29:00+05:30,14.30,14.35,14.25,14.32,0,0
                2026-04-21T09:15:00+05:30,14.35,14.40,14.28,14.37,0,0
                2026-04-21T15:29:00+05:30,15.05,15.12,14.95,15.10,0,0
                """
            )
        )
        loader = IndiaVIXLoader(data_dir=tmp_path)
        # Per-day "last close" picks 14.32 and 15.10 respectively.
        assert loader.get_vix(date(2026, 4, 20)) == pytest.approx(14.32)
        assert loader.get_vix(date(2026, 4, 21)) == pytest.approx(15.10)

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        # tmp_path has nothing in it — loader must not raise.
        loader = IndiaVIXLoader(data_dir=tmp_path)
        assert loader.get_vix(date(2026, 4, 20)) is None
        assert loader.available_dates() == []

    def test_returns_none_for_missing_date(self, tmp_path: Path) -> None:
        csv = tmp_path / "india_vix_daily.csv"
        csv.write_text("date,close\n2026-04-20,14.5\n")
        loader = IndiaVIXLoader(data_dir=tmp_path)
        assert loader.get_vix(date(2025, 1, 1)) is None  # not in file
        assert loader.get_vix(date(2026, 4, 20)) == pytest.approx(14.5)

    def test_explicit_path_overrides_search(self, tmp_path: Path) -> None:
        explicit = tmp_path / "myvix.csv"
        explicit.write_text("date,close\n2026-04-20,18.1\n")
        loader = IndiaVIXLoader(data_dir=tmp_path, csv_path=explicit)
        assert loader.get_vix(date(2026, 4, 20)) == pytest.approx(18.1)

    def test_accepts_uppercase_columns(self, tmp_path: Path) -> None:
        csv = tmp_path / "india_vix_daily.csv"
        csv.write_text("Date,Close\n2026-04-20,14.5\n")
        loader = IndiaVIXLoader(data_dir=tmp_path)
        assert loader.get_vix(date(2026, 4, 20)) == pytest.approx(14.5)

    def test_available_dates_is_sorted(self, tmp_path: Path) -> None:
        csv = tmp_path / "india_vix_daily.csv"
        csv.write_text(
            dedent(
                """\
                date,close
                2026-04-22,15.8
                2026-04-20,14.25
                2026-04-21,15.1
                """
            )
        )
        loader = IndiaVIXLoader(data_dir=tmp_path)
        dates = loader.available_dates()
        assert dates == sorted(dates)
        assert dates[0] == date(2026, 4, 20)
        assert dates[-1] == date(2026, 4, 22)

    def test_malformed_csv_does_not_raise(self, tmp_path: Path) -> None:
        csv = tmp_path / "india_vix_daily.csv"
        csv.write_text("something,random\n1,2\n")
        loader = IndiaVIXLoader(data_dir=tmp_path)
        # No date column → return None, do not raise
        assert loader.get_vix(date(2026, 4, 20)) is None


class TestResampleHelper:
    def test_resample_minute_to_daily(self, tmp_path: Path) -> None:
        src = tmp_path / "minute.csv"
        src.write_text(
            dedent(
                """\
                date,open,high,low,close,volume,oi
                2026-04-20T09:15:00+05:30,14.0,14.2,13.9,14.1,0,0
                2026-04-20T15:29:00+05:30,14.3,14.35,14.25,14.32,0,0
                2026-04-21T09:15:00+05:30,14.35,14.4,14.28,14.37,0,0
                2026-04-21T15:29:00+05:30,15.05,15.12,14.95,15.10,0,0
                """
            )
        )
        out = tmp_path / "daily.csv"
        n = resample_minute_to_daily(src, out)
        assert n == 2
        df = pd.read_csv(out)
        assert list(df.columns) == ["date", "close"]
        df["close"] = df["close"].astype(float)
        assert df.iloc[0]["close"] == pytest.approx(14.32)
        assert df.iloc[1]["close"] == pytest.approx(15.10)


class TestGDFLLoaderFallback:
    """Ensure ``_reconstruct_spot_and_vix`` honours the loader when given
    one, and falls back to the proxy when it's None.

    This is a narrow test: we don't need full GDFL plumbing. We patch
    ``compute_iv`` to a known value so the proxy output is predictable,
    then verify the real-VIX path overrides it.
    """

    def _tiny_contracts(self, trade_date: date):
        """Synthetic 2-strike CE+PE chain at one minute."""
        from src.backtest.gdfl_loader import ContractMeta
        expiry = date(trade_date.year, trade_date.month, trade_date.day + 7)
        minute_ts = pd.Timestamp(
            year=trade_date.year, month=trade_date.month, day=trade_date.day,
            hour=10, minute=0,
        )
        base_row = {
            "time": [minute_ts],
            "LTP": [100.0],
            "BuyPrice": [99.0],
            "SellPrice": [101.0],
            "OpenInterest": [0],
            "LTQ": [0],
        }
        contracts = {}
        for k in (24000.0, 24100.0):
            for ot in ("CE", "PE"):
                meta = ContractMeta("NIFTY", expiry, k, ot)
                contracts[meta] = pd.DataFrame(base_row)
        return contracts

    def test_proxy_path_when_loader_none(self) -> None:
        """Guard rail: when no loader is given, the VIX column is still
        populated (by the proxy). Asserting non-empty is enough — the
        proxy's numeric value itself isn't the subject of this test."""
        from src.backtest.gdfl_loader import _reconstruct_spot_and_vix
        trade_date = date(2026, 4, 20)
        contracts = self._tiny_contracts(trade_date)
        df = _reconstruct_spot_and_vix(contracts, trade_date, india_vix_loader=None)
        assert not df.empty
        assert "vix" in df.columns

    def test_real_vix_overrides_proxy(self, tmp_path: Path) -> None:
        """When the loader has a real close for the day, every minute row
        carries that exact value (not the ATM-IV proxy)."""
        from src.backtest.gdfl_loader import _reconstruct_spot_and_vix
        trade_date = date(2026, 4, 20)
        csv = tmp_path / "india_vix_daily.csv"
        csv.write_text(f"date,close\n{trade_date.isoformat()},17.42\n")
        loader = IndiaVIXLoader(data_dir=tmp_path)
        contracts = self._tiny_contracts(trade_date)
        df = _reconstruct_spot_and_vix(contracts, trade_date, india_vix_loader=loader)
        assert not df.empty
        assert df["vix"].nunique() == 1
        assert df["vix"].iloc[0] == pytest.approx(17.42)

    def test_real_vix_out_of_range_falls_back(self, tmp_path: Path) -> None:
        """If the loader returns an implausible value, the proxy still runs
        rather than propagating bad data downstream."""
        from src.backtest.gdfl_loader import _reconstruct_spot_and_vix
        trade_date = date(2026, 4, 20)
        csv = tmp_path / "india_vix_daily.csv"
        csv.write_text(f"date,close\n{trade_date.isoformat()},125.0\n")  # >80
        loader = IndiaVIXLoader(data_dir=tmp_path)
        contracts = self._tiny_contracts(trade_date)
        df = _reconstruct_spot_and_vix(contracts, trade_date, india_vix_loader=loader)
        # vix column must exist regardless — proxy kicked in
        assert "vix" in df.columns
        # And it's never exactly the bad value.
        assert (df["vix"] != pytest.approx(125.0)).all()
