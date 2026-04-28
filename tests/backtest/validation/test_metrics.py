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
    monte_carlo_skill_pvalue,
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


# ─── Monte Carlo skill p-value (replaces dropped DSR gate) ───────────


def test_mc_pvalue_reproducible() -> None:
    rng = np.random.default_rng(11)
    pnl = rng.normal(100.0, 1000.0, size=120)
    p1 = monte_carlo_skill_pvalue(pnl, n_perm=2000, block_size_mean=15.0, seed=42)
    p2 = monte_carlo_skill_pvalue(pnl, n_perm=2000, block_size_mean=15.0, seed=42)
    assert p1 == p2


def test_mc_pvalue_strong_skill_rejects_null() -> None:
    """Daily PnL with strong positive drift (mean=200, sd=1000, n=200)
    should produce p << 0.10 — null of zero-mean returns is strongly
    rejected."""
    rng = np.random.default_rng(0)
    pnl = rng.normal(200.0, 1000.0, size=200)
    p = monte_carlo_skill_pvalue(pnl, n_perm=5000, block_size_mean=15.0, seed=1)
    assert p < 0.05


def test_mc_pvalue_negative_drift_does_not_reject_null() -> None:
    """Negative-drift PnL cannot beat the zero-mean null — p ~ 1.0."""
    rng = np.random.default_rng(2)
    pnl = rng.normal(-150.0, 1000.0, size=200)
    p = monte_carlo_skill_pvalue(pnl, n_perm=5000, block_size_mean=15.0, seed=3)
    assert p > 0.90


def test_mc_pvalue_degenerate_inputs() -> None:
    """Empty / single-point / zero-variance series → p=1.0 (no skill)."""
    assert monte_carlo_skill_pvalue(np.array([]), seed=1) == 1.0
    assert monte_carlo_skill_pvalue(np.array([100.0]), seed=2) == 1.0
    assert monte_carlo_skill_pvalue(np.zeros(50), seed=3) == 1.0


def test_mc_pvalue_benchmark_lifts_null() -> None:
    """The benchmark parameter is in daily-PnL ₹ units (it shifts the
    null mean before bootstrapping). Raising it from 0 toward the
    observed sample mean should monotonically push p higher — the null
    distribution moves into and through the observed-Sharpe region."""
    rng = np.random.default_rng(4)
    pnl = rng.normal(150.0, 1000.0, size=200)
    p_zero = monte_carlo_skill_pvalue(
        pnl, n_perm=2000, block_size_mean=15.0, benchmark=0.0, seed=5
    )
    p_half = monte_carlo_skill_pvalue(
        pnl, n_perm=2000, block_size_mean=15.0, benchmark=75.0, seed=5
    )
    p_match = monte_carlo_skill_pvalue(
        pnl, n_perm=2000, block_size_mean=15.0, benchmark=150.0, seed=5
    )
    # Strictly monotone — raising the bar must not lower p
    assert p_zero <= p_half <= p_match
    # And the bar at the observed mean is meaningfully different from zero
    assert p_match > p_zero + 0.05
