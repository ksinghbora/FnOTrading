"""Tests for ``src.backtest.validation.cpcv``.

Contracts locked in here:

  1. ``(n_folds=10, n_test_folds=2)`` yields ``C(10, 2)=45`` paths,
     which is below the 50-cap so all 45 are generated.
  2. ``(n_folds=15, n_test_folds=2)`` yields ``C(15, 2)=105`` combos ->
     trimmed to ``max_paths=50``.
  3. Train/test index sets have zero intersection for every path.
  4. Embargo is applied: no test index is adjacent (within embargo band)
     to a train index.
  5. ``evaluate`` with a mock runner returning Sharpe=1.0 produces
     ``sharpe_median == 1.0`` and ``sharpe_distribution`` shape
     ``(n_paths,)``.
  6. ``param_set`` is not mutated across paths.
  7. Constructor guards invalid args.
"""

from __future__ import annotations

import math
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from src.backtest.validation.cpcv import (  # noqa: E402
    CombinatorialPurgedCV,
    CPCVPath,
)


@pytest.fixture
def dates_200() -> list[date]:
    return [ts.date() for ts in pd.bdate_range("2024-01-02", periods=200)]


# ─── Constructor ──────────────────────────────────────────────────────


def test_ctor_guards() -> None:
    with pytest.raises(ValueError):
        CombinatorialPurgedCV(n_folds=1)
    with pytest.raises(ValueError):
        CombinatorialPurgedCV(n_folds=5, n_test_folds=0)
    with pytest.raises(ValueError):
        CombinatorialPurgedCV(n_folds=5, n_test_folds=5)
    with pytest.raises(ValueError):
        CombinatorialPurgedCV(n_folds=5, embargo_pct=-0.1)
    with pytest.raises(ValueError):
        CombinatorialPurgedCV(n_folds=5, embargo_pct=0.5)
    with pytest.raises(ValueError):
        CombinatorialPurgedCV(n_folds=5, max_paths=0)


# ─── Combinatorial path count ─────────────────────────────────────────


def test_10_choose_2_yields_45_paths(dates_200) -> None:
    cv = CombinatorialPurgedCV(n_folds=10, n_test_folds=2, embargo_pct=0.0,
                               max_paths=50)
    paths = list(cv.split(dates_200))
    assert len(paths) == math.comb(10, 2) == 45


def test_15_choose_2_capped_at_max_paths(dates_200) -> None:
    cv = CombinatorialPurgedCV(n_folds=15, n_test_folds=2, embargo_pct=0.0,
                               max_paths=50)
    paths = list(cv.split(dates_200))
    assert len(paths) == 50


def test_subsampling_is_deterministic(dates_200) -> None:
    cv_a = CombinatorialPurgedCV(n_folds=15, n_test_folds=2, embargo_pct=0.0,
                                 max_paths=50, seed=123)
    cv_b = CombinatorialPurgedCV(n_folds=15, n_test_folds=2, embargo_pct=0.0,
                                 max_paths=50, seed=123)
    a = [tfids for _, _, tfids in cv_a.split(dates_200)]
    b = [tfids for _, _, tfids in cv_b.split(dates_200)]
    assert a == b


# ─── Train/test purity ────────────────────────────────────────────────


def test_train_test_disjoint_every_path(dates_200) -> None:
    cv = CombinatorialPurgedCV(n_folds=10, n_test_folds=2, embargo_pct=0.0)
    for train_idx, test_idx, _ in cv.split(dates_200):
        assert set(train_idx).isdisjoint(test_idx)


def test_embargo_excludes_adjacent_train(dates_200) -> None:
    # Embargo = 1% of 200 = 2 days
    embargo_pct = 0.02
    expected_embargo = max(1, math.ceil(embargo_pct * len(dates_200)))
    cv = CombinatorialPurgedCV(
        n_folds=10, n_test_folds=2,
        embargo_pct=embargo_pct,
    )
    for train_idx, test_idx, _ in cv.split(dates_200):
        train_set = set(train_idx)
        test_sorted = sorted(test_idx)
        # For every test index boundary, verify no train index within
        # `expected_embargo` positions.
        for t in test_sorted:
            for delta in range(1, expected_embargo + 1):
                assert (t - delta) not in train_set or t - delta < 0
                assert (t + delta) not in train_set or t + delta >= len(dates_200)


def test_test_region_contiguity_per_fold(dates_200) -> None:
    """With n_test_folds=1, the single test region must be contiguous."""
    cv = CombinatorialPurgedCV(n_folds=10, n_test_folds=1, embargo_pct=0.0)
    for _, test_idx, _ in cv.split(dates_200):
        # Diff must always be 1 across sorted indices
        ts = sorted(test_idx)
        diffs = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
        assert all(d == 1 for d in diffs)


# ─── evaluate() aggregation ───────────────────────────────────────────


async def _fake_runner(train_dates, params):
    return {
        "metrics": {
            "sharpe_ratio": 1.0,
            "total_pnl": 1000.0,
            "num_trades": 50,
            "profit_factor": 1.5,
            "max_drawdown": -200.0,
        }
    }


@pytest.mark.asyncio
async def test_evaluate_aggregates_sharpe_distribution(dates_200) -> None:
    cv = CombinatorialPurgedCV(n_folds=10, n_test_folds=2, embargo_pct=0.0)
    result = await cv.evaluate(
        param_set={"stop": 0.3},
        runner_fn=_fake_runner,
        dates=dates_200,
    )
    assert result["sharpe_median"] == 1.0
    assert result["sharpe_mean"] == 1.0
    assert isinstance(result["sharpe_distribution"], np.ndarray)
    assert result["sharpe_distribution"].shape == (45,)
    assert result["pbo"] is None
    assert result["num_trades_mean"] == 50.0
    assert all(isinstance(p, CPCVPath) for p in result["paths"])
    # Path train/test dates materialised
    for p in result["paths"]:
        assert len(p.train_dates) > 0
        assert len(p.test_dates) > 0


@pytest.mark.asyncio
async def test_evaluate_does_not_mutate_param_set(dates_200) -> None:
    seen_ids = []

    async def runner(train_dates, params):
        seen_ids.append(id(params))
        params["injected"] = True  # try to leak
        return {"metrics": {"sharpe_ratio": 0.5, "num_trades": 40}}

    cv = CombinatorialPurgedCV(n_folds=5, n_test_folds=2, embargo_pct=0.0)
    orig = {"stop": 0.3}
    await cv.evaluate(param_set=orig, runner_fn=runner, dates=dates_200)
    assert "injected" not in orig
    # Each path got a distinct copy
    assert len(set(seen_ids)) > 1


@pytest.mark.asyncio
async def test_evaluate_handles_varying_sharpe(dates_200) -> None:
    counter = {"i": 0}

    async def runner(train_dates, params):
        counter["i"] += 1
        return {"metrics": {"sharpe_ratio": float(counter["i"]), "num_trades": 40}}

    cv = CombinatorialPurgedCV(n_folds=5, n_test_folds=2, embargo_pct=0.0)
    # C(5, 2) = 10 paths
    result = await cv.evaluate(
        param_set={}, runner_fn=runner, dates=dates_200,
    )
    dist = result["sharpe_distribution"]
    assert dist.shape == (10,)
    # Runner returned 1..10, so median should be 5.5
    assert result["sharpe_median"] == pytest.approx(5.5)
    assert result["sharpe_mean"] == pytest.approx(5.5)


@pytest.mark.asyncio
async def test_small_trade_count_logs_warning(dates_200, caplog) -> None:
    async def runner(train_dates, params):
        return {"metrics": {"sharpe_ratio": 0.0, "num_trades": 5}}

    cv = CombinatorialPurgedCV(n_folds=5, n_test_folds=2, embargo_pct=0.0)
    with caplog.at_level("WARNING"):
        await cv.evaluate(param_set={}, runner_fn=runner, dates=dates_200)
    warned = [r for r in caplog.records if "small-sample" in r.getMessage()]
    assert len(warned) >= 1
