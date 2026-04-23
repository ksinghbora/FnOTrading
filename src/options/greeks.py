"""Option Greeks computation — analytical closed-form from BSM.

Computes: Delta, Gamma, Theta, Vega, Rho.
Both scalar and vectorized versions for batch computation.
"""

import math
from datetime import date, datetime, time, timedelta

import numpy as np
from scipy.stats import norm

from src.core.models import Greeks

# Business-day conventions
TRADING_DAYS_PER_YEAR = 252
CALENDAR_DAYS_PER_YEAR = 365

# Numerical guard — below this, gamma is effectively unbounded (0DTE at
# expiry). Callers decide how to handle an `inf`/`nan` return.
_GAMMA_DENOM_MIN = 1e-8


def business_days_to_expiry(
    expiry_date: date,
    now: datetime | date | None = None,
) -> float:
    """Return business-day time to expiry in years using 252 trading days.

    Counts weekdays (Mon-Fri) between `now` and `expiry_date` (exclusive of
    today if intraday, inclusive of expiry day). Does NOT honour NSE trading
    holidays — callers that need holiday-accurate T should pass a clock.

    Args:
        expiry_date: Target expiry date.
        now: Reference datetime/date (defaults to today).

    Returns:
        T in years using 252 trading-day convention. Returns 0.0 if expiry
        has already passed.
    """
    if now is None:
        now_date = date.today()
        intraday_frac = 0.0
    elif isinstance(now, datetime):
        now_date = now.date()
        # Fraction of the trading day remaining (9:15 -> 15:30 = 375 min)
        market_open = time(9, 15)
        market_close = time(15, 30)
        t = now.time()
        if t <= market_open:
            intraday_frac = 0.0
        elif t >= market_close:
            intraday_frac = 1.0
        else:
            elapsed = (t.hour * 60 + t.minute) - (9 * 60 + 15)
            intraday_frac = elapsed / 375.0
    else:
        now_date = now
        intraday_frac = 0.0

    if expiry_date <= now_date:
        # Same day expiry or past — treat as 0 unless intraday reference
        if expiry_date == now_date:
            remaining = max(0.0, 1.0 - intraday_frac)
            return remaining / TRADING_DAYS_PER_YEAR
        return 0.0

    # Count business days between now_date (exclusive) and expiry_date (inclusive)
    bdays = 0
    cur = now_date + timedelta(days=1)
    while cur <= expiry_date:
        if cur.weekday() < 5:
            bdays += 1
        cur += timedelta(days=1)

    # Consume today's remaining fraction if intraday
    today_fraction = 0.0
    if now_date.weekday() < 5:
        today_fraction = max(0.0, 1.0 - intraday_frac)

    return (bdays + today_fraction) / TRADING_DAYS_PER_YEAR


def _theta_days_per_year(use_business_days: bool) -> int:
    """Return denominator for converting annualized theta to per-day theta."""
    return TRADING_DAYS_PER_YEAR if use_business_days else CALENDAR_DAYS_PER_YEAR


def compute_greeks(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str,
    use_business_days: bool = False,
) -> Greeks:
    """Compute all Greeks for a single option.

    Args:
        S: Spot price.
        K: Strike price.
        T: Time to expiry in years. Interpreted as calendar-year T when
           `use_business_days=False` (default, backward compatible), or as
           business-day-year T (252 denom) when True.
        r: Risk-free rate.
        sigma: Implied volatility.
        option_type: 'CE' or 'PE'.
        use_business_days: If True, divides annualized theta by 252 instead of
           365. Only affects the theta-per-day conversion; T must be supplied
           in the matching convention by the caller.

    Returns:
        Greeks dataclass with delta, gamma, theta, vega, rho.

    Notes:
        - Gamma is NOT capped. On 0DTE ATM, `S*sigma*sqrt(T)` can approach
          zero; the function returns `float('inf')` if the denominator falls
          below `1e-8`. Callers should decide how to cap/penalise this.
        - Theta acceleration near expiry is encoded in the closed-form BS
          math (emerges from d1/d2 as T->0); no ad-hoc 1/sqrt(T) adjustment.
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
    # No artificial cap — 0DTE ATM gamma genuinely spikes. Guard only the
    # numerical singularity where the denominator underflows.
    denom = S * sigma * sqrt_T
    if denom < _GAMMA_DENOM_MIN:
        gamma = float("inf")
    else:
        gamma = n_d1 / denom

    # ─── Theta (per day) ─────────────────────────────────────────
    common_theta = -(S * n_d1 * sigma) / (2 * sqrt_T)
    if option_type == "CE":
        theta = common_theta - r * K * math.exp(-r * T) * norm.cdf(d2)
    else:
        theta = common_theta + r * K * math.exp(-r * T) * norm.cdf(-d2)
    theta = theta / _theta_days_per_year(use_business_days)

    # ─── Vega (per 1% change in IV) ──────────────────────────────
    vega = S * n_d1 * sqrt_T / 100  # Divided by 100 for 1% change

    # ─── Rho (per 1% change in rate) ────────────────────────────
    if option_type == "CE":
        rho = K * T * math.exp(-r * T) * norm.cdf(d2) / 100
    else:
        rho = -K * T * math.exp(-r * T) * norm.cdf(-d2) / 100

    # Only round finite gamma — inf cannot be rounded.
    gamma_out = gamma if math.isinf(gamma) else round(gamma, 6)

    return Greeks(
        delta=round(delta, 4),
        gamma=gamma_out,
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
    use_business_days: bool = False,
) -> dict[str, np.ndarray]:
    """Vectorized Greeks computation for an entire option chain.

    Args:
        S: Spot prices (broadcast-compatible).
        K: Strike prices array.
        T: Time to expiry array (years).
        r: Risk-free rate (scalar).
        sigma: IV array.
        is_call: Boolean array (True=CE, False=PE).
        use_business_days: If True, divides annualized theta by 252 instead
            of 365.

    Returns:
        Dict with 'delta', 'gamma', 'theta', 'vega', 'rho' arrays. `gamma`
        can contain `np.inf` entries where `S*sigma*sqrt(T)` underflows.
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

    # Gamma — no cap, but mark underflow as +inf
    denom = S * sigma * sqrt_T
    unsafe = denom < _GAMMA_DENOM_MIN
    safe_denom = np.where(unsafe, 1.0, denom)  # avoid /0 warning
    gamma = np.where(unsafe, np.inf, n_d1 / safe_denom)

    # Theta (per day)
    denom_days = _theta_days_per_year(use_business_days)
    common_theta = -(S * n_d1 * sigma) / (2 * sqrt_T)
    theta_ce = common_theta - r * K * np.exp(-r * T) * norm.cdf(d2)
    theta_pe = common_theta + r * K * np.exp(-r * T) * norm.cdf(-d2)
    theta = np.where(is_call, theta_ce, theta_pe) / denom_days

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


def compute_expiry_risk(
    S: float,
    K: float,
    T: float,
    sigma: float,
    qty: int,
    option_type: str,
    r: float = 0.07,
    use_business_days: bool = False,
) -> dict:
    """Compute 1%-spot-move PnL convexity ("dollar gamma") for a position.

    Used by the portfolio risk budget on expiry day, where gamma exposure
    dominates PnL variance and the standard `gamma * qty` summary
    understates risk by a factor of S*spot_move.

    Formula:
        dollar_gamma = gamma * S * qty         # PnL per point of spot move
        gamma_1pct_move = gamma * (S * 0.01)^2 * qty   # 2nd-order PnL for 1%

    Args:
        S: Spot price.
        K: Strike.
        T: Time to expiry (years, same convention as `compute_greeks`).
        sigma: IV.
        qty: Position size (signed: positive=long, negative=short).
        option_type: 'CE' or 'PE'.
        r: Risk-free rate.
        use_business_days: Passed through to `compute_greeks`.

    Returns:
        dict with keys:
          - `gamma`: raw per-share gamma (may be `inf` on degenerate input)
          - `dollar_gamma`: `gamma * S * qty` (linear exposure, PnL/point)
          - `gamma_1pct_move`: `gamma * (S*0.01)^2 * qty` (quadratic 1%-move PnL)
    """
    greeks = compute_greeks(S, K, T, r, sigma, option_type, use_business_days)
    gamma = greeks.gamma
    if math.isinf(gamma):
        return {
            "gamma": float("inf"),
            "dollar_gamma": float("inf") if qty != 0 else 0.0,
            "gamma_1pct_move": float("inf") if qty != 0 else 0.0,
        }
    one_pct = S * 0.01
    return {
        "gamma": gamma,
        "dollar_gamma": gamma * S * qty,
        "gamma_1pct_move": gamma * (one_pct ** 2) * qty,
    }
