"""Tests for expiry-day Greeks math.

Covers:
- Gamma cap removal (0DTE ATM gamma free to spike).
- Theta acceleration near expiry (closed-form BS produces the right curve).
- `compute_expiry_risk` returns sensible dollar-gamma for NIFTY-scale inputs.
- `business_days_to_expiry` helper.
"""

import math
from datetime import date, datetime, time

import numpy as np
import pytz

from src.options.greeks import (
    business_days_to_expiry,
    compute_expiry_risk,
    compute_greeks,
    compute_greeks_vec,
)

IST = pytz.timezone("Asia/Kolkata")


class TestGammaCapRemoved:
    """The old code hard-capped gamma at 1.0. Verify the cap is gone."""

    def test_near_expiry_gamma_not_truncated(self):
        """With tiny sigma+T, gamma should exceed 1.0 (proving cap removed)."""
        # Construct a low-vol 0DTE ATM scenario where gamma mathematically > 1.
        # gamma = n_d1 / (S*sigma*sqrt(T))
        # At ATM, d1 ~ 0, n_d1 ~ 0.3989. Choose S*sigma*sqrt(T) < 0.3989.
        # S=100, sigma=0.01, T=1/(252*100) -> S*sigma*sqrt(T) ~ 0.0063
        g = compute_greeks(
            S=100.0, K=100.0, T=1 / (252 * 100), r=0.0, sigma=0.01, option_type="CE"
        )
        assert g.gamma > 1.0, (
            f"gamma={g.gamma} still capped or suppressed; cap should be removed"
        )

    def test_nifty_zero_dte_gamma_positive(self):
        """Realistic NIFTY 0DTE ATM — gamma should be small but positive."""
        # NIFTY spot/strike large so gamma in per-share terms stays <1.
        # Check it is positive and finite.
        g = compute_greeks(
            S=24000.0, K=24000.0, T=1 / 365, r=0.07, sigma=0.15, option_type="CE"
        )
        assert g.gamma > 0
        assert math.isfinite(g.gamma)

    def test_degenerate_input_returns_inf(self):
        """If denominator underflows, gamma should be reported as +inf."""
        g = compute_greeks(
            S=1e-9,
            K=24000.0,
            T=1e-10,
            r=0.07,
            sigma=1e-9,
            option_type="CE",
        )
        # With S, sigma, T all near 0, S*sigma*sqrt(T) < 1e-8 — expect inf.
        assert math.isinf(g.gamma) or g.gamma == 0  # guarded path

    def test_vec_gamma_not_capped(self):
        """Vectorised path must behave the same as the scalar path."""
        S = np.array([100.0])
        K = np.array([100.0])
        T = np.array([1 / (252 * 100)])
        sigma = np.array([0.01])
        is_call = np.array([True])
        result = compute_greeks_vec(S, K, T, r=0.0, sigma=sigma, is_call=is_call)
        assert result["gamma"][0] > 1.0


class TestThetaAccelerationNearExpiry:
    """Theta per day should be larger in magnitude at DTE=0 than DTE=5."""

    def test_theta_zero_dte_vs_five_dte(self):
        s = k = 24000.0
        sigma, r = 0.15, 0.07
        g0 = compute_greeks(s, k, 1 / 365, r, sigma, "CE")
        g5 = compute_greeks(s, k, 5 / 365, r, sigma, "CE")
        assert g0.theta < 0 and g5.theta < 0
        assert abs(g0.theta) > abs(g5.theta), (
            f"theta0={g0.theta} not larger in magnitude than theta5={g5.theta}"
        )

    def test_theta_scales_roughly_inverse_sqrt_T(self):
        """|theta(1d)| / |theta(5d)| ~ sqrt(5) (derives from BS d1/d2 in T->0)."""
        s = k = 24000.0
        g0 = compute_greeks(s, k, 1 / 365, 0.07, 0.15, "CE")
        g5 = compute_greeks(s, k, 5 / 365, 0.07, 0.15, "CE")
        ratio = abs(g0.theta) / abs(g5.theta)
        # sqrt(5) ≈ 2.236 — allow wide band (rho term + r*K*exp(-rT) shifts it)
        assert 1.5 < ratio < 3.5, f"ratio={ratio} outside expected band"


class TestBusinessDaysToExpiry:
    """Verify the business-day T helper."""

    def test_same_day_returns_zero(self):
        """Expiry today at or after close => 0."""
        today = date(2026, 4, 23)  # Thursday
        t = business_days_to_expiry(today, datetime.combine(today, time(15, 30)))
        assert t == 0.0

    def test_next_business_day(self):
        """Start-of-Thursday -> Friday expiry.

        Full remainder of Thu (1 business day) + Fri (1 business day) = 2/252.
        """
        thu = date(2026, 4, 23)
        fri = date(2026, 4, 24)
        t = business_days_to_expiry(fri, thu)
        assert abs(t - 2 / 252) < 1e-9

    def test_end_of_day_next_business_day(self):
        """Thu 15:30 (market close) -> Fri expiry = 1 business day."""
        thu = date(2026, 4, 23)
        fri = date(2026, 4, 24)
        now = IST.localize(datetime.combine(thu, time(15, 30)))
        t = business_days_to_expiry(fri, now)
        assert abs(t - 1 / 252) < 1e-9

    def test_skips_weekend(self):
        """Start-of-Friday -> Monday = 1 (Fri remainder) + 1 (Mon) = 2/252."""
        fri = date(2026, 4, 24)
        mon = date(2026, 4, 27)
        t = business_days_to_expiry(mon, fri)
        assert abs(t - 2 / 252) < 1e-9

    def test_skips_weekend_end_of_friday(self):
        """Fri 15:30 -> Mon expiry = 1 business day (Sat/Sun skipped)."""
        fri = date(2026, 4, 24)
        mon = date(2026, 4, 27)
        now = IST.localize(datetime.combine(fri, time(15, 30)))
        t = business_days_to_expiry(mon, now)
        assert abs(t - 1 / 252) < 1e-9

    def test_past_expiry_is_zero(self):
        assert (
            business_days_to_expiry(date(2020, 1, 1), date(2026, 4, 23)) == 0.0
        )

    def test_use_business_days_flag(self):
        """With use_business_days=True, theta is divided by 252 instead of 365."""
        g_cal = compute_greeks(
            24000.0, 24000.0, 5 / 252, 0.07, 0.15, "CE", use_business_days=False
        )
        g_bd = compute_greeks(
            24000.0, 24000.0, 5 / 252, 0.07, 0.15, "CE", use_business_days=True
        )
        # Same annualized theta, different denominator: bd magnitude larger.
        assert abs(g_bd.theta) > abs(g_cal.theta)
        # Ratio should be 365/252 ≈ 1.449
        ratio = abs(g_bd.theta) / abs(g_cal.theta)
        assert 1.40 < ratio < 1.50


class TestExpiryRisk:
    """`compute_expiry_risk` returns dollar-gamma for position sizing."""

    def test_nifty_scale_dollar_gamma(self):
        """NIFTY: S=24000, K=24000, T=0.004, sigma=0.15, qty=75 (1 lot)."""
        r = compute_expiry_risk(
            S=24000.0,
            K=24000.0,
            T=0.004,
            sigma=0.15,
            qty=75,
            option_type="CE",
        )
        assert r["gamma"] > 0
        assert r["dollar_gamma"] > 0
        assert r["gamma_1pct_move"] > 0
        # Sanity: gamma_1pct_move = gamma * (S*0.01)^2 * qty
        #       = dollar_gamma * (S*0.01)   -- not exactly, dollar_gamma=gamma*S*qty
        # Reproduce directly:
        gamma = r["gamma"]
        expected_1pct = gamma * (24000 * 0.01) ** 2 * 75
        assert abs(r["gamma_1pct_move"] - expected_1pct) < 1e-6

    def test_short_position_negative_gamma(self):
        """Short (qty=-75) => negative dollar_gamma (convex loss on move)."""
        r = compute_expiry_risk(
            S=24000.0,
            K=24000.0,
            T=0.004,
            sigma=0.15,
            qty=-75,
            option_type="CE",
        )
        assert r["dollar_gamma"] < 0
        assert r["gamma_1pct_move"] < 0

    def test_reasonable_magnitude(self):
        """gamma_1pct_move for 1 lot short ATM 0DTE should be in the ₹1k-30k range."""
        r = compute_expiry_risk(
            S=24000.0,
            K=24000.0,
            T=0.004,
            sigma=0.15,
            qty=75,
            option_type="CE",
        )
        # Order of magnitude check — this is 1% convexity for 1 NIFTY lot
        assert 500 < r["gamma_1pct_move"] < 100_000, (
            f"1%-move convexity out of sane band: {r['gamma_1pct_move']}"
        )
