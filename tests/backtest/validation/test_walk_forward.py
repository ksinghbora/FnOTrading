"""Tests for ``src.backtest.validation.walk_forward``.

Contracts locked in here:

  1. Window count formula: ``floor((n - W - T - E) / S) + 1``.
  2. First window's test slice starts at ``train_end + embargo_days``.
  3. ``WFReport.median_decay`` is median of per-window
     ``train_sharpe - test_sharpe``.
  4. ``WFReport.passed == True`` iff median_decay < 0.5 AND
     fraction_positive_test >= 0.7.
  5. ``optimizer_fn`` is called per window with
     ``(train_days, baseline_params_copy, validator)``; its returned
     params are threaded to ``runner_fn``.
  6. ``baseline_params`` is never mutated.
  7. Constructor argument guards.
"""

from __future__ import annotations

import math
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from src.backtest.validation.walk_forward import (  # noqa: E402
    WalkForwardValidator,
    WFReport,
    WFWindow,
)


@pytest.fixture
def dates_200() -> list[date]:
    return [ts.date() for ts in pd.bdate_range("2024-01-02", periods=200)]


# ─── Constructor ──────────────────────────────────────────────────────


def test_ctor_guards() -> None:
    with pytest.raises(ValueError):
        WalkForwardValidator(train_window_days=0)
    with pytest.raises(ValueError):
        WalkForwardValidator(test_window_days=0)
    with pytest.raises(ValueError):
        WalkForwardValidator(step_days=0)
    with pytest.raises(ValueError):
        WalkForwardValidator(embargo_days=-1)


# ─── Window arithmetic ────────────────────────────────────────────────


def test_window_count_and_boundaries(dates_200) -> None:
    wf = WalkForwardValidator(
        train_window_days=90, test_window_days=30,
        step_days=15, embargo_days=0,
    )
    bounds = wf._window_bounds(len(dates_200))
    expected = math.floor((200 - 90 - 30) / 15) + 1
    assert len(bounds) == expected == 6

    # First window: train [0, 90), test [90, 120)
    train_lo, train_hi, test_lo, test_hi = bounds[0]
    assert (train_lo, train_hi, test_lo, test_hi) == (0, 90, 90, 120)

    # Second: step by 15 -> train [15, 105), test [105, 135)
    train_lo, train_hi, test_lo, test_hi = bounds[1]
    assert (train_lo, train_hi, test_lo, test_hi) == (15, 105, 105, 135)


def test_embargo_applied(dates_200) -> None:
    wf = WalkForwardValidator(
        train_window_days=90, test_window_days=30,
        step_days=15, embargo_days=1,
    )
    bounds = wf._window_bounds(len(dates_200))
    # Now test_lo = train_hi + 1 = 91
    train_lo, train_hi, test_lo, test_hi = bounds[0]
    assert (train_lo, train_hi, test_lo, test_hi) == (0, 90, 91, 121)
    # Second: train [15, 105), test [106, 136)
    train_lo, train_hi, test_lo, test_hi = bounds[1]
    assert (train_lo, train_hi, test_lo, test_hi) == (15, 105, 106, 136)


def test_no_windows_when_too_short() -> None:
    wf = WalkForwardValidator(
        train_window_days=90, test_window_days=30,
        step_days=15, embargo_days=0,
    )
    # Need at least 120 dates for one window
    bounds = wf._window_bounds(50)
    assert bounds == []


# ─── run() aggregation ────────────────────────────────────────────────


async def _const_runner(train_dates, params):
    # Deterministic — every call returns the same metrics
    return {
        "metrics": {
            "sharpe_ratio": 1.0,
            "total_pnl": 1000.0,
            "num_trades": 40,
        }
    }


@pytest.mark.asyncio
async def test_run_produces_one_window_per_bound(dates_200) -> None:
    wf = WalkForwardValidator(
        train_window_days=90, test_window_days=30,
        step_days=15, embargo_days=0,
    )
    report = await wf.run(
        dates=dates_200, runner_fn=_const_runner,
        baseline_params={"stop": 0.3},
    )
    assert isinstance(report, WFReport)
    assert len(report.windows) == 6
    assert all(isinstance(w, WFWindow) for w in report.windows)
    # All identical under const runner
    assert all(w.train_sharpe == 1.0 for w in report.windows)
    assert all(w.test_sharpe == 1.0 for w in report.windows)


@pytest.mark.asyncio
async def test_median_decay_computation(dates_200) -> None:
    # Force decay = 2.0 - 1.0 = 1.0 on train, test alternates
    counter = {"i": 0}

    async def runner(train_dates, params):
        counter["i"] += 1
        # Odd calls are "train" (train happens before test in run()),
        # Even calls are "test". Make train sharpe=2.0, test sharpe=1.0
        # to produce decay=1.0 per window.
        is_train_call = counter["i"] % 2 == 1
        sharpe = 2.0 if is_train_call else 1.0
        return {"metrics": {"sharpe_ratio": sharpe, "total_pnl": 0.0,
                            "num_trades": 30}}

    wf = WalkForwardValidator(90, 30, 15, embargo_days=0)
    report = await wf.run(dates_200, runner, baseline_params={})
    assert report.median_decay == pytest.approx(1.0)
    assert report.fraction_positive_test == 1.0
    # decay 1.0 is NOT < 0.5 => passed False
    assert report.passed is False


@pytest.mark.asyncio
async def test_passed_true_when_decay_low_and_positive_fraction_high(dates_200) -> None:
    counter = {"i": 0}

    async def runner(train_dates, params):
        counter["i"] += 1
        # train sharpe=1.2, test sharpe=1.0 -> decay=0.2, all positive
        is_train_call = counter["i"] % 2 == 1
        return {"metrics": {"sharpe_ratio": 1.2 if is_train_call else 1.0,
                            "total_pnl": 0.0, "num_trades": 30}}

    wf = WalkForwardValidator(90, 30, 15, embargo_days=0)
    report = await wf.run(dates_200, runner, baseline_params={})
    assert report.median_decay == pytest.approx(0.2)
    assert report.fraction_positive_test == 1.0
    assert report.passed is True


@pytest.mark.asyncio
async def test_passed_false_when_positive_fraction_low(dates_200) -> None:
    # Alternate positive/negative test sharpe to push frac_positive below 0.7
    seq_call = {"i": 0}

    async def runner(train_dates, params):
        seq_call["i"] += 1
        is_train_call = seq_call["i"] % 2 == 1
        if is_train_call:
            # train sharpes always positive; decay low
            return {"metrics": {"sharpe_ratio": 0.3, "total_pnl": 0.0,
                                "num_trades": 30}}
        # test alternates signs (every other window positive)
        window_idx = (seq_call["i"] // 2) - 1  # 0-indexed
        sh = 0.1 if window_idx % 2 == 0 else -0.1
        return {"metrics": {"sharpe_ratio": sh, "total_pnl": 0.0,
                            "num_trades": 30}}

    wf = WalkForwardValidator(90, 30, 15, embargo_days=0)
    report = await wf.run(dates_200, runner, baseline_params={})
    # fraction positive ~ 0.5 => fails gate
    assert report.fraction_positive_test <= 0.5
    assert report.passed is False


# ─── optimizer_fn hook ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_optimizer_fn_called_with_train_days_and_validator(dates_200) -> None:
    seen_calls: list[tuple[int, int, object]] = []

    def optimizer(train_days, params, validator):
        seen_calls.append(
            (len(train_days), len(params), validator)
        )
        # Return distinct params per window to verify threading
        return dict(params, tuned_epoch=len(seen_calls))

    threaded_params: list[dict] = []

    async def runner(train_dates, params):
        threaded_params.append(dict(params))
        return {"metrics": {"sharpe_ratio": 0.5, "total_pnl": 0.0,
                            "num_trades": 30}}

    wf = WalkForwardValidator(90, 30, 15, embargo_days=0)
    baseline = {"stop": 0.3}
    report = await wf.run(
        dates_200, runner, baseline_params=baseline, optimizer_fn=optimizer,
    )

    # 6 windows -> 6 optimizer invocations; each invocation gets the
    # validator itself (so hooks can read train/test windowing config).
    assert len(seen_calls) == 6
    assert all(call[0] == 90 for call in seen_calls)
    assert all(call[2] is wf for call in seen_calls)

    # Runner called 2x per window (train + test) => 12 total, each with
    # the optimized params for that window.
    assert len(threaded_params) == 12
    # tuned_epoch threads through to every runner call for that window.
    epochs = [p.get("tuned_epoch") for p in threaded_params]
    # Each epoch value appears exactly twice (train + test call)
    assert sorted(epochs) == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6]

    # WFWindow.params must reflect the optimized values (not baseline)
    for i, w in enumerate(report.windows, start=1):
        assert w.params.get("tuned_epoch") == i
        assert w.params["stop"] == 0.3

    # Baseline untouched
    assert baseline == {"stop": 0.3}


@pytest.mark.asyncio
async def test_baseline_params_not_mutated_without_optimizer(dates_200) -> None:
    baseline = {"stop": 0.3, "wing": 150}

    async def runner(train_dates, params):
        params["mutated"] = True  # try to leak up
        return {"metrics": {"sharpe_ratio": 0.5, "total_pnl": 0.0,
                            "num_trades": 30}}

    wf = WalkForwardValidator(90, 30, 15, embargo_days=0)
    await wf.run(dates_200, runner, baseline_params=baseline)
    assert baseline == {"stop": 0.3, "wing": 150}


@pytest.mark.asyncio
async def test_empty_when_dates_shorter_than_one_window() -> None:
    wf = WalkForwardValidator(90, 30, 15, embargo_days=1)
    short = [ts.date() for ts in pd.bdate_range("2024-01-02", periods=50)]

    async def runner(train_dates, params):
        return {"metrics": {"sharpe_ratio": 0.0, "num_trades": 0}}

    report = await wf.run(short, runner, baseline_params={})
    assert report.windows == []
    assert report.passed is False
    assert report.median_decay == 0.0
    assert report.fraction_positive_test == 0.0
