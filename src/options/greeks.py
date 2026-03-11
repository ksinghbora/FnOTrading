"""Option Greeks computation — analytical closed-form from BSM.

Computes: Delta, Gamma, Theta, Vega, Rho.
Both scalar and vectorized versions for batch computation.
"""

import math

import numpy as np
from scipy.stats import norm

from src.core.models import Greeks


def compute_greeks(
    S: float, K: float, T: float, r: float, sigma: float, option_type: str
) -> Greeks:
    """Compute all Greeks for a single option.

    Args:
        S: Spot price.
        K: Strike price.
        T: Time to expiry (years).
        r: Risk-free rate.
        sigma: Implied volatility.
        option_type: 'CE' or 'PE'.

    Returns:
        Greeks dataclass with delta, gamma, theta, vega, rho.
    """
    if S <= 0 or K <= 0:
        return Greeks()

    if T <= 0 or sigma <= 0:
        # At/past expiry
        if option_type == "CE":
            delta = 1.0 if S > K else 0.0
        else:
            delta = -1.0 if S < K else 0.0
        return Greeks(delta=delta, gamma=0, theta=0, vega=0, rho=0, iv=sigma)

    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    n_d1 = norm.pdf(d1)  # Standard normal PDF at d1

    # ─── Delta ───────────────────────────────────────────────────
    if option_type == "CE":
        delta = norm.cdf(d1)
    else:
        delta = norm.cdf(d1) - 1

    # ─── Gamma (same for calls and puts) ─────────────────────────
    gamma = n_d1 / (S * sigma * sqrt_T)
    gamma = min(gamma, 1.0)  # Cap to prevent explosion near expiry

    # ─── Theta (per day) ─────────────────────────────────────────
    common_theta = -(S * n_d1 * sigma) / (2 * sqrt_T)
    if option_type == "CE":
        theta = common_theta - r * K * math.exp(-r * T) * norm.cdf(d2)
    else:
        theta = common_theta + r * K * math.exp(-r * T) * norm.cdf(-d2)
    theta = theta / 365  # Convert to per-day

    # ─── Vega (per 1% change in IV) ──────────────────────────────
    vega = S * n_d1 * sqrt_T / 100  # Divided by 100 for 1% change

    # ─── Rho (per 1% change in rate) ────────────────────────────
    if option_type == "CE":
        rho = K * T * math.exp(-r * T) * norm.cdf(d2) / 100
    else:
        rho = -K * T * math.exp(-r * T) * norm.cdf(-d2) / 100

    return Greeks(
        delta=round(delta, 4),
        gamma=round(gamma, 6),
        theta=round(theta, 4),
        vega=round(vega, 4),
        rho=round(rho, 4),
        iv=round(sigma, 4),
    )


def compute_greeks_vec(
    S: np.ndarray,
    K: np.ndarray,
    T: np.ndarray,
    r: float,
    sigma: np.ndarray,
    is_call: np.ndarray,
) -> dict[str, np.ndarray]:
    """Vectorized Greeks computation for an entire option chain.

    Args:
        S: Spot prices (broadcast-compatible).
        K: Strike prices array.
        T: Time to expiry array (years).
        r: Risk-free rate (scalar).
        sigma: IV array.
        is_call: Boolean array (True=CE, False=PE).

    Returns:
        Dict with 'delta', 'gamma', 'theta', 'vega', 'rho' arrays.
    """
    T = np.maximum(T, 1e-10)
    sigma = np.maximum(sigma, 1e-10)
    S = np.maximum(S, 1e-10)
    K = np.maximum(K, 1e-10)
    sqrt_T = np.sqrt(T)

    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    n_d1 = norm.pdf(d1)

    # Delta
    delta = np.where(is_call, norm.cdf(d1), norm.cdf(d1) - 1)

    # Gamma
    gamma = n_d1 / (S * sigma * sqrt_T)
    gamma = np.minimum(gamma, 1.0)  # Cap to prevent explosion near expiry

    # Theta (per day)
    common_theta = -(S * n_d1 * sigma) / (2 * sqrt_T)
    theta_ce = common_theta - r * K * np.exp(-r * T) * norm.cdf(d2)
    theta_pe = common_theta + r * K * np.exp(-r * T) * norm.cdf(-d2)
    theta = np.where(is_call, theta_ce, theta_pe) / 365

    # Vega (per 1%)
    vega = S * n_d1 * sqrt_T / 100

    # Rho (per 1%)
    rho_ce = K * T * np.exp(-r * T) * norm.cdf(d2) / 100
    rho_pe = -K * T * np.exp(-r * T) * norm.cdf(-d2) / 100
    rho = np.where(is_call, rho_ce, rho_pe)

    return {
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "rho": rho,
    }
