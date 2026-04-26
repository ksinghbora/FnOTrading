"""Tests for src/backtest/validation/regime.py."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from src.backtest.validation.regime import (
    REGIMES,
    RegimeStats,
    bucket_row,
    load_event_dates,
    stratify,
)


# ─── Fixtures ─────────────────────────────────────────────────────────


def _make_row(
    d: date,
    *,
    vix: float,
    is_expiry: int = 0,
    dte: int = 5,
    move: float = 0.0,
    pnl: float = 100.0,
) -> dict:
    return {
        "date": d,
        "vix": vix,
        "is_expiry": is_expiry,
        "dte": dte,
        "move_from_open_pct": move,
        "outcome_pnl": pnl,
    }


# ─── bucket_row ───────────────────────────────────────────────────────


def test_bucket_row_high_vix_and_expiry():
    row = pd.Series(
        _make_row(date(2025, 1, 7), vix=16.0, is_expiry=1, dte=0, move=0.2)
    )
    labels = bucket_row(row, {})
    assert "high_vix" in labels
    assert "expiry_week" in labels
    # move=0.2 (abs 0.2 <= 0.5) → range_bound
    assert "range_bound" in labels
    assert "trending" not in labels


def test_bucket_row_low_vix_range_bound():
    row = pd.Series(_make_row(date(2025, 1, 6), vix=11.0, move=0.3))
    labels = bucket_row(row, {})
    assert "low_vix" in labels
    assert "range_bound" in labels
    assert "trending" not in labels
    assert "high_vix" not in labels
    assert "mid_vix" not in labels


def test_bucket_row_mid_vix_trending():
    row = pd.Series(_make_row(date(2025, 1, 6), vix=14.0, move=1.5))
    labels = bucket_row(row, {})
    assert "mid_vix" in labels
    assert "trending" in labels
    assert "range_bound" not in labels


def test_bucket_row_dte_two_counts_as_expiry_week():
    row = pd.Series(_make_row(date(2025, 1, 6), vix=13.0, is_expiry=0, dte=2))
    labels = bucket_row(row, {})
    assert "expiry_week" in labels


def test_bucket_row_event_day_detected():
    d = date(2025, 2, 8)
    row = pd.Series(_make_row(d, vix=14.0))
    labels = bucket_row(row, {d: "RBI_POLICY"})
    assert "event_day" in labels


def test_bucket_row_boundary_vix_13_is_mid():
    # vix == 13 → mid (13 <= v <= 15)
    row = pd.Series(_make_row(date(2025, 1, 6), vix=13.0))
    labels = bucket_row(row, {})
    assert "mid_vix" in labels
    assert "low_vix" not in labels
    assert "high_vix" not in labels


def test_bucket_row_boundary_move_at_0_5_is_range():
    row = pd.Series(_make_row(date(2025, 1, 6), vix=14.0, move=0.5))
    labels = bucket_row(row, {})
    assert "range_bound" in labels
    # 0.5 is not > 1.0 so not trending
    assert "trending" not in labels


def test_bucket_row_boundary_move_between_0_5_and_1_neither():
    row = pd.Series(_make_row(date(2025, 1, 6), vix=14.0, move=0.7))
    labels = bucket_row(row, {})
    assert "range_bound" not in labels
    assert "trending" not in labels


# ─── stratify ─────────────────────────────────────────────────────────


def test_stratify_empty_dataframe_all_regimes_zero():
    df = pd.DataFrame(columns=["date", "vix", "is_expiry", "dte", "move_from_open_pct", "outcome_pnl"])
    out = stratify(df, event_dates={})
    assert set(out.keys()) == set(REGIMES)
    for r in REGIMES:
        stats = out[r]
        assert isinstance(stats, RegimeStats)
        assert stats.num_trades == 0
        assert stats.total_pnl == 0.0
        assert stats.passed is True


def test_stratify_every_regime_appears_in_output():
    rows = []
    base = date(2025, 1, 6)
    # High VIX range
    for i in range(10):
        rows.append(_make_row(base + timedelta(days=i), vix=17.0, move=0.3, pnl=50.0))
    # Mid VIX
    for i in range(10):
        rows.append(_make_row(base + timedelta(days=i + 10), vix=14.0, move=0.3, pnl=50.0))
    # Low VIX
    for i in range(10):
        rows.append(_make_row(base + timedelta(days=i + 20), vix=11.0, move=0.3, pnl=50.0))
    # Expiry week
    for i in range(5):
        rows.append(_make_row(base + timedelta(days=i + 30), vix=14.0, is_expiry=1, dte=0, pnl=10.0))
    # Event day — pick a date far outside all other generated ranges to
    # avoid coincidental overlap with the block-based days above.
    event_d = date(2026, 2, 8)
    rows.append(_make_row(event_d, vix=14.0, pnl=10.0))
    # Trending
    for i in range(5):
        rows.append(_make_row(base + timedelta(days=i + 50), vix=14.0, move=1.5, pnl=30.0))

    df = pd.DataFrame(rows)
    out = stratify(df, event_dates={event_d: "RBI_POLICY"})
    for r in REGIMES:
        assert r in out

    assert out["high_vix"].num_trades == 10
    assert out["mid_vix"].num_trades >= 10
    assert out["low_vix"].num_trades == 10
    assert out["expiry_week"].num_trades == 5
    assert out["event_day"].num_trades == 1
    assert out["trending"].num_trades == 5
    assert out["range_bound"].num_trades >= 30  # 30 all-regimes + 1 event-day


def test_stratify_overlap_high_vix_and_expiry_week():
    """A row with vix=16 AND is_expiry should land in BOTH buckets."""
    rows = [
        _make_row(date(2025, 1, 7), vix=16.0, is_expiry=1, dte=0, pnl=100.0)
        for _ in range(3)
    ]
    df = pd.DataFrame(rows)
    out = stratify(df, event_dates={})
    assert out["high_vix"].num_trades == 3
    assert out["expiry_week"].num_trades == 3


def test_stratify_low_vix_and_range_bound_overlap():
    rows = [
        _make_row(date(2025, 1, 6), vix=11.0, move=0.3, pnl=100.0) for _ in range(4)
    ]
    df = pd.DataFrame(rows)
    out = stratify(df, event_dates={})
    assert out["low_vix"].num_trades == 4
    assert out["range_bound"].num_trades == 4


def test_stratify_fail_case_large_losing_bucket():
    """Regime with >20 trades and Sharpe < -0.5 should have passed=False."""
    rows = []
    base = date(2025, 1, 6)
    # 25 trading days with monotonically negative daily P&L — guarantees
    # std > 0, mean < 0 and |mean/std| large enough to drop Sharpe well
    # below -0.5 annualized.
    for i in range(25):
        rows.append(
            _make_row(
                base + timedelta(days=i),
                vix=17.0,
                move=0.3,
                pnl=-(500 + i * 20),
            )
        )
    df = pd.DataFrame(rows)
    out = stratify(df, event_dates={})
    stats = out["high_vix"]
    assert stats.num_trades == 25
    assert stats.sharpe < -0.5
    assert stats.passed is False


def test_stratify_small_losing_bucket_is_still_passed():
    """Fewer than 20 trades should NOT trigger a failure even if Sharpe negative."""
    rows = []
    base = date(2025, 1, 6)
    for i in range(5):
        rows.append(
            _make_row(base + timedelta(days=i), vix=17.0, pnl=-1000.0)
        )
    df = pd.DataFrame(rows)
    out = stratify(df, event_dates={})
    stats = out["high_vix"]
    assert stats.num_trades == 5
    # Small sample → passed=True regardless of Sharpe sign.
    assert stats.passed is True


def test_stratify_accepts_iso_string_date():
    rows = [
        {
            "date": "2025-01-06",
            "vix": 14.0,
            "is_expiry": 0,
            "dte": 5,
            "move_from_open_pct": 0.3,
            "outcome_pnl": 100.0,
        },
        {
            "date": "2025-01-07",
            "vix": 14.0,
            "is_expiry": 0,
            "dte": 5,
            "move_from_open_pct": 0.3,
            "outcome_pnl": 100.0,
        },
    ]
    df = pd.DataFrame(rows)
    out = stratify(df, event_dates={})
    assert out["mid_vix"].num_trades == 2


def test_stratify_accepts_timestamp_column():
    """Real decisions CSV format: `timestamp` column, no `date`."""
    rows = [
        {
            "timestamp": "2025-01-06T09:30:00+05:30",
            "vix": 14.0,
            "is_expiry": 0,
            "dte": 5,
            "move_from_open_pct": 0.3,
            "outcome_pnl": 100.0,
        },
        {
            "timestamp": "2025-01-07T09:30:00+05:30",
            "vix": 14.0,
            "is_expiry": 0,
            "dte": 5,
            "move_from_open_pct": 0.3,
            "outcome_pnl": 100.0,
        },
    ]
    df = pd.DataFrame(rows)
    out = stratify(df, event_dates={})
    assert out["mid_vix"].num_trades == 2


# ─── stratify ENTER/EXIT pairing (entry-time labelling) ──────────────


def _enter_row(
    d: date,
    *,
    vix: float,
    move: float,
    is_expiry: int = 0,
    dte: int = 5,
    leg: str = "PREMIUM",
    strategy_id: str = "portfolio_bt",
) -> dict:
    """ENTER row: entry-time vix/move/dte. outcome_pnl is NaN by convention."""
    return {
        "date": d,
        "decision": "ENTER",
        "strategy_id": strategy_id,
        "leg": leg,
        "vix": vix,
        "is_expiry": is_expiry,
        "dte": dte,
        "move_from_open_pct": move,
        "outcome_pnl": float("nan"),
    }


def _exit_row(
    d: date,
    *,
    vix: float,
    move: float,
    pnl: float,
    is_expiry: int = 0,
    dte: int = 5,
    leg: str = "PREMIUM",
    strategy_id: str = "portfolio_bt",
) -> dict:
    """EXIT row: exit-time vix/move; carries the realized pnl."""
    return {
        "date": d,
        "decision": "EXIT",
        "strategy_id": strategy_id,
        "leg": leg,
        "vix": vix,
        "is_expiry": is_expiry,
        "dte": dte,
        "move_from_open_pct": move,
        "outcome_pnl": pnl,
    }


def test_stratify_pairs_enter_exit_uses_entry_time_labels():
    """A trade entering at mid_vix/range_bound and exiting at high_vix/trending
    must be counted in the ENTRY-time buckets, not the exit-time ones.
    This is the contract that makes runtime regime gating measurable.
    """
    d = date(2025, 1, 6)
    rows = [
        # Entered at vix=14 (mid_vix), move=0.3 (range_bound).
        # Exited at vix=18 (high_vix), move=1.5 (trending). pnl=-1500.
        _enter_row(d, vix=14.0, move=0.3),
        _exit_row(d, vix=18.0, move=1.5, pnl=-1500.0),
    ]
    df = pd.DataFrame(rows)
    out = stratify(df, event_dates={})
    # Entry-time buckets: this trade is counted here with its -1500 pnl.
    assert out["mid_vix"].num_trades == 1
    assert out["mid_vix"].total_pnl == -1500.0
    assert out["range_bound"].num_trades == 1
    assert out["range_bound"].total_pnl == -1500.0
    # Exit-time buckets: must NOT see this trade — entry never satisfied.
    assert out["high_vix"].num_trades == 0
    assert out["high_vix"].total_pnl == 0.0
    assert out["trending"].num_trades == 0
    assert out["trending"].total_pnl == 0.0


def test_stratify_pairs_per_leg_independently():
    """Two trades on different legs at the same timestamp must pair within
    their own (strategy_id, leg) groups — premium and trend don't cross.
    """
    d = date(2025, 1, 6)
    rows = [
        _enter_row(d, vix=14.0, move=0.3, leg="PREMIUM"),
        _enter_row(d, vix=20.0, move=1.5, leg="TREND"),  # different entry regime
        _exit_row(d, vix=14.5, move=0.4, pnl=+100.0, leg="PREMIUM"),
        _exit_row(d, vix=20.5, move=1.6, pnl=-300.0, leg="TREND"),
    ]
    df = pd.DataFrame(rows)
    out = stratify(df, event_dates={})
    # PREMIUM trade: mid_vix + range_bound at entry, +100 pnl.
    # TREND trade: high_vix + trending at entry, -300 pnl.
    assert out["mid_vix"].total_pnl == 100.0
    assert out["range_bound"].total_pnl == 100.0
    assert out["high_vix"].total_pnl == -300.0
    assert out["trending"].total_pnl == -300.0


def test_stratify_legacy_fixture_without_decision_column_unchanged():
    """Synthetic test rows without a ``decision`` column fall through to
    the original per-row labelling behaviour. Locks in backwards-compat
    for the bulk of the existing regime test suite.
    """
    rows = [
        _make_row(date(2025, 1, 6) + timedelta(days=i), vix=20.0, move=0.2, pnl=10.0)
        for i in range(5)
    ]
    df = pd.DataFrame(rows)
    assert "decision" not in df.columns
    out = stratify(df, event_dates={})
    assert out["high_vix"].num_trades == 5
    assert out["high_vix"].total_pnl == 50.0


# ─── load_event_dates ────────────────────────────────────────────────


def test_load_event_dates_skips_comments_and_header(tmp_path: Path):
    csv = tmp_path / "events.csv"
    csv.write_text(
        """# comment line
# another comment

date,event_type,severity
2025-02-08,RBI_POLICY,HARD_BLOCK
# section separator
2025-04-10,FED_FOMC,SOFT_CAUTION
2025-06-15,OTHER,IGNORED_SEVERITY
""",
        encoding="utf-8",
    )
    out = load_event_dates(csv)
    assert out[date(2025, 2, 8)] == "RBI_POLICY"
    assert out[date(2025, 4, 10)] == "FED_FOMC"
    # Unknown severity must be skipped.
    assert date(2025, 6, 15) not in out


def test_load_event_dates_missing_file_returns_empty(tmp_path: Path):
    out = load_event_dates(tmp_path / "nonexistent.csv")
    assert out == {}
