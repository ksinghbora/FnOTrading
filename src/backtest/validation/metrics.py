"""Advanced Sharpe statistics — PSR, DSR, minTRL, PBO, stationary bootstrap.

Pure-math leaf module: no deps on other src.backtest.validation.* modules.
"""

from __future__ import annotations

import math
import sys

import numpy as np
from scipy.stats import norm

# Euler–Mascheroni constant (used in DSR expected-max formula)
_EULER_MASCHERONI = 0.5772156649015329


def _annualized_sharpe(returns: np.ndarray, periods_per_year: int = 252) -> float:
    """Annualized Sharpe: mean(r) / std(r, ddof=1) * sqrt(periods_per_year)."""
    r = np.asarray(returns, dtype=float)
    if r.size < 2:
        return 0.0
    s = r.std(ddof=1)
    if s == 0 or not np.isfinite(s):
        return 0.0
    return float(r.mean() / s * math.sqrt(periods_per_year))


def probabilistic_sharpe_ratio(
    sharpe: float,
    n: int,
    skew: float = 0.0,
    kurt: float = 3.0,
    benchmark: float = 0.0,
) -> float:
    """Bailey & López de Prado 2012 eq.3 — probability observed Sharpe > benchmark."""
    if n < 2:
        return 0.0
    denom_sq = 1.0 - skew * sharpe + ((kurt - 1.0) / 4.0) * (sharpe**2)
    if denom_sq <= 0 or not np.isfinite(denom_sq):
        return 0.0 if sharpe <= benchmark else 1.0
    z = (sharpe - benchmark) * math.sqrt(n - 1) / math.sqrt(denom_sq)
    return float(norm.cdf(z))


def deflated_sharpe_ratio(
    sharpe_hat: float,
    variance_of_trials: float,
    n_trials: int,
    n: int,
    skew: float,
    kurt: float,
) -> float:
    """Bailey 2014 eq.9 — PSR at deflated benchmark accounting for N trials."""
    n_trials = max(int(n_trials), 1)
    var_trials = max(float(variance_of_trials), 0.0)
    if n_trials <= 1:
        sr_star = 0.0
    else:
        maxz = norm.ppf(1.0 - 1.0 / n_trials)
        subz = norm.ppf(1.0 - 1.0 / (n_trials * math.e))
        sr_star = math.sqrt(var_trials) * (
            (1.0 - _EULER_MASCHERONI) * maxz + _EULER_MASCHERONI * subz
        )
    return probabilistic_sharpe_ratio(sharpe_hat, n, skew, kurt, benchmark=sr_star)


def min_track_record_length(
    sharpe: float,
    benchmark: float,
    skew: float,
    kurt: float,
    confidence: float = 0.95,
) -> int:
    """Bailey & López 2012 §3.3 eq.5 — min n for SR_hat distinguishable from benchmark."""
    if sharpe <= benchmark:
        return sys.maxsize
    denom = 1.0 - skew * sharpe + ((kurt - 1.0) / 4.0) * (sharpe**2)
    if denom <= 0 or not np.isfinite(denom):
        return sys.maxsize
    z = norm.ppf(confidence)
    gap = sharpe - benchmark
    if gap <= 0:
        return sys.maxsize
    n_req = 1.0 + denom * (z / gap) ** 2
    if not np.isfinite(n_req) or n_req <= 0:
        return sys.maxsize
    return int(math.ceil(n_req))


def pbo(train_sharpes: np.ndarray, test_sharpes: np.ndarray) -> float:
    """Bailey et al 2015 §3.2 (CSCV) — Probability of Backtest Overfitting; ship if < 0.5."""
    train = np.asarray(train_sharpes, dtype=float)
    test = np.asarray(test_sharpes, dtype=float)
    if train.ndim != 2 or test.ndim != 2 or train.shape != test.shape:
        raise ValueError("train_sharpes and test_sharpes must be same-shape 2D arrays")
    n_splits, n_configs = train.shape
    if n_splits == 0 or n_configs < 2:
        return 0.0
    logits = np.empty(n_splits, dtype=float)
    # Sentinel that sign-preserves the rank-0 / rank-(n-1) edges
    _BIG = 1e9
    for s in range(n_splits):
        c_star = int(np.argmax(train[s, :]))
        # rank 0 = worst, n_configs-1 = best — use stable ordering for ties
        order = np.argsort(test[s, :], kind="stable")
        ranks = np.empty(n_configs, dtype=int)
        ranks[order] = np.arange(n_configs)
        r = int(ranks[c_star])
        if r == 0:
            logits[s] = -_BIG
        elif r == n_configs - 1:
            logits[s] = _BIG
        else:
            # relative rank in (0, 1)
            logits[s] = math.log(r / (n_configs - 1 - r))
    return float(np.mean(logits <= 0.0))


def stationary_bootstrap_sharpe_ci(
    returns: np.ndarray,
    block_size_mean: float = 5.0,
    n_boot: int = 2000,
    confidence: float = 0.95,
    seed: int | None = None,
) -> tuple[float, float, float]:
    """Politis & Romano 1994 stationary bootstrap — annualized Sharpe CI on daily returns."""
    r = np.asarray(returns, dtype=float)
    n = r.size
    if n < 2:
        return (0.0, 0.0, 0.0)
    if block_size_mean <= 1.0:
        p = 1.0
    else:
        p = 1.0 / float(block_size_mean)
    rng = np.random.default_rng(seed)
    sharpes = np.empty(n_boot, dtype=float)
    for b in range(n_boot):
        idx = np.empty(n, dtype=np.int64)
        i = int(rng.integers(0, n))
        idx[0] = i
        # Vectorize restart coin flips and step increments for speed
        restarts = rng.random(n - 1) < p
        starts = rng.integers(0, n, size=n - 1)
        for t in range(1, n):
            if restarts[t - 1]:
                i = int(starts[t - 1])
            else:
                i = (i + 1) % n
            idx[t] = i
        sample = r[idx]
        sharpes[b] = _annualized_sharpe(sample)
    alpha = (1.0 - confidence) / 2.0
    low = float(np.percentile(sharpes, 100.0 * alpha))
    med = float(np.percentile(sharpes, 50.0))
    high = float(np.percentile(sharpes, 100.0 * (1.0 - alpha)))
    return (low, med, high)
