"""Tests for the parametric IV skew module."""

import math

import numpy as np

from src.options.skew import ParametricSkew


class TestNiftyTypical:
    """Default NIFTY-typical skew parameters."""

    def test_coefficients(self):
        s = ParametricSkew.nifty_typical()
        assert s.a == 8.0
        assert s.b == -3.0

    def test_atm_returns_atm_iv(self):
        """At K=S, log-moneyness=0 => iv == atm_iv."""
        s = ParametricSkew.nifty_typical()
        iv = s.apply(atm_iv=0.15, strike=24000, spot=24000)
        assert abs(iv - 0.15) < 1e-9

    def test_put_iv_higher_than_call_at_equal_moneyness(self):
        """Equity skew: OTM puts should be priced with higher IV than OTM
        calls at the same |log-moneyness|."""
        s = ParametricSkew.nifty_typical()
        spot = 24000.0
        atm_iv = 0.15
        # |m|=0.01 -> strikes at spot * exp(±0.01)
        k_put = spot * math.exp(-0.01)
        k_call = spot * math.exp(0.01)
        iv_put = s.apply(atm_iv, k_put, spot)
        iv_call = s.apply(atm_iv, k_call, spot)
        assert iv_put > iv_call, (
            f"skew broken: IV_put({iv_put}) not > IV_call({iv_call})"
        )

    def test_matches_legacy_formula(self):
        """Backward compatibility: must match `iv_atm*(1 + 8m^2 - 3m)`."""
        s = ParametricSkew.nifty_typical()
        spot = 24000.0
        atm_iv = 0.18
        for k in (22000.0, 23000.0, 24000.0, 25000.0, 26000.0):
            m = math.log(k / spot)
            legacy = atm_iv * (1 + 8 * m * m - 3 * m)
            got = s.apply(atm_iv, k, spot)
            assert abs(got - legacy) < 1e-9, f"mismatch at K={k}"

    def test_wings_clamp_to_floor(self):
        """Extreme OTM should not produce negative or near-zero IV."""
        s = ParametricSkew.nifty_typical()
        iv = s.apply(atm_iv=0.15, strike=10_000, spot=24000)
        assert iv >= 1e-6


class TestFitFromObserved:
    """`fit_from_observed` should recover known coefficients."""

    def test_recovers_known_coefficients(self):
        """Synthetic chain with a=8, b=-3, noise σ=0.005 IV points."""
        rng = np.random.default_rng(42)
        spot = 24000.0
        atm_iv = 0.15
        strikes = np.linspace(spot * 0.92, spot * 1.08, 33)
        m = np.log(strikes / spot)
        true_ivs = atm_iv * (1 + 8.0 * m * m - 3.0 * m)
        noise = rng.normal(0, 0.005, size=strikes.size)
        observed = true_ivs + noise

        fitted = ParametricSkew.fit_from_observed(
            strikes=strikes,
            ivs=observed,
            atm_iv=atm_iv,
            spot=spot,
        )
        # With ~σ=0.005 IV noise on 33 strikes, fit should be well inside ±0.5 of each coef.
        assert abs(fitted.a - 8.0) < 1.0, f"a={fitted.a}"
        assert abs(fitted.b - (-3.0)) < 0.5, f"b={fitted.b}"

    def test_fallback_when_too_few_points(self):
        """Fewer than min_points valid IVs -> falls back to nifty_typical."""
        fitted = ParametricSkew.fit_from_observed(
            strikes=[24000, 24100],  # only 2 points
            ivs=[0.15, 0.152],
            atm_iv=0.15,
            spot=24000.0,
            min_points=5,
        )
        assert fitted.a == 8.0 and fitted.b == -3.0

    def test_fallback_on_non_finite_atm_iv(self):
        fitted = ParametricSkew.fit_from_observed(
            strikes=[24000, 24100, 24200, 24300, 24400, 24500],
            ivs=[0.15] * 6,
            atm_iv=0.0,
            spot=24000.0,
        )
        assert fitted.a == 8.0 and fitted.b == -3.0

    def test_clipping_removes_outliers(self):
        """Wings beyond moneyness_clip should be discarded before fit."""
        # Inject a bad point at far-OTM that would wreck the fit if included
        strikes = [24000, 24500, 25000, 23500, 23000, 50000]
        ivs = [0.15, 0.145, 0.148, 0.158, 0.165, 100.0]  # last one corrupt
        fitted = ParametricSkew.fit_from_observed(
            strikes=strikes,
            ivs=ivs,
            atm_iv=0.15,
            spot=24000.0,
            min_points=3,
            moneyness_clip=0.10,
        )
        # Sanity: the corrupted wing must not drag `a` into absurd territory.
        assert math.isfinite(fitted.a) and abs(fitted.a) < 50
        assert math.isfinite(fitted.b) and abs(fitted.b) < 20


class TestApplyVec:
    """Vectorised evaluation matches scalar path."""

    def test_vec_matches_scalar(self):
        s = ParametricSkew.nifty_typical()
        spot = 24000.0
        atm_iv = 0.18
        strikes = np.array([22000, 23000, 24000, 25000, 26000], dtype=float)
        vec = s.apply_vec(atm_iv, strikes, spot)
        for k, v in zip(strikes, vec):
            scalar = s.apply(atm_iv, float(k), spot)
            assert abs(scalar - v) < 1e-12

    def test_vec_handles_zero_strike_safely(self):
        s = ParametricSkew.nifty_typical()
        vec = s.apply_vec(0.15, np.array([0.0, 24000.0]), 24000.0)
        # zero strike -> falls through to atm_iv floor; 24000 -> atm_iv
        assert vec[0] > 0 and math.isfinite(vec[0])
        assert abs(vec[1] - 0.15) < 1e-9
