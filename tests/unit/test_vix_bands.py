"""Tests for Indian VIX band gating across strategies and regime classifier.

Validates the VIX threshold rebuild (Apr 17): per-strategy entry bands
(strangle 13-16, IC 16-22, straddle 13-15, trend 12-25) and the 4-state
VolRegime classifier (LOW <13, NORMAL 13-16, HIGH 16-20, EXTREME >20).
"""

import pytest

from src.core.constants import VIX_EXTREME, VIX_HIGH, VIX_LOW, VIX_NORMAL
from src.strategy.params import (
    IronCondorParams,
    PortfolioParams,
    ShortStraddleParams,
    ShortStrangleParams,
    TrendDebitSpreadParams,
)
from src.strategy.regime import ActionRegime, VolRegime, _recommend
from src.strategy.scoring import (
    IRON_CONDOR_CONFIG,
    SHORT_STRADDLE_CONFIG,
    SHORT_STRANGLE_CONFIG,
    score_strategy,
)


class TestConstants:
    """The four VIX boundary constants must encode Indian regime breakpoints."""

    def test_vix_low_at_complacency_boundary(self):
        assert VIX_LOW == 13.0

    def test_vix_normal_at_strangle_ceiling(self):
        # 13-16 strangle ideal — top of band defines NORMAL
        assert VIX_NORMAL == 16.0

    def test_vix_high_at_ic_ceiling(self):
        # 16-20 iron condor ideal
        assert VIX_HIGH == 20.0

    def test_vix_extreme_at_no_trade_boundary(self):
        # >25 = event/crash, no premium
        assert VIX_EXTREME == 25.0


class TestStrategyEntryBands:
    """Each strategy must enforce its Indian-calibrated VIX entry band."""

    def test_short_strangle_band_13_to_16(self):
        p = ShortStrangleParams()
        assert p.vix_entry_min == 13.0
        assert p.vix_entry_max == 16.0

    def test_iron_condor_band_16_to_22(self):
        p = IronCondorParams()
        assert p.vix_entry_min == 16.0
        assert p.vix_entry_max == 22.0
        # IC sized down inside the stressed sub-band 20-22
        assert p.vix_reduce_above == 20.0

    def test_short_straddle_band_13_to_15(self):
        # ATM exposure is most fragile — tightest band
        p = ShortStraddleParams()
        assert p.vix_entry_min == 13.0
        assert p.vix_entry_max == 15.0

    def test_trend_debit_spread_band_12_to_25(self):
        # Trend benefits from elevated vol — wider tolerance than premium sellers
        p = TrendDebitSpreadParams()
        assert p.vix_entry_min == 12.0
        assert p.vix_entry_max == 25.0


class TestPortfolioRouterBand:
    """PortfolioParams encodes the simple 3-band router:
       <13 sit-out | [13,16) strangle | [16,22] IC | >22 sit-out.

    The Apr 18 attempt to slice this further into a toxic-gap (20,23)
    + stressed band [23,28] was reverted after the 23-day chain-replay
    A/B showed it made P&L worse. Sample size (n≤4 in the contested
    band) was below what the change required to be statistically
    distinguishable from noise.
    """

    def test_portfolio_strangle_min_at_complacency_boundary(self):
        assert PortfolioParams().strangle_vix_min == 13.0

    def test_portfolio_strangle_max_at_strangle_ceiling(self):
        assert PortfolioParams().strangle_vix_max == 16.0

    def test_portfolio_ic_max_at_event_risk_boundary(self):
        # Above this: no premium leg at all (event risk).
        assert PortfolioParams().ic_vix_max == 22.0


class TestRegimeRecommendations:
    """The 2D regime → strategy table must reflect Indian VIX bands."""

    def test_low_vol_complacency_sits_out_range_bound(self):
        # New: LOW (<13) is complacency — DO NOT sell premium (was 1.5x strangle, now sit_out)
        strat, lots, _ = _recommend(
            VolRegime.LOW, ActionRegime.RANGE_BOUND, confidence=1.0, conflicted=False
        )
        assert strat == "sit_out"
        assert lots == 0.0

    def test_low_vol_complacency_only_trends_at_half_size(self):
        strat, lots, _ = _recommend(
            VolRegime.LOW, ActionRegime.TRENDING, confidence=1.0, conflicted=False
        )
        assert strat == "trend_debit_spread"
        assert lots == 0.5

    def test_normal_vol_range_picks_strangle_full_size(self):
        # 13-16 is ideal strangle band — full size
        strat, lots, _ = _recommend(
            VolRegime.NORMAL, ActionRegime.RANGE_BOUND, confidence=1.0, conflicted=False
        )
        assert strat == "short_strangle"
        assert lots == 1.0

    def test_high_vol_range_picks_iron_condor_full_size(self):
        # 16-20 is ideal IC band — full size (was 0.5x; IC ideal here, not stressed)
        strat, lots, _ = _recommend(
            VolRegime.HIGH, ActionRegime.RANGE_BOUND, confidence=1.0, conflicted=False
        )
        assert strat == "iron_condor"
        assert lots == 1.0

    def test_extreme_vol_range_sizes_ic_down(self):
        # >20 is stressed — IC at half size (vix_entry_max=22 catches above)
        strat, lots, _ = _recommend(
            VolRegime.EXTREME, ActionRegime.RANGE_BOUND, confidence=1.0, conflicted=False
        )
        assert strat == "iron_condor"
        assert lots == 0.5

    def test_high_vol_choppy_uses_iron_condor_not_sit_out(self):
        # Old: HIGH+CHOPPY=sit_out. New: IC handles chop with wings.
        strat, lots, _ = _recommend(
            VolRegime.HIGH, ActionRegime.CHOPPY, confidence=1.0, conflicted=False
        )
        assert strat == "iron_condor"
        assert lots == 0.5


class TestScoringVixBands:
    """Per-strategy scoring bands must give max points only inside the ideal band."""

    def _score_vix(self, config, vix):
        # Hold non-VIX inputs constant to isolate the VIX band contribution
        score, reasons = score_strategy(
            config, vix=vix, morning_range_pct=0.2, move_from_open_pct=0.05,
            pcr_oi=1.0, is_expiry_day=False, dte=3,
        )
        return score, reasons

    def test_strangle_max_points_at_ideal_14_5(self):
        score, reasons = self._score_vix(SHORT_STRANGLE_CONFIG, vix=14.5)
        assert any("ideal" in r for r in reasons)

    def test_strangle_marginal_at_17(self):
        # 16-18 is "marginal" — small points, not max
        score, reasons = self._score_vix(SHORT_STRANGLE_CONFIG, vix=17.0)
        assert any("marginal" in r for r in reasons)

    def test_strangle_too_high_above_18(self):
        # >18 is "too_high(naked)" — strangle should get 0 VIX points
        score, reasons = self._score_vix(SHORT_STRANGLE_CONFIG, vix=19.0)
        assert any("too_high" in r for r in reasons)

    def test_strangle_zero_vix_points_at_complacency(self):
        score, reasons = self._score_vix(SHORT_STRANGLE_CONFIG, vix=12.0)
        assert any("complacency" in r for r in reasons)

    def test_ic_max_points_at_ideal_18(self):
        score, reasons = self._score_vix(IRON_CONDOR_CONFIG, vix=18.0)
        assert any("ideal(IC)" in r for r in reasons)

    def test_ic_no_trade_above_25(self):
        score, reasons = self._score_vix(IRON_CONDOR_CONFIG, vix=27.0)
        assert any("no_trade" in r for r in reasons)

    def test_ic_thin_premium_in_strangle_band(self):
        # 13-16 is strangle's territory — IC scores low (thin premium)
        score, reasons = self._score_vix(IRON_CONDOR_CONFIG, vix=14.0)
        assert any("thin" in r for r in reasons)

    def test_straddle_dangerous_above_17(self):
        score, reasons = self._score_vix(SHORT_STRADDLE_CONFIG, vix=18.0)
        assert any("dangerous" in r for r in reasons)
