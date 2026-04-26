"""Tests for PortfolioGammaBudget (src/risk/portfolio_budget.py).

Covers:
  * can_open gate rejects when aggregate dollar_gamma_1pct would exceed
    the configured capital fraction.
  * Aggregation signs for a short strangle (short = negative position
    quantity contributes negatively to net gamma).
  * Convexity math: 0.5 × Γ × (S × 1%)² at the PnL budget.
"""

from decimal import Decimal

from src.core.models import Greeks, Position
from src.core.types import ProductType
from src.risk.portfolio_budget import (
    GammaBudgetConfig,
    PortfolioGammaBudget,
)


def _make_position(
    symbol: str,
    quantity: int,
    gamma: float,
    tradingsymbol: str | None = None,
) -> Position:
    return Position(
        instrument_token=hash(symbol) & 0xFFFFFF,
        tradingsymbol=tradingsymbol or symbol,
        strategy_id="test",
        quantity=quantity,
        average_price=Decimal("100"),
        ltp=Decimal("100"),
        pnl=Decimal("0"),
        product=ProductType.NRML,
        greeks=Greeks(delta=0.0, gamma=gamma, theta=0.0, vega=0.0),
    )


# ─── Convexity math ─────────────────────────────────────────────────────


class TestConvexityMath:
    def test_dollar_gamma_1pct_formula(self):
        # Γ = 0.001, spot = 25000 → dS = 250 → 0.5 × 0.001 × 250² = 31.25
        budget = PortfolioGammaBudget()
        assert budget._dollar_gamma_1pct(0.001, 25_000.0) == 31.25

    def test_abs_is_used_for_short_book(self):
        # Negative aggregate gamma (short inventory) budgets against the
        # same absolute convexity exposure as long inventory.
        budget = PortfolioGammaBudget()
        assert budget._dollar_gamma_1pct(-0.001, 25_000.0) == 31.25

    def test_zero_spot_zero_pnl(self):
        budget = PortfolioGammaBudget()
        assert budget._dollar_gamma_1pct(0.1, 0.0) == 0.0


# ─── Aggregation ────────────────────────────────────────────────────────


class TestAggregation:
    def test_short_strangle_is_net_short_gamma(self):
        # Short CE + short PE at two strikes = net negative gamma.
        pos_ce = _make_position("NIFTY25100CE", quantity=-75, gamma=0.002)
        pos_pe = _make_position("NIFTY24800PE", quantity=-75, gamma=0.0022)
        budget = PortfolioGammaBudget(
            GammaBudgetConfig(max_gamma_1pct_pnl_pct_of_capital=0.01, capital=1_000_000)
        )
        exposure = budget.current_exposure([pos_ce, pos_pe], spot=25_000.0)

        # Net gamma = 0.002 × -75 + 0.0022 × -75 = -0.315
        assert exposure.total_gamma < 0
        assert abs(exposure.total_gamma) == 0.315
        # Dollar gamma for 1% move: 0.5 × 0.315 × 250² = 9843.75
        assert exposure.dollar_gamma_1pct == 9843.75
        # Budget = 10000 → util ≈ 0.9844
        assert 0.98 < exposure.utilization < 1.0
        assert exposure.breaches == []

    def test_flat_book_zero_exposure(self):
        budget = PortfolioGammaBudget()
        exp = budget.current_exposure([], spot=25_000.0)
        assert exp.total_gamma == 0.0
        assert exp.dollar_gamma_1pct == 0.0
        assert exp.utilization == 0.0


# ─── can_open gate ──────────────────────────────────────────────────────


class TestCanOpen:
    def setup_method(self):
        self.budget = PortfolioGammaBudget(
            GammaBudgetConfig(
                max_gamma_1pct_pnl_pct_of_capital=0.01,  # 10,000 INR budget
                capital=1_000_000,
            )
        )

    def test_rejects_when_sum_exceeds_cap(self):
        # Start at 50% util (5000 of 10000)
        pos = _make_position("X", quantity=-75, gamma=0.00213333333)
        exp = self.budget.current_exposure([pos], spot=25_000.0)
        assert 0.49 < exp.utilization < 0.51

        # Proposing an additional trade that would add 6000 INR dollar_gamma_1pct
        # exceeds the 10000 cap (5000 + 6000 = 11000 > 10000).
        allowed, reason = self.budget.can_open(6000.0, exp)
        assert allowed is False
        assert reason is not None
        assert "projected" in reason
        assert "budget" in reason

    def test_allows_under_budget(self):
        # Small initial position → any reasonable add stays under cap.
        pos = _make_position("X", quantity=-75, gamma=0.0001)
        exp = self.budget.current_exposure([pos], spot=25_000.0)
        allowed, reason = self.budget.can_open(1000.0, exp)
        assert allowed is True
        assert reason is None

    def test_zero_capital_is_no_op(self):
        # Misconfigured budget should not block orders silently.
        budget = PortfolioGammaBudget(
            GammaBudgetConfig(max_gamma_1pct_pnl_pct_of_capital=0.01, capital=0.0)
        )
        allowed, reason = budget.can_open(10_000.0)
        assert allowed is True
        assert reason is None


# ─── Largest-gamma target ───────────────────────────────────────────────


class TestLargestGammaPosition:
    def test_identifies_biggest_contributor(self):
        budget = PortfolioGammaBudget()
        small = _make_position("S", quantity=-75, gamma=0.0001)
        big = _make_position("B", quantity=-75, gamma=0.003)
        mid = _make_position("M", quantity=-75, gamma=0.001)
        target = budget.largest_gamma_position([small, big, mid])
        assert target is not None
        assert target.tradingsymbol == "B"

    def test_empty_returns_none(self):
        budget = PortfolioGammaBudget()
        assert budget.largest_gamma_position([]) is None


# ─── Emergency utilization threshold ────────────────────────────────────


class TestEmergencyThreshold:
    def test_utilization_above_1_2_flags_breach(self):
        # 150% utilization.
        budget = PortfolioGammaBudget(
            GammaBudgetConfig(
                max_gamma_1pct_pnl_pct_of_capital=0.01, capital=1_000_000,
                emergency_utilization=1.2,
            )
        )
        # Need dollar_gamma_1pct = 15000 to hit 1.5x
        # 0.5 × Γ × 250² = 15000 → Γ = 0.48
        pos = _make_position("X", quantity=-75, gamma=0.48 / 75)
        exp = budget.current_exposure([pos], spot=25_000.0)
        assert exp.utilization >= budget.config.emergency_utilization
        assert exp.breaches
