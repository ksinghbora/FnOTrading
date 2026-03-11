"""Tests for Black-Scholes pricing and Greeks computation."""

import pytest
from src.options.pricing import bs_call_price, bs_put_price
from src.options.greeks import compute_greeks
from src.options.iv import compute_iv


class TestBlackScholes:
    """Test BS pricing model."""

    def test_call_price_basic(self):
        # NIFTY-like: S=22000, K=22000, T=7/365, r=0.07, sigma=0.15
        price = bs_call_price(22000, 22000, 7 / 365, 0.07, 0.15)
        assert price > 0
        assert price < 500  # ATM weekly option should be reasonable

    def test_put_price_basic(self):
        price = bs_put_price(22000, 22000, 7 / 365, 0.07, 0.15)
        assert price > 0
        assert price < 500

    def test_put_call_parity(self):
        """Put-Call Parity: C - P = S - K*e^(-rT)"""
        import math
        S, K, T, r, sigma = 22000, 22000, 30 / 365, 0.07, 0.18
        call = bs_call_price(S, K, T, r, sigma)
        put = bs_put_price(S, K, T, r, sigma)
        parity = S - K * math.exp(-r * T)
        assert abs((call - put) - parity) < 1  # Within Rs 1

    def test_deep_itm_call(self):
        price = bs_call_price(22000, 20000, 30 / 365, 0.07, 0.18)
        assert price > 2000  # Deep ITM, mostly intrinsic

    def test_deep_otm_call(self):
        price = bs_call_price(22000, 24000, 7 / 365, 0.07, 0.18)
        assert price < 50  # Deep OTM weekly

    def test_expired_call(self):
        # At expiry, value = max(S-K, 0)
        assert bs_call_price(22000, 21000, 0, 0.07, 0.18) == 1000
        assert bs_call_price(22000, 23000, 0, 0.07, 0.18) == 0


class TestGreeks:
    """Test Greeks computation."""

    def test_atm_call_delta(self):
        greeks = compute_greeks(22000, 22000, 30 / 365, 0.07, 0.18, "CE")
        assert 0.45 < greeks.delta < 0.65  # ATM call delta ~0.5

    def test_atm_put_delta(self):
        greeks = compute_greeks(22000, 22000, 30 / 365, 0.07, 0.18, "PE")
        assert -0.65 < greeks.delta < -0.40  # ATM put delta ~-0.5

    def test_gamma_positive(self):
        greeks = compute_greeks(22000, 22000, 30 / 365, 0.07, 0.18, "CE")
        assert greeks.gamma > 0  # Gamma always positive for long options

    def test_theta_negative_for_options(self):
        greeks = compute_greeks(22000, 22000, 30 / 365, 0.07, 0.18, "CE")
        assert greeks.theta < 0  # Time decay

    def test_vega_positive(self):
        greeks = compute_greeks(22000, 22000, 30 / 365, 0.07, 0.18, "CE")
        assert greeks.vega > 0


class TestIVSolver:
    """Test implied volatility solver."""

    def test_round_trip_call(self):
        """Price an option, then recover IV from the price."""
        sigma = 0.18
        price = bs_call_price(22000, 22000, 30 / 365, 0.07, sigma)
        recovered_iv = compute_iv(price, 22000, 22000, 30 / 365, 0.07, "CE")
        assert abs(recovered_iv - sigma) < 0.001

    def test_round_trip_put(self):
        sigma = 0.20
        price = bs_put_price(22000, 22000, 30 / 365, 0.07, sigma)
        recovered_iv = compute_iv(price, 22000, 22000, 30 / 365, 0.07, "PE")
        assert abs(recovered_iv - sigma) < 0.001

    def test_zero_price_returns_none_iv(self):
        iv = compute_iv(0, 22000, 22000, 30 / 365, 0.07, "CE")
        assert iv is None

    def test_deep_otm_iv(self):
        """Deep OTM options should still get a valid IV."""
        price = 5  # Very cheap OTM option
        iv = compute_iv(price, 22000, 24000, 7 / 365, 0.07, "CE")
        assert iv > 0
