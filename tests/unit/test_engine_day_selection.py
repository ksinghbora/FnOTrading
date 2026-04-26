"""Tests for ``_select_trading_days`` — the day-iteration entry point.

The Apr 25 2026 audit found that ``BacktestEngine.run`` accepted only
``start_date + num_days`` and silently expanded non-contiguous date
lists into contiguous slices. CPCV passes train indices like
``[0,1,5,6,7,...]`` (gaps for held-out test folds); without the explicit
``days`` parameter the engine ran ``[0,1,2,3,4,5,6,7,...]`` and leaked
test days into train.

This file locks in the contract:

1. Explicit ``days=[...]`` is honored exactly — gaps preserved.
2. Days not in ``available`` are dropped (returned in ``missing``).
3. Legacy ``num_days`` + ``start_date`` still works for callers that
   want "first N available".
4. Both modes never call ``available_days()`` more than once.
"""
from __future__ import annotations

from datetime import date

from src.backtest.engine import _select_trading_days


def _avail() -> list[date]:
    """A 10-day available calendar, simulating GDFL parquet coverage."""
    return [date(2024, 9, 2 + i) for i in range(10)]  # Sep 2..11


# ─── Explicit days path ────────────────────────────────────────────────


def test_explicit_days_contiguous_returns_same_list():
    avail = _avail()
    asked = [avail[2], avail[3], avail[4]]
    out, missing = _select_trading_days(avail, days=asked, start_date=None, num_days=0)
    assert out == asked
    assert missing == []


def test_explicit_days_non_contiguous_preserves_gaps():
    """The bug fix's reason for being. Train idx [0,1,5,6,7] must be
    iterated in that order with the gap [2,3,4] preserved — not
    silently expanded to [0..7].
    """
    avail = _avail()
    asked = [avail[0], avail[1], avail[5], avail[6], avail[7]]
    out, missing = _select_trading_days(avail, days=asked, start_date=None, num_days=99)
    assert out == asked  # gaps preserved
    assert missing == []
    # And it explicitly differs from a contiguous slice.
    contiguous = [avail[0], avail[1], avail[2], avail[3], avail[4]]
    assert out != contiguous


def test_explicit_days_missing_dropped_with_report():
    avail = _avail()
    not_in_avail = date(2024, 12, 25)
    asked = [avail[0], not_in_avail, avail[1]]
    out, missing = _select_trading_days(avail, days=asked, start_date=None, num_days=0)
    assert out == [avail[0], avail[1]]
    assert missing == [not_in_avail]


def test_explicit_days_overrides_start_date_and_num_days():
    """When ``days`` is provided ``start_date`` and ``num_days`` are ignored —
    the explicit list wins. Documented in BacktestEngine.run docstring.
    """
    avail = _avail()
    asked = [avail[7], avail[8]]
    out, missing = _select_trading_days(
        avail, days=asked, start_date=avail[0], num_days=999,
    )
    assert out == asked
    assert missing == []


def test_explicit_days_empty_list_returns_empty():
    out, missing = _select_trading_days(_avail(), days=[], start_date=None, num_days=5)
    assert out == []
    assert missing == []


# ─── Legacy num_days + start_date path ─────────────────────────────────


def test_legacy_num_days_first_n():
    avail = _avail()
    out, missing = _select_trading_days(avail, days=None, start_date=None, num_days=3)
    assert out == avail[:3]
    assert missing == []


def test_legacy_start_date_skips_earlier():
    avail = _avail()
    out, missing = _select_trading_days(
        avail, days=None, start_date=avail[5], num_days=10,
    )
    assert out == avail[5:]
    assert missing == []


def test_legacy_num_days_zero_returns_empty():
    out, missing = _select_trading_days(_avail(), days=None, start_date=None, num_days=0)
    assert out == []
    assert missing == []


def test_legacy_start_date_after_last_available_returns_empty():
    avail = _avail()
    far_future = date(2026, 1, 1)
    out, missing = _select_trading_days(
        avail, days=None, start_date=far_future, num_days=10,
    )
    assert out == []
    assert missing == []
