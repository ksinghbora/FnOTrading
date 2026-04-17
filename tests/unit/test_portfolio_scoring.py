"""Tests for the portfolio scoring functions extracted in #13.

These were inline in portfolio_strategy.py and were never directly tested —
they only got coverage incidentally via integration runs. Moving them to a
module made it natural to lock in the score boundaries here.

Note on PCR: pcr_oi=0 short-circuits the PCR scoring branch (the function
treats 0 as "missing"). Tests that aren't probing PCR set it to 0 to keep
the score arithmetic readable.
"""

from __future__ import annotations

from src.strategy.implementations.portfolio_scoring import (
    score_premium_selling,
    score_trend_following,
)
from src.strategy.indicators import BreakoutSignal


def _bs(direction: str | None = "UP", strength: float = 0.6) -> BreakoutSignal:
    return BreakoutSignal(
        direction=direction, strength=strength,
        breakout_level=24500.0, morning_high=24500.0, morning_low=24400.0,
    )


class TestScorePremiumSellingNakedMode:
    """Naked-selling VIX scoring (default ic_mode=False) — conservative."""

    def test_ideal_vix_window_full_25(self):
        # PCR=0 to skip that branch; only VIX +25 and DTE +10 should score
        score, reasons = score_premium_selling(
            vix=14.0, morning_range_pct=1.0, move_from_open_pct=1.0,
            pcr_oi=0, is_expiry_day=False, dte=5,
        )
        assert score == 35
        assert any("VIX=14.0 ideal" in r for r in reasons)

    def test_high_vix_too_high_scores_only_dte(self):
        # VIX 30 → "too high" branch (no points). PCR 0 (skip). Only DTE +10.
        score, _ = score_premium_selling(
            vix=30.0, morning_range_pct=1.0, move_from_open_pct=1.0,
            pcr_oi=0, is_expiry_day=False, dte=5,
        )
        assert score == 10

    def test_all_gates_pass_max_in(self):
        # VIX 14 +25, range 0.2 +25, move 0.1 +25, PCR 1.0 +15, DTE 5 +10 = 100
        score, _ = score_premium_selling(
            vix=14.0, morning_range_pct=0.2, move_from_open_pct=0.1,
            pcr_oi=1.0, is_expiry_day=False, dte=5,
        )
        assert score == 100

    def test_extreme_pcr_subtracts_ten(self):
        # VIX +25, DTE +10, PCR 2.0 -10 = 25
        score, reasons = score_premium_selling(
            vix=14.0, morning_range_pct=1.0, move_from_open_pct=1.0,
            pcr_oi=2.0, is_expiry_day=False, dte=5,
        )
        assert score == 25
        assert any("extreme" in r for r in reasons)

    def test_neutral_pcr_adds_fifteen(self):
        # PCR 1.0 in 0.8-1.2 → +15
        score, reasons = score_premium_selling(
            vix=14.0, morning_range_pct=1.0, move_from_open_pct=1.0,
            pcr_oi=1.0, is_expiry_day=False, dte=5,
        )
        # +25 + 10 + 15 = 50
        assert score == 50
        assert any("neutral" in r for r in reasons)

    def test_expiry_day_penalty_applied(self):
        score, reasons = score_premium_selling(
            vix=14.0, morning_range_pct=1.0, move_from_open_pct=1.0,
            pcr_oi=0, is_expiry_day=True, dte=0,
        )
        # VIX +25, expiry -10, no DTE bonus on expiry = 15
        assert score == 15
        assert any("expiry_day_penalty" in r for r in reasons)


class TestScorePremiumSellingICMode:
    """ic_mode=True is more generous on VIX — wings cap downside."""

    def test_ic_mode_allows_higher_vix(self):
        # VIX 22 in IC mode: "rich premiums(IC)" +20
        score, reasons = score_premium_selling(
            vix=22.0, morning_range_pct=1.0, move_from_open_pct=1.0,
            pcr_oi=0, is_expiry_day=False, dte=5, ic_mode=True,
        )
        assert score == 30  # +20 (VIX rich) + 10 (DTE)
        assert any("rich premiums(IC)" in r for r in reasons)

    def test_naked_same_vix_scores_lower(self):
        # Same VIX 22, but naked mode → "high(risky naked)" +8
        naked, _ = score_premium_selling(
            vix=22.0, morning_range_pct=1.0, move_from_open_pct=1.0,
            pcr_oi=0, is_expiry_day=False, dte=5, ic_mode=False,
        )
        assert naked == 18  # +8 + 10

    def test_ic_mode_thin_premiums_below_ideal(self):
        score, reasons = score_premium_selling(
            vix=12.5, morning_range_pct=1.0, move_from_open_pct=1.0,
            pcr_oi=0, is_expiry_day=False, dte=5, ic_mode=True,
        )
        assert score == 18  # +8 (thin) + 10 (DTE)
        assert any("thin premiums(IC)" in r for r in reasons)


class TestScoreTrendFollowing:
    def test_no_breakout_returns_zero(self):
        score, reasons = score_trend_following(
            breakout=_bs(direction=None, strength=0.0),
            oi_confirmed=False, trend_duration_minutes=0, vix=15.0,
        )
        assert score == 0
        assert reasons == ["no breakout"]

    def test_strong_confirmed_sustained_max(self):
        score, _ = score_trend_following(
            breakout=_bs(direction="UP", strength=0.6),
            oi_confirmed=True, trend_duration_minutes=45, vix=15.0,
        )
        # +30 strong + 25 OI + 25 sustained + 20 VIX = 100
        assert score == 100

    def test_moderate_breakout_no_oi_early(self):
        score, reasons = score_trend_following(
            breakout=_bs(direction="UP", strength=0.35),
            oi_confirmed=False, trend_duration_minutes=20, vix=12.0,
        )
        # +15 moderate + 12 sustained-early + 8 VIX-low = 35
        assert score == 35
        assert any("moderate" in r for r in reasons)
        assert any("early" in r for r in reasons)

    def test_low_vix_no_bonus(self):
        score, reasons = score_trend_following(
            breakout=_bs(direction="UP", strength=0.6),
            oi_confirmed=True, trend_duration_minutes=45, vix=10.0,
        )
        # +30 + 25 + 25 + 0 = 80
        assert score == 80
        assert not any(r.startswith("VIX=") for r in reasons)
