"""Black-Scholes-Merton option pricing model.

European-style options (Nifty/BankNifty options are European).
Optimized with numpy vectorization for batch pricing.
"""

import math

import numpy as np
from scipy.stats import norm


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


def bs_call_price_vec(
    S: np.ndarray, K: np.ndarray, T: np.ndarray, r: float, sigma: np.ndarray
) -> np.ndarray:
    """Vectorized Black-Scholes call pricing for entire option chain."""
    T = np.maximum(T, 1e-10)  # Avoid division by zero
    sigma = np.maximum(sigma, 1e-10)
    S = np.maximum(S, 1e-10)
    K = np.maximum(K, 1e-10)

    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)

    return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)


def bs_put_price_vec(
    S: np.ndarray, K: np.ndarray, T: np.ndarray, r: float, sigma: np.ndarray
) -> np.ndarray:
    """Vectorized Black-Scholes put pricing."""
    T = np.maximum(T, 1e-10)
    sigma = np.maximum(sigma, 1e-10)
    S = np.maximum(S, 1e-10)
    K = np.maximum(K, 1e-10)

    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)

    return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
