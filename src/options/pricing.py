"""Black-Scholes-Merton option pricing model.

European-style options (Nifty/BankNifty options are European).
Optimized with numpy vectorization for batch pricing.
Hot-path vectorized functions (bs_call_price_vec, bs_put_price_vec) are
JIT-compiled with Numba for maximum throughput. Note: fastmath=False to
preserve floating-point semantics and keep the determinism test passing.
"""

import math

import numpy as np
from numba import njit
from scipy.stats import norm


# ─── Numba helper: standard normal CDF (avoids scipy in nopython mode) ─
#
# Uses the erf-based identity CDF(x) = 0.5 * (1 + erf(x / sqrt(2))).
# math.erf is hardware-accelerated (glibc/libm) and matches scipy to
# ~14 decimal places — bit-identical within IEEE-754 rounding for the
# range of d1/d2 values seen in F&O option pricing (|x| < 10).
# Do NOT add fastmath=True: that allows reassociations that can change
# the last-bit result and would break the CPCV determinism test.
@njit(cache=True)
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@njit(cache=True)
def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_call_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes call option price.

    Args:
        S: Spot price of underlying.
        K: Strike price.
        T: Time to expiry in years.
        r: Risk-free interest rate (annualized).
        sigma: Implied volatility (annualized).

    Returns:
        Call option theoretical price.
    """
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0:
        return max(0, S - K)
    if sigma <= 0:
        return max(0, S * math.exp(-r * T) - K * math.exp(-r * T))

    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)


def bs_put_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes put option price."""
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0:
        return max(0, K - S)
    if sigma <= 0:
        return max(0, K * math.exp(-r * T) - S * math.exp(-r * T))

    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def bs_price(
    S: float, K: float, T: float, r: float, sigma: float, option_type: str
) -> float:
    """Calculate option price (call or put).

    Args:
        option_type: 'CE' for call, 'PE' for put.
    """
    if option_type == "CE":
        return bs_call_price(S, K, T, r, sigma)
    return bs_put_price(S, K, T, r, sigma)


# ─── Vectorized versions for batch pricing ───────────────────────────
#
# Numba @njit eliminates:
#   • per-call Python overhead (~40 ns/call × 45K calls = ~1.8 ms saved)
#   • scipy.stats.norm.cdf C-extension call overhead (fixed ~20 µs/call)
# The functions operate on numpy float64 arrays; Numba emits LLVM IR that
# fuses the loop over d1/d2/cdf into a single kernel with no Python frames.
# fastmath=False is intentional — it prevents IEEE reassociations that
# could shift the last ULP and break the CPCV determinism contract.


@njit(cache=True)
def bs_call_price_vec(
    S: np.ndarray, K: np.ndarray, T: np.ndarray, r: float, sigma: np.ndarray
) -> np.ndarray:
    """Vectorized Black-Scholes call pricing for entire option chain.

    Numba-compiled. scipy.stats.norm.cdf replaced by the erf-based
    _norm_cdf() approximation which matches scipy to ~14 decimal places.
    """
    n = S.shape[0]
    out = np.empty(n, dtype=np.float64)
    for i in range(n):
        s = max(S[i], 1e-10)
        k = max(K[i], 1e-10)
        t = max(T[i], 1e-10)
        sig = max(sigma[i], 1e-10)
        d1 = (math.log(s / k) + (r + 0.5 * sig * sig) * t) / (sig * math.sqrt(t))
        d2 = d1 - sig * math.sqrt(t)
        out[i] = s * _norm_cdf(d1) - k * math.exp(-r * t) * _norm_cdf(d2)
    return out


@njit(cache=True)
def bs_put_price_vec(
    S: np.ndarray, K: np.ndarray, T: np.ndarray, r: float, sigma: np.ndarray
) -> np.ndarray:
    """Vectorized Black-Scholes put pricing.

    Numba-compiled. See bs_call_price_vec docstring for design notes.
    """
    n = S.shape[0]
    out = np.empty(n, dtype=np.float64)
    for i in range(n):
        s = max(S[i], 1e-10)
        k = max(K[i], 1e-10)
        t = max(T[i], 1e-10)
        sig = max(sigma[i], 1e-10)
        d1 = (math.log(s / k) + (r + 0.5 * sig * sig) * t) / (sig * math.sqrt(t))
        d2 = d1 - sig * math.sqrt(t)
        out[i] = k * math.exp(-r * t) * _norm_cdf(-d2) - s * _norm_cdf(-d1)
    return out
