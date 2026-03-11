"""Implied Volatility solver using Newton-Raphson method.

Fast convergence with Brenner-Subrahmanyam seed, fallback to Brent's method.
"""

import logging
import math

from scipy.optimize import brentq
from scipy.stats import norm

from src.options.pricing import bs_call_price, bs_put_price

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
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        vega = S * norm.pdf(d1) * math.sqrt(T)

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
