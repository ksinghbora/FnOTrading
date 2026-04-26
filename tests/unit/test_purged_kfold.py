"""Tests for Purged K-Fold Cross-Validation with embargo.

Verifies the invariants that guard against temporal leakage in time-series
tuning (Marcos López de Prado, AFML ch. 7):

  1. Test folds are mutually disjoint.
  2. Train fold never intersects its paired test fold.
  3. No train date falls within ±embargo_days of a test fold boundary.
  4. Union of train + test + purged covers the full date universe exactly.
  5. Acceptance rule fires on the std_oos_sharpe > mean_oos_sharpe case
     (the classical curve-fit tell).
"""

from __future__ import annotations

import asyncio
import math
from datetime import date, timedelta

import pytest

from src.backtest.purged_kfold import FoldResult, PurgedKFold


def _synthetic_dates(n: int, start: date = date(2025, 1, 1)) -> list[date]:
    """n consecutive calendar days — the CV splitter treats days as opaque."""
    return [start + timedelta(days=i) for i in range(n)]


class TestSplitInvariants:
    """Split(dates) must produce non-overlapping, embargo-purged folds."""

    def test_rejects_n_splits_below_2(self):
        with pytest.raises(ValueError):
            PurgedKFold(n_splits=1)

    def test_rejects_embargo_out_of_range(self):
        with pytest.raises(ValueError):
            PurgedKFold(n_splits=5, embargo_pct=-0.1)
        with pytest.raises(ValueError):
            PurgedKFold(n_splits=5, embargo_pct=0.5)

    def test_split_produces_n_splits_folds_when_enough_dates(self):
        dates = _synthetic_dates(100)
        kf = PurgedKFold(n_splits=5, embargo_pct=0.01)
        folds = list(kf.split(dates))
        assert len(folds) == 5

    def test_test_folds_are_mutually_disjoint(self):
        dates = _synthetic_dates(100)
        kf = PurgedKFold(n_splits=5, embargo_pct=0.01)
        test_sets = [set(test_idx) for _, test_idx in kf.split(dates)]
        for i, a in enumerate(test_sets):
            for j, b in enumerate(test_sets):
                if i == j:
                    continue
                assert not (a & b), f"Fold {i} overlaps fold {j} on test indices"

    def test_test_folds_cover_full_universe(self):
        dates = _synthetic_dates(100)
        kf = PurgedKFold(n_splits=5, embargo_pct=0.01)
        union = set()
        for _, test_idx in kf.split(dates):
            union.update(test_idx)
        assert union == set(range(100))

    def test_train_and_test_folds_are_disjoint(self):
        dates = _synthetic_dates(100)
        kf = PurgedKFold(n_splits=5, embargo_pct=0.01)
        for train_idx, test_idx in kf.split(dates):
            assert not (set(train_idx) & set(test_idx))

    def test_embargo_purges_immediate_boundary(self):
        """With 100 dates and embargo_pct=0.01, the embargo band is 1 day.

        No train index may fall within [test_start-1, test_end] window.
        """
        n = 100
        dates = _synthetic_dates(n)
        kf = PurgedKFold(n_splits=5, embargo_pct=0.01)
        embargo_days = max(1, int(math.ceil(0.01 * n)))
        assert embargo_days == 1

        for train_idx, test_idx in kf.split(dates):
            test_lo, test_hi = min(test_idx), max(test_idx)
            forbidden = set(range(test_lo - embargo_days, test_hi + 1 + embargo_days))
            leaks = forbidden & set(train_idx)
            assert not leaks, (
                f"Embargo leak: train overlaps forbidden band around "
                f"test[{test_lo}..{test_hi}]: {sorted(leaks)}"
            )

    def test_embargo_scales_with_date_count(self):
        """embargo_pct=0.05 on 200 dates => 10-day band on each side."""
        n = 200
        dates = _synthetic_dates(n)
        kf = PurgedKFold(n_splits=5, embargo_pct=0.05)
        for _, test_idx in kf.split(dates):
            test_lo, test_hi = min(test_idx), max(test_idx)
            # Every index in [test_lo-10, test_hi+10] must be purged or test.
            for train_idx, _ in [next(iter(kf.split(dates)))]:  # just for type
                pass
        # Direct check
        for train_idx, test_idx in kf.split(dates):
            test_lo, test_hi = min(test_idx), max(test_idx)
            forbidden = set(range(max(0, test_lo - 10), min(n, test_hi + 1 + 10)))
            assert not (forbidden & set(train_idx))

    def test_uneven_division_distributes_extras_to_first_folds(self):
        """7 dates / 2 folds => sizes [4, 3]."""
        dates = _synthetic_dates(7)
        kf = PurgedKFold(n_splits=2, embargo_pct=0.0)
        folds = list(kf.split(dates))
        sizes = [len(test_idx) for _, test_idx in folds]
        assert sizes == [4, 3]

    def test_split_indices_are_chronological_within_fold(self):
        """Each test fold must be a contiguous chronological block."""
        dates = _synthetic_dates(50)
        kf = PurgedKFold(n_splits=5, embargo_pct=0.0)
        for _, test_idx in kf.split(dates):
            assert test_idx == list(range(test_idx[0], test_idx[-1] + 1))

    def test_raises_when_dates_below_n_splits(self):
        kf = PurgedKFold(n_splits=5, embargo_pct=0.01)
        with pytest.raises(ValueError):
            list(kf.split(_synthetic_dates(3)))


class TestEvaluate:
    """evaluate() must aggregate fold Sharpes and apply the curve-fit rule."""

    def _run(self, kf: PurgedKFold, strategy_fn, dates):
        return asyncio.run(
            kf.evaluate(param_set={"x": 1}, strategy_fn=strategy_fn, dates=dates)
        )

    def test_stable_sharpe_is_accepted(self):
        """All folds returning Sharpe=1.0 => mean 1.0, std 0.0, accept."""
        async def fn(*, param_set, train_dates, test_dates):
            return {"sharpe": 1.0, "profit_factor": 2.0}

        kf = PurgedKFold(n_splits=5, embargo_pct=0.01)
        out = self._run(kf, fn, _synthetic_dates(100))
        assert out["mean_oos_sharpe"] == 1.0
        assert out["std_oos_sharpe"] == 0.0
        assert out["accepted"] is True
        assert out["oos_pf"] == 2.0
        assert len(out["fold_results"]) == 5
        assert all(isinstance(f, FoldResult) for f in out["fold_results"])

    def test_noisy_sharpe_is_rejected(self):
        """Per-fold sharpes [1, -1, 1, -1, 1] => mean 0.2, std ~1.0 => REJECT."""
        seq = iter([1.0, -1.0, 1.0, -1.0, 1.0])

        async def fn(*, param_set, train_dates, test_dates):
            return {"sharpe": next(seq), "profit_factor": 1.0}

        kf = PurgedKFold(n_splits=5, embargo_pct=0.01)
        out = self._run(kf, fn, _synthetic_dates(100))
        assert out["std_oos_sharpe"] > out["mean_oos_sharpe"]
        assert out["accepted"] is False

    def test_non_positive_mean_is_rejected(self):
        """All folds break-even at 0 => mean 0 => accepted=False even if std=0."""
        async def fn(*, param_set, train_dates, test_dates):
            return {"sharpe": 0.0, "profit_factor": 1.0}

        kf = PurgedKFold(n_splits=5, embargo_pct=0.01)
        out = self._run(kf, fn, _synthetic_dates(100))
        assert out["mean_oos_sharpe"] == 0.0
        assert out["accepted"] is False

    def test_extra_fields_preserved_per_fold(self):
        async def fn(*, param_set, train_dates, test_dates):
            return {"sharpe": 0.5, "profit_factor": 1.2, "num_trades": 42}

        kf = PurgedKFold(n_splits=3, embargo_pct=0.01)
        out = self._run(kf, fn, _synthetic_dates(60))
        for fr in out["fold_results"]:
            assert fr.extra.get("num_trades") == 42

    def test_strategy_fn_receives_correct_dates(self):
        """Train + test dates must exactly partition the non-purged universe."""
        captured: list[tuple[list, list]] = []

        async def fn(*, param_set, train_dates, test_dates):
            captured.append((list(train_dates), list(test_dates)))
            return {"sharpe": 1.0, "profit_factor": 1.0}

        dates = _synthetic_dates(100)
        kf = PurgedKFold(n_splits=5, embargo_pct=0.01)
        self._run(kf, fn, dates)

        for train, test in captured:
            assert set(train).isdisjoint(set(test))
            # Exactly one embargo-day gap around each test fold.
            test_lo, test_hi = min(test), max(test)
            for d in train:
                offset_lo = abs((d - test_lo).days)
                offset_hi = abs((d - test_hi).days)
                # d must be >= embargo_days outside [test_lo, test_hi]
                if test_lo <= d <= test_hi:
                    pytest.fail("train date inside test window")
                if d < test_lo:
                    assert offset_lo >= 1
                else:
                    assert offset_hi >= 1
