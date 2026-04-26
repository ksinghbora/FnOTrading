"""Implied Volatility solver using Newton-Raphson method.

Fast convergence with Brenner-Subrahmanyam seed, fallback to Brent's method.

Two entry points:
  * ``compute_iv`` — scalar solve for a single option (live path).
  * ``compute_iv_vec`` — batch solve for an entire strike vector at a shared
    (S, T, r). Used by ``GDFLMarketSource.apply`` to collapse 20+ per-strike
    ``scipy.stats.norm.cdf`` round-trips into one vectorized call, yielding a
    large speedup in real-tick backtests (profiling showed `norm.cdf` took
    74% of engine CPU when called scalar-per-strike).
"""

from __future__ import annotations

import logging
import math

import numpy as np
from scipy.optimize import brentq
from scipy.stats import norm

from src.options.pricing import (
    _norm_cdf,
    _norm_pdf,
    bs_call_price,
    bs_call_price_vec,
    bs_put_price,
    bs_put_price_vec,
)

logger = logging.getLogger(__name__)


def compute_iv(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    option_type: str,
    max_iterations: int = 50,
    tolerance: float = 1e-6,
) -> float | None:
    """Calculate implied volatility from market price.

    Uses Newton-Raphson with Brenner-Subrahmanyam initial guess,
    falling back to Brent's method if NR fails to converge.

    Args:
        market_price: Observed option market price.
        S: Spot price of underlying.
        K: Strike price.
        T: Time to expiry (years).
        r: Risk-free rate.
        option_type: 'CE' or 'PE'.
        max_iterations: Max Newton-Raphson iterations.
        tolerance: Convergence tolerance.

    Returns:
        Implied volatility (annualized), or None if unable to solve.
    """
    if market_price <= 0 or T <= 0 or S <= 0 or K <= 0:
        return None

    # Check if price is below intrinsic value (no valid IV)
    if option_type == "CE":
        intrinsic = max(0, S - K * math.exp(-r * T))
    else:
        intrinsic = max(0, K * math.exp(-r * T) - S)

    if market_price < intrinsic * 0.99:  # Allow small tolerance
        return None

    # ─── Brenner-Subrahmanyam initial guess ──────────────────────
    # sigma_approx = sqrt(2*pi/T) * (C/S)
    sigma = math.sqrt(2 * math.pi / T) * (market_price / S)
    sigma = max(0.01, min(sigma, 5.0))  # Clamp to reasonable range

    # ─── Newton-Raphson ──────────────────────────────────────────
    for _ in range(max_iterations):
        if option_type == "CE":
            price = bs_call_price(S, K, T, r, sigma)
        else:
            price = bs_put_price(S, K, T, r, sigma)

        diff = price - market_price

        if abs(diff) < tolerance:
            return sigma

        # Vega (derivative of price w.r.t. sigma)
        # Use the numba-compiled _norm_pdf (erf-based) instead of scipy
        # norm.pdf to avoid the ~20 µs per-call scipy C-extension overhead.
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        vega = S * float(_norm_pdf(d1)) * math.sqrt(T)

        if vega < 1e-12:
            logger.debug(
                f"IV NR: vega<1e-12, falling back to Brent. S={S} K={K} T={T:.4f}"
            )
            break  # Vega too small, NR won't converge

        sigma -= diff / vega
        sigma = max(0.001, min(sigma, 5.0))  # Keep in bounds

    # ─── Fallback: Brent's method ────────────────────────────────
    try:
        if option_type == "CE":
            price_func = lambda s: bs_call_price(S, K, T, r, s) - market_price
        else:
            price_func = lambda s: bs_put_price(S, K, T, r, s) - market_price

        sigma = brentq(price_func, 0.001, 5.0, xtol=tolerance)
        return sigma
    except (ValueError, RuntimeError) as e:
        logger.debug(f"IV Brent failed: {e} for S={S} K={K} T={T:.4f} price={market_price}")
        return None


def compute_iv_batch(
    market_prices: list[float],
    S: float,
    strikes: list[float],
    T: float,
    r: float,
    option_types: list[str],
) -> list[float | None]:
    """Compute IV for a batch of options (e.g., entire option chain)."""
    return [
        compute_iv(price, S, K, T, r, opt_type)
        for price, K, opt_type in zip(market_prices, strikes, option_types)
    ]


# ─── Vectorized Newton-Raphson IV solver ─────────────────────────────
#
# One call replaces ``N`` scalar Newton-Raphson loops at a shared (S, T, r).
# Per-iteration we issue a single ``scipy.stats.norm.cdf`` / ``norm.pdf``
# over length-N arrays instead of N scalar calls — scipy's fixed per-call
# overhead dominates its vectorized compute cost at N>5, and backtests see
# N≈100-200 strikes × 2 sides per minute. Failures (non-converged + below-
# intrinsic prices) fall back to scalar ``compute_iv`` to preserve the
# brentq escape hatch without vectorizing brentq itself.


def compute_iv_vec(
    market_prices: np.ndarray,
    S: float,
    strikes: np.ndarray,
    T: float,
    r: float,
    is_call: np.ndarray,
    max_iterations: int = 50,
    tolerance: float = 1e-6,
) -> np.ndarray:
    """Vectorized Newton-Raphson IV solve for a strike vector sharing (S, T, r).

    Args:
        market_prices: observed LTP per row (shape N).
        S: spot price (scalar, shared across all rows).
        strikes: strike price per row (shape N).
        T: time-to-expiry years (scalar, shared).
        r: risk-free rate.
        is_call: boolean array — True for CE rows, False for PE (shape N).
        max_iterations: Newton-Raphson iteration cap.
        tolerance: price-diff convergence threshold.

    Returns:
        IV array (shape N). ``np.nan`` for rows that fail both NR and the
        scalar brentq fallback; callers replace NaN with their default
        (0.01 in the backtest apply path).
    """
    market_prices = np.asarray(market_prices, dtype=np.float64)
    strikes = np.asarray(strikes, dtype=np.float64)
    is_call = np.asarray(is_call, dtype=bool)
    n = market_prices.shape[0]
    if n == 0:
        return np.empty(0, dtype=np.float64)

    # Intrinsic-value floor: rows below intrinsic have no real IV.
    disc_K = strikes * math.exp(-r * T) if T > 0 else strikes
    intrinsic_ce = np.maximum(0.0, S - disc_K)
    intrinsic_pe = np.maximum(0.0, disc_K - S)
    intrinsic = np.where(is_call, intrinsic_ce, intrinsic_pe)
    below_intrinsic = market_prices < intrinsic * 0.99

    invalid = (
        (market_prices <= 0)
        | (strikes <= 0)
        | below_intrinsic
        | ~np.isfinite(market_prices)
    )
    valid = ~invalid

    # Brenner-Subrahmanyam seed; clamped.
    if T <= 0 or S <= 0:
        return np.full(n, np.nan, dtype=np.float64)
    sigma = np.sqrt(2.0 * math.pi / T) * (market_prices / max(S, 1e-12))
    sigma = np.clip(sigma, 0.01, 5.0)

    # Rows we still need to solve for.
    active = valid.copy()
    converged = np.zeros(n, dtype=bool)
    sqrt_T = math.sqrt(T) if T > 0 else 1e-5

    S_vec = np.full(n, S, dtype=np.float64)
    T_vec = np.full(n, T, dtype=np.float64)

    # Suppress RuntimeWarnings from masked-but-still-evaluated arithmetic
    # inside np.where branches (overflow on deep-OTM rows that we immediately
    # mask out). The mask keeps numerical results correct; the warnings are
    # pure cosmetic noise.
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        for _ in range(max_iterations):
            if not active.any():
                break

            sig = np.where(active, sigma, 1.0)  # dummy sigma for inactive rows
            call_prices = bs_call_price_vec(S_vec, strikes, T_vec, r, sig)
            put_prices = bs_put_price_vec(S_vec, strikes, T_vec, r, sig)
            prices = np.where(is_call, call_prices, put_prices)

            diff = prices - market_prices
            done_now = active & (np.abs(diff) < tolerance)
            converged |= done_now
            active &= ~done_now
            if not active.any():
                break

            d1 = (np.log(S_vec / np.maximum(strikes, 1e-12)) + (r + 0.5 * sig**2) * T_vec) / (
                sig * sqrt_T
            )
            # Vectorized normal PDF via erf-based formula: avoids per-call
            # scipy C-extension overhead. Applied element-wise via numpy
            # ufunc equivalent — mathematically identical to norm.pdf.
            vega = S_vec * (np.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)) * sqrt_T
            too_small = vega < 1e-12
            active &= ~too_small

            safe_vega = np.where(vega > 1e-12, vega, 1.0)
            step = np.where(vega > 1e-12, diff / safe_vega, 0.0)
            sigma = np.where(active, sigma - step, sigma)
            sigma = np.clip(sigma, 0.001, 5.0)

    result = np.where(converged, sigma, np.nan)
    result[invalid] = np.nan

    # Vectorized bisection fallback — one norm.cdf call per iteration over
    # the whole unresolved sub-vector, instead of per-row scalar brentq.
    # Profiling showed scalar brentq dominated runtime at 42k×17 norm.cdf
    # calls ≈ 700k scipy calls; bisection is 40×1 = 40 calls for the entire
    # batch, preserving accuracy for the deep-OTM wings that NR typically
    # misses (prices at or below cabinet-level).
    need_fallback = valid & ~converged
    if need_fallback.any():
        idx = np.where(need_fallback)[0]
        K_sub = strikes[idx]
        P_sub = market_prices[idx]
        is_call_sub = is_call[idx]
        S_sub = np.full(idx.shape[0], S, dtype=np.float64)
        T_sub = np.full(idx.shape[0], T, dtype=np.float64)

        lo = np.full(idx.shape[0], 0.001, dtype=np.float64)
        hi = np.full(idx.shape[0], 5.0, dtype=np.float64)

        # Bracket validity: need f(lo) < 0 < f(hi). If f(hi) is still below
        # the market price, the contract is priced above even the 500% IV
        # ceiling — treat as NaN (caller floors to a small sigma).
        for _ in range(40):
            mid = 0.5 * (lo + hi)
            call_p = bs_call_price_vec(S_sub, K_sub, T_sub, r, mid)
            put_p = bs_put_price_vec(S_sub, K_sub, T_sub, r, mid)
            price_at_mid = np.where(is_call_sub, call_p, put_p)
            under = price_at_mid < P_sub
            lo = np.where(under, mid, lo)
            hi = np.where(under, hi, mid)
            if np.all(hi - lo < tolerance):
                break

        result[idx] = 0.5 * (lo + hi)

    return result
