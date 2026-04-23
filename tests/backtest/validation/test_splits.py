"""Tests for ``src.backtest.validation.splits``.

Contracts locked in here:

  1. ``train_days`` / ``val_days`` / ``holdout_days`` enumerate the
     expected inclusive business-day windows.
  2. Holdout access counter increments on every call and is persisted to
     ``state_dir/holdout_access.json``.
  3. A second ``holdout_days()`` call raises :class:`HoldoutOverUsed`.
  4. ``holdout_days(allow_burn=True)`` bypasses the raise but still
     increments the counter (audit trail).
  5. The counter survives re-instantiation of ``SplitLoader`` (same
     state_dir) — no in-memory-only guard.
  6. Without a GDFL source, ``pd.bdate_range`` drives the domain.
  7. With a GDFL source, windows intersect with ``available_days()``.
  8. ``load_decisions`` returns an empty DataFrame for nonexistent days
     (no error).
  9. ``StrategySplit`` validates boundary ordering.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from src.backtest.validation.splits import (  # noqa: E402
    HoldoutOverUsed,
    SplitLoader,
    StrategySplit,
)


class _FakeGDFL:
    """Minimal stand-in with just ``available_days()``."""

    def __init__(self, days: list[date]):
        self._days = sorted(days)

    def available_days(self) -> list[date]:
        return list(self._days)


# ─── StrategySplit boundary validation ────────────────────────────────


def test_strategy_split_rejects_bad_ordering() -> None:
    with pytest.raises(ValueError):
        StrategySplit(
            train_end=date(2025, 12, 31),
            val_end=date(2025, 12, 31),  # equal to train_end
            holdout_end=date(2026, 3, 31),
        )
    with pytest.raises(ValueError):
        StrategySplit(
            train_end=date(2025, 12, 31),
            val_end=date(2025, 11, 30),  # before train_end
            holdout_end=date(2026, 3, 31),
        )


# ─── Windows with explicit GDFL source ────────────────────────────────


@pytest.fixture
def gdfl_days() -> list[date]:
    # Business-day set spanning Sep 2025 to April 2026; we'll take every
    # business day in that range as "available".
    return [ts.date() for ts in pd.bdate_range("2025-09-01", "2026-04-30")]


@pytest.fixture
def split() -> StrategySplit:
    return StrategySplit(
        train_end=date(2025, 12, 31),
        val_end=date(2026, 2, 27),
        holdout_end=date(2026, 4, 22),
    )


def test_train_val_holdout_windows(tmp_path: Path, split, gdfl_days) -> None:
    gdfl = _FakeGDFL(gdfl_days)
    loader = SplitLoader(
        split=split,
        strategy="portfolio",
        decisions_dir=tmp_path / "decisions",
        gdfl_source=gdfl,
        state_dir=tmp_path / "state",
    )

    train = loader.train_days()
    val = loader.val_days()
    hold = loader.holdout_days()  # first access OK

    # Disjoint and chronologically adjacent
    assert train[-1] <= split.train_end
    assert val[0] > split.train_end
    assert val[-1] <= split.val_end
    assert hold[0] > split.val_end
    assert hold[-1] <= split.holdout_end

    # No overlap
    assert set(train).isdisjoint(val)
    assert set(val).isdisjoint(hold)
    assert set(train).isdisjoint(hold)

    # Every returned day is in the GDFL-available set
    avail = set(gdfl_days)
    assert set(train).issubset(avail)
    assert set(val).issubset(avail)
    assert set(hold).issubset(avail)


def test_windows_without_gdfl_use_bdate_range(tmp_path: Path) -> None:
    split = StrategySplit(
        train_end=date(2026, 1, 9),
        val_end=date(2026, 1, 16),
        holdout_end=date(2026, 1, 23),
    )
    loader = SplitLoader(
        split=split,
        strategy="mini",
        decisions_dir=tmp_path / "decisions",
        state_dir=tmp_path / "state",
    )
    val = loader.val_days()
    # Jan 12-16 2026: Mon-Fri all business days
    expected = [ts.date() for ts in pd.bdate_range("2026-01-12", "2026-01-16")]
    assert val == expected


# ─── Holdout access counter ───────────────────────────────────────────


def test_holdout_counter_first_access_ok(tmp_path: Path, split, gdfl_days) -> None:
    gdfl = _FakeGDFL(gdfl_days)
    loader = SplitLoader(
        split=split, strategy="portfolio",
        decisions_dir=tmp_path / "decisions",
        gdfl_source=gdfl,
        state_dir=tmp_path / "state",
    )
    assert loader.holdout_access_count() == 0
    days = loader.holdout_days()
    assert len(days) > 0
    assert loader.holdout_access_count() == 1


def test_second_holdout_access_raises(tmp_path: Path, split, gdfl_days) -> None:
    gdfl = _FakeGDFL(gdfl_days)
    loader = SplitLoader(
        split=split, strategy="portfolio",
        decisions_dir=tmp_path / "decisions",
        gdfl_source=gdfl,
        state_dir=tmp_path / "state",
    )
    loader.holdout_days()
    with pytest.raises(HoldoutOverUsed):
        loader.holdout_days()


def test_allow_burn_bypasses_raise_but_still_increments(
    tmp_path: Path, split, gdfl_days,
) -> None:
    gdfl = _FakeGDFL(gdfl_days)
    loader = SplitLoader(
        split=split, strategy="portfolio",
        decisions_dir=tmp_path / "decisions",
        gdfl_source=gdfl,
        state_dir=tmp_path / "state",
    )
    loader.holdout_days()
    # allow_burn=True should NOT raise
    loader.holdout_days(allow_burn=True)
    loader.holdout_days(allow_burn=True)
    assert loader.holdout_access_count() == 3


def test_counter_persists_across_instances(
    tmp_path: Path, split, gdfl_days,
) -> None:
    gdfl = _FakeGDFL(gdfl_days)
    state_dir = tmp_path / "state"
    loader_a = SplitLoader(
        split=split, strategy="portfolio",
        decisions_dir=tmp_path / "decisions",
        gdfl_source=gdfl, state_dir=state_dir,
    )
    loader_a.holdout_days()

    # New instance same state_dir — counter persists
    loader_b = SplitLoader(
        split=split, strategy="portfolio",
        decisions_dir=tmp_path / "decisions",
        gdfl_source=gdfl, state_dir=state_dir,
    )
    assert loader_b.holdout_access_count() == 1
    with pytest.raises(HoldoutOverUsed):
        loader_b.holdout_days()


def test_different_strategies_independent(
    tmp_path: Path, split, gdfl_days,
) -> None:
    gdfl = _FakeGDFL(gdfl_days)
    state_dir = tmp_path / "state"
    a = SplitLoader(split=split, strategy="one",
                    decisions_dir=tmp_path / "dec", gdfl_source=gdfl,
                    state_dir=state_dir)
    b = SplitLoader(split=split, strategy="two",
                    decisions_dir=tmp_path / "dec", gdfl_source=gdfl,
                    state_dir=state_dir)
    a.holdout_days()
    # b has its own counter
    assert b.holdout_access_count() == 0
    b.holdout_days()  # doesn't raise
    assert b.holdout_access_count() == 1


def test_access_limit_configurable(
    tmp_path: Path, split, gdfl_days,
) -> None:
    gdfl = _FakeGDFL(gdfl_days)
    loader = SplitLoader(
        split=split, strategy="portfolio",
        decisions_dir=tmp_path / "decisions",
        gdfl_source=gdfl,
        state_dir=tmp_path / "state",
        access_limit=3,
    )
    loader.holdout_days()
    loader.holdout_days()
    loader.holdout_days()
    with pytest.raises(HoldoutOverUsed):
        loader.holdout_days()


# ─── Decision CSV loader ──────────────────────────────────────────────


def test_load_decisions_missing_days_returns_empty_df(
    tmp_path: Path, split, gdfl_days,
) -> None:
    loader = SplitLoader(
        split=split, strategy="portfolio",
        decisions_dir=tmp_path / "decisions",   # directory doesn't exist
        state_dir=tmp_path / "state",
    )
    df = loader.load_decisions([date(2025, 10, 1), date(2025, 10, 2)])
    assert isinstance(df, pd.DataFrame)
    assert df.empty


def test_load_decisions_concatenates_existing(tmp_path: Path, split) -> None:
    dec_dir = tmp_path / "decisions"
    dec_dir.mkdir()

    # Write two CSVs with 2 rows each
    pd.DataFrame({"a": [1, 2], "b": [3, 4]}).to_csv(
        dec_dir / "decisions_2026-01-05.csv", index=False,
    )
    pd.DataFrame({"a": [5, 6], "b": [7, 8]}).to_csv(
        dec_dir / "decisions_2026-01-06.csv", index=False,
    )

    loader = SplitLoader(
        split=split, strategy="portfolio",
        decisions_dir=dec_dir,
        state_dir=tmp_path / "state",
    )
    df = loader.load_decisions(
        [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]  # third is missing
    )
    assert len(df) == 4
    assert list(df.columns) == ["a", "b"]


# ─── available_gdfl_days ──────────────────────────────────────────────


def test_available_gdfl_days_intersects(tmp_path: Path, split) -> None:
    gdfl = _FakeGDFL([date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)])
    loader = SplitLoader(
        split=split, strategy="portfolio",
        decisions_dir=tmp_path / "decisions",
        gdfl_source=gdfl,
        state_dir=tmp_path / "state",
    )
    out = loader.available_gdfl_days(
        [date(2026, 1, 5), date(2026, 1, 8), date(2026, 1, 7)]
    )
    assert out == [date(2026, 1, 5), date(2026, 1, 7)]


def test_available_gdfl_days_noop_without_source(tmp_path: Path, split) -> None:
    loader = SplitLoader(
        split=split, strategy="portfolio",
        decisions_dir=tmp_path / "decisions",
        state_dir=tmp_path / "state",
    )
    days = [date(2026, 1, 5), date(2026, 1, 6)]
    assert loader.available_gdfl_days(days) == days
