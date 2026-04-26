"""Tests for advanced Sharpe statistics (PSR/DSR/minTRL/PBO/bootstrap)."""

from __future__ import annotations

import math
import sys

import numpy as np
import pytest
from scipy.stats import norm

from src.backtest.validation.metrics import (
    _annualized_sharpe,
    deflated_sharpe_ratio,
    min_track_record_length,
    pbo,
    probabilistic_sharpe_ratio,
    stationary_bootstrap_sharpe_ci,
)


def test_psr_normal_returns() -> None:
    # Sharpe=0.5, n=252, skew=0, kurt=3, benchmark=0
    # denom_sq = 1 - 0*0.5 + ((3-1)/4)*0.25 = 1 + 0.125 = 1.125
    # z = 0.5 * sqrt(251) / sqrt(1.125)
    psr = probabilistic_sharpe_ratio(0.5, 252, skew=0.0, kurt=3.0, benchmark=0.0)
    z = 0.5 * math.sqrt(251) / math.sqrt(1.125)
    expected = float(norm.cdf(z))
    assert psr == pytest.approx(expected, abs=1e-10)
    assert psr > 0.99


def test_psr_below_benchmark() -> None:
    psr = probabilistic_sharpe_ratio(0.5, 252, skew=0.0, kurt=3.0, benchmark=1.0)
    assert psr < 0.5


def test_dsr_adjusts_for_trials() -> None:
    # Use params that don't saturate the normal CDF (sharpe=2, n=252 → both round to 1.0).
    # Fact under test: more trials → lower DSR (stricter benchmark).
    psr = probabilistic_sharpe_ratio(1.0, 100, skew=0.0, kurt=3.0, benchmark=0.0)
    dsr_many = deflated_sharpe_ratio(1.0, 0.5, 100, 100, 0.0, 3.0)
    dsr_few = deflated_sharpe_ratio(1.0, 0.5, 10, 100, 0.0, 3.0)
    assert dsr_many < dsr_few < psr


def test_dsr_single_trial_equals_psr_with_zero_benchmark() -> None:
    dsr = deflated_sharpe_ratio(1.0, 0.0, 1, 252, 0.0, 3.0)
    psr = probabilistic_sharpe_ratio(1.0, 252, skew=0.0, kurt=3.0, benchmark=0.0)
    assert dsr == pytest.approx(psr, abs=1e-6)


def test_min_trl_monotone() -> None:
    high_sig = min_track_record_length(1.0, 0.0, 0.0, 3.0, 0.95)
    low_sig = min_track_record_length(0.2, 0.0, 0.0, 3.0, 0.95)
    assert high_sig < low_sig


def test_min_trl_returns_maxsize_when_below_benchmark() -> None:
    assert min_track_record_length(0.1, 0.5, 0.0, 3.0, 0.95) == sys.maxsize


def test_pbo_perfect_correlation() -> None:
    # Same sort order — best-in-train is best-in-test → PBO = 0
    rng = np.random.default_rng(42)
    train = rng.normal(size=(10, 20))
    # test is monotone transform (x + small noise in same order) → ranks preserved
    test = train.copy() + 0.0
    p = pbo(train, test)
    assert p == pytest.approx(0.0, abs=1e-9)


def test_pbo_anti_correlation() -> None:
    rng = np.random.default_rng(42)
    train = rng.normal(size=(10, 20))
    test = -train
    p = pbo(train, test)
    assert p == pytest.approx(1.0, abs=1e-9)


def test_pbo_random() -> None:
    rng = np.random.default_rng(123)
    train = rng.normal(size=(10, 20))
    test = rng.normal(size=(10, 20))
    p = pbo(train, test)
    assert 0.3 <= p <= 0.7


def test_bootstrap_reproducibility() -> None:
    rng = np.random.default_rng(7)
    returns = rng.normal(0.001, 0.01, size=100)
    ci1 = stationary_bootstrap_sharpe_ci(returns, 5, 500, 0.95, seed=99)
    ci2 = stationary_bootstrap_sharpe_ci(returns, 5, 500, 0.95, seed=99)
    assert ci1 == ci2


def test_bootstrap_narrow_for_many_samples() -> None:
    rng = np.random.default_rng(11)
    small = rng.normal(0.001, 0.01, size=50)
    large = rng.normal(0.001, 0.01, size=500)
    lo_s, _, hi_s = stationary_bootstrap_sharpe_ci(small, 5, 500, 0.95, seed=1)
    lo_l, _, hi_l = stationary_bootstrap_sharpe_ci(large, 5, 500, 0.95, seed=1)
    assert (hi_l - lo_l) < (hi_s - lo_s)


def test_bootstrap_covers_point_estimate() -> None:
    rng = np.random.default_rng(3)
    returns = rng.normal(0.001, 0.01, size=300)
    point = _annualized_sharpe(returns)
    low, _, high = stationary_bootstrap_sharpe_ci(returns, 5, 1000, 0.95, seed=5)
    assert low <= point <= high
