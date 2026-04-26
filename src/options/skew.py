"""Parametric IV skew model for option-chain backtests.

Replaces the hard-coded `iv = iv_atm * (1 + 8*m^2 - 3*m)` formula with a
fittable `ParametricSkew` object so the backtest engine can use either a
calibrated daily skew (fit from observed chain IVs) or a NIFTY-typical
default when chain data is thin.

Moneyness convention used throughout:
    m = log(K / S)

- m > 0 -> OTM call side
- m < 0 -> OTM put side

Shape:
    iv(K) = iv_atm * (1 + a*m^2 + b*m)

The default `nifty_typical()` returns `a=8.0, b=-3.0` — exactly matches
the legacy inline formula, so drop-in replacement is a no-op on numerics.
Negative `b` biases OTM puts higher than OTM calls (the classic equity
"skew" shape).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ParametricSkew:
    """IV skew of the form `iv = iv_atm * (1 + a*m^2 + b*m)` where m=log(K/S).

    Attributes:
        a: Quadratic coefficient (smile curvature).
        b: Linear coefficient (skew slope — negative for put-favouring skew).
    """

    a: float
    b: float

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def nifty_typical(cls) -> "ParametricSkew":
        """Default NIFTY-like skew (matches legacy hardcoded formula)."""
        return cls(a=8.0, b=-3.0)

    @classmethod
    def flat(cls) -> "ParametricSkew":
        """Degenerate flat skew — used when fit data is insufficient."""
        return cls(a=0.0, b=0.0)

    @classmethod
    def fit_from_observed(
        cls,
        strikes: "np.ndarray | list[float]",
        ivs: "np.ndarray | list[float]",
        atm_iv: float,
        spot: float,
        *,
        min_points: int = 5,
        moneyness_clip: float = 0.15,
    ) -> "ParametricSkew":
        """Least-squares fit `iv/atm_iv - 1 = a*m^2 + b*m` from observed chain.

        Filters out non-finite IVs, zero strikes, and moneyness beyond
        `moneyness_clip` (wings are noisy and can drag the fit). Falls back
        to `nifty_typical()` if fewer than `min_points` valid observations
        remain or if the linear system is ill-conditioned.

        Args:
            strikes: Strike prices.
            ivs: Observed IVs at each strike (same order as `strikes`).
            atm_iv: ATM IV anchor (usually the forward ATM or spot-ATM IV).
            spot: Spot price.
            min_points: Minimum valid points required to accept the fit.
            moneyness_clip: Drop |m| > this before fitting (noisy wings).

        Returns:
            A fitted ParametricSkew, or `nifty_typical()` on failure.
        """
        if atm_iv is None or atm_iv <= 0 or spot is None or spot <= 0:
            return cls.nifty_typical()

        strikes_arr = np.asarray(strikes, dtype=float)
        ivs_arr = np.asarray(ivs, dtype=float)
        if strikes_arr.shape != ivs_arr.shape:
            return cls.nifty_typical()

        with np.errstate(divide="ignore", invalid="ignore"):
            m = np.log(strikes_arr / spot)
        valid = (
            np.isfinite(m)
            & np.isfinite(ivs_arr)
            & (ivs_arr > 0)
            & (strikes_arr > 0)
            & (np.abs(m) <= moneyness_clip)
        )
        m_v = m[valid]
        iv_v = ivs_arr[valid]

        if m_v.size < min_points:
            return cls.nifty_typical()

        # y = a*m^2 + b*m  where y = iv/atm_iv - 1
        y = iv_v / atm_iv - 1.0
        A = np.column_stack([m_v ** 2, m_v])

        try:
            coefs, *_ = np.linalg.lstsq(A, y, rcond=None)
        except np.linalg.LinAlgError:
            return cls.nifty_typical()

        a, b = float(coefs[0]), float(coefs[1])
        if not (math.isfinite(a) and math.isfinite(b)):
            return cls.nifty_typical()
        return cls(a=a, b=b)

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def apply(self, atm_iv: float, strike: float, spot: float) -> float:
        """Return skew-adjusted IV at `strike` given `atm_iv` and `spot`.

        Mirrors the legacy formula: clamps result >= a small floor so the
        BS pricer never sees sigma<=0 on extreme OTM wings.
        """
        if atm_iv <= 0 or spot <= 0 or strike <= 0:
            return max(atm_iv, 1e-6)
        m = math.log(strike / spot)
        iv = atm_iv * (1.0 + self.a * m * m + self.b * m)
        return max(iv, 1e-6)

    def apply_vec(
        self,
        atm_iv: float,
        strikes: "np.ndarray",
        spot: float,
    ) -> "np.ndarray":
        """Vectorised variant of `apply` for an entire strike array."""
        if atm_iv <= 0 or spot <= 0:
            return np.full_like(np.asarray(strikes, dtype=float), max(atm_iv, 1e-6))
        strikes_arr = np.asarray(strikes, dtype=float)
        safe = strikes_arr > 0
        m = np.where(safe, np.log(np.where(safe, strikes_arr, 1.0) / spot), 0.0)
        iv = atm_iv * (1.0 + self.a * m * m + self.b * m)
        return np.maximum(iv, 1e-6)
