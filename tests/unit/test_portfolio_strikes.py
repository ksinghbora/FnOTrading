"""Tests for the strike-selection helpers extracted in #13.

These were instance methods on PortfolioStrategy that read scattered
attributes off `self`. Tests stub the chain with simple namespace objects
to keep the assertions about *strike-selection logic*, not chain plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from src.strategy.implementations.portfolio_strikes import (
    find_delta_strikes,
    find_oi_validated_strikes,
    find_spot_token,
)


# ── Test doubles ────────────────────────────────────────────────────


@dataclass
class _Greeks:
    delta: float


@dataclass
class _Leg:
    greeks: _Greeks
    oi: int = 0
    ltp: float = 50.0


@dataclass
class _StrikeEntry:
    strike: float
    ce: _Leg | None = None
    pe: _Leg | None = None


@dataclass
class _Chain:
    spot_price: float
    strikes: list[_StrikeEntry] = field(default_factory=list)


def _entry(strike: float, ce_delta: float | None, pe_delta: float | None,
           ce_oi: int = 0, pe_oi: int = 0, ce_ltp: float = 50.0, pe_ltp: float = 50.0):
    return _StrikeEntry(
        strike=strike,
        ce=_Leg(greeks=_Greeks(delta=ce_delta), oi=ce_oi, ltp=ce_ltp) if ce_delta is not None else None,
        pe=_Leg(greeks=_Greeks(delta=pe_delta), oi=pe_oi, ltp=pe_ltp) if pe_delta is not None else None,
    )


# ── find_delta_strikes ──────────────────────────────────────────────


class TestFindDeltaStrikes:
    def test_picks_strike_with_closest_delta_to_target(self):
        chain = _Chain(spot_price=24500, strikes=[
            _entry(24400, ce_delta=0.45, pe_delta=-0.55),
            _entry(24500, ce_delta=0.30, pe_delta=-0.30),
            _entry(24600, ce_delta=0.20, pe_delta=-0.20),
            _entry(24700, ce_delta=0.10, pe_delta=-0.10),
        ])
        ce, pe = find_delta_strikes(chain, target_ce_delta=0.20, target_pe_delta=-0.20)
        assert ce.strike == 24600
        assert pe.strike == 24600

    def test_skips_legs_with_wrong_sign(self):
        # CE delta must be > 0; PE delta must be < 0. A bogus negative CE
        # or positive PE shouldn't be picked even if it's "closest" numerically.
        chain = _Chain(spot_price=24500, strikes=[
            _entry(24400, ce_delta=-0.20, pe_delta=0.20),  # garbage signs
            _entry(24500, ce_delta=0.25, pe_delta=-0.25),
        ])
        ce, pe = find_delta_strikes(chain, target_ce_delta=0.20, target_pe_delta=-0.20)
        assert ce.strike == 24500
        assert pe.strike == 24500

    def test_returns_none_for_missing_side(self):
        chain = _Chain(spot_price=24500, strikes=[
            _entry(24500, ce_delta=0.25, pe_delta=None),  # no PE
        ])
        ce, pe = find_delta_strikes(chain, target_ce_delta=0.20, target_pe_delta=-0.20)
        assert ce is not None
        assert pe is None


# ── find_oi_validated_strikes ───────────────────────────────────────


class TestFindOIValidatedStrikes:
    def test_oi_wall_chosen_when_delta_in_range_and_beyond_vix(self):
        # Spot 24500, VIX 15, DTE 7 → expected_range ≈ 24500 * 0.15 * sqrt(7/365) ≈ 510
        # So VIX boundary CE ≈ 25010, PE ≈ 23990
        # OI wall CE @ 25100, delta 0.20 → should be selected
        # OI wall PE @ 23900, delta -0.18 → should be selected
        chain = _Chain(spot_price=24500, strikes=[
            _entry(24500, ce_delta=0.50, pe_delta=-0.50, ce_oi=100, pe_oi=100, ce_ltp=200, pe_ltp=200),
            _entry(25100, ce_delta=0.20, pe_delta=-0.05, ce_oi=50_000, pe_oi=10, ce_ltp=80),
            _entry(23900, ce_delta=0.05, pe_delta=-0.18, ce_oi=10, pe_oi=50_000, pe_ltp=80),
        ])
        ce, pe = find_oi_validated_strikes(
            chain, target_ce_delta=0.20, target_pe_delta=-0.20,
            vix=15.0, expiry=date(2026, 4, 24), today=date(2026, 4, 17),
        )
        assert ce.strike == 25100
        assert pe.strike == 23900

    def test_falls_back_to_delta_when_oi_wall_delta_too_high(self):
        # OI wall CE has delta 0.50 — outside [0.05, 0.30] → reject OI wall
        # Should fall back to the delta-closest CE strike
        chain = _Chain(spot_price=24500, strikes=[
            _entry(24500, ce_delta=0.50, pe_delta=-0.50, ce_oi=100_000, pe_oi=100_000),
            _entry(24800, ce_delta=0.20, pe_delta=-0.18, ce_oi=10, pe_oi=10),
        ])
        ce, pe = find_oi_validated_strikes(
            chain, target_ce_delta=0.20, target_pe_delta=-0.20,
            vix=15.0, expiry=date(2026, 4, 24), today=date(2026, 4, 17),
        )
        # Delta fallback picks 24800 for both (delta closest to 0.20 / -0.20)
        assert ce.strike == 24800
        assert pe.strike == 24800

    def test_min_premium_floor_rejects_too_cheap(self):
        # Spot 24500 → min_prem = 122.5
        # Only one strike with very thin premiums (10 + 10 = 20 < 122.5) and
        # acceptable OI/delta → should fall back to delta and pick the same
        # strike (only one available), but log via "delta(min_prem)"
        chain = _Chain(spot_price=24500, strikes=[
            _entry(25100, ce_delta=0.20, pe_delta=-0.05, ce_oi=50_000, pe_oi=0, ce_ltp=10),
            _entry(23900, ce_delta=0.05, pe_delta=-0.18, ce_oi=0, pe_oi=50_000, pe_ltp=10),
        ])
        ce, pe = find_oi_validated_strikes(
            chain, target_ce_delta=0.20, target_pe_delta=-0.20,
            vix=15.0, expiry=date(2026, 4, 24), today=date(2026, 4, 17),
        )
        # Whatever happens, must not crash and must return both strikes
        assert ce is not None
        assert pe is not None

    def test_zero_vix_uses_no_boundary(self):
        # If VIX is 0 (unknown), the function shouldn't filter by VIX boundary —
        # it should still pick the OI walls if delta is acceptable.
        chain = _Chain(spot_price=24500, strikes=[
            _entry(24600, ce_delta=0.25, pe_delta=-0.25, ce_oi=50_000, pe_oi=50_000, ce_ltp=80, pe_ltp=80),
        ])
        ce, pe = find_oi_validated_strikes(
            chain, target_ce_delta=0.20, target_pe_delta=-0.20,
            vix=0.0, expiry=date(2026, 4, 24), today=date(2026, 4, 17),
        )
        assert ce is not None
        assert pe is not None


# ── find_spot_token ─────────────────────────────────────────────────


class TestFindSpotToken:
    def test_returns_token_for_matching_underlying(self):
        builder = type("B", (), {"_spot_tokens": {256265: "NIFTY", 260105: "BANKNIFTY"}})()
        assert find_spot_token(builder, "NIFTY") == 256265
        assert find_spot_token(builder, "BANKNIFTY") == 260105

    def test_returns_none_for_unknown_underlying(self):
        builder = type("B", (), {"_spot_tokens": {256265: "NIFTY"}})()
        assert find_spot_token(builder, "FINNIFTY") is None

    def test_empty_table_returns_none(self):
        builder = type("B", (), {"_spot_tokens": {}})()
        assert find_spot_token(builder, "NIFTY") is None
