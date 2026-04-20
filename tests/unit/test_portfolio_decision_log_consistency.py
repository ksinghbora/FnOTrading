"""Decision-log self-consistency: EXIT rows must carry entry context.

Apr 20 chain-vs-trade reconciliation showed the EXIT row in
decisions_2026-04-20.csv had `entry_premium=0.0` and `quantity=0` while
the matching ENTER row had `entry_premium=44.65` and `quantity=75`.
That broke the "one row → full attribution" property the ML pipeline
relies on (without it you have to JOIN ENTER↔EXIT on a fragile
strategy_id+leg+date key).

Root cause: `_exit_premium` and `_exit_trend` only forwarded exit-side
fields to `_build_snapshot`; entry_premium and quantity defaulted to
zero. Fix passes them explicitly. These tests lock the contract so a
future caller can't silently drop them again.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock

from src.strategy.decision_logger import DecisionSnapshot
from src.strategy.implementations.portfolio_strategy import PortfolioStrategy
from src.strategy.params import PortfolioParams


def _make_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.get_spot_price = MagicMock(return_value=Decimal("24380"))
    ctx.get_vix = MagicMock(return_value=18.5)
    ctx.get_ltp = MagicMock(return_value=Decimal("10"))
    ctx.get_positions = MagicMock(return_value=[])
    ctx.clock = MagicMock()
    ctx.clock.now = MagicMock(return_value=datetime(2026, 4, 20, 15, 15))
    ctx._spot_tokens = ()
    return ctx


def _make_strategy() -> PortfolioStrategy:
    s = PortfolioStrategy("portfolio_test", PortfolioParams())
    s.set_context(_make_ctx())
    s._expiry = date(2026, 4, 21)
    # Replace decision logger with a capture mock so we can inspect rows
    s._decision_logger = MagicMock()
    return s


class TestPremiumExitDecisionRow:
    """_exit_premium must populate entry_premium and quantity on the EXIT row."""

    def _setup_open_premium_position(self, s: PortfolioStrategy) -> None:
        """Mark the strategy as in an open IC with known credit + qty."""
        s._prem_entered = True
        s._prem_mode = "iron_condor"
        s._prem_quantity = 75
        s._entry_premium = Decimal("44.65")
        s._prem_entry_time = datetime(2026, 4, 20, 9, 30)
        # Leg state — symbols/tokens just need to be truthy for legs to build
        s._short_ce_token, s._short_ce_symbol = 1001, "NIFTY24750CE"
        s._short_pe_token, s._short_pe_symbol = 1002, "NIFTY24000PE"
        s._long_ce_token, s._long_ce_symbol = 1003, "NIFTY25150CE"
        s._long_pe_token, s._long_pe_symbol = 1004, "NIFTY23600PE"
        s._prem_realized_pnl = 0.0

    def test_exit_row_carries_entry_premium(self):
        s = _make_strategy()
        self._setup_open_premium_position(s)
        # _exit_premium uses _build_snapshot → _decision_logger.log(snap)
        s._exit_premium("Time exit")
        s._decision_logger.log.assert_called_once()
        snap: DecisionSnapshot = s._decision_logger.log.call_args[0][0]
        assert snap.entry_premium == 44.65, (
            f"EXIT row dropped entry_premium (got {snap.entry_premium}, "
            "expected 44.65). ML pipeline can no longer compute decay% "
            "from a single row."
        )

    def test_exit_row_carries_quantity(self):
        s = _make_strategy()
        self._setup_open_premium_position(s)
        s._exit_premium("Time exit")
        snap: DecisionSnapshot = s._decision_logger.log.call_args[0][0]
        assert snap.quantity == 75, (
            f"EXIT row dropped quantity (got {snap.quantity}, expected 75). "
            "outcome_pnl / quantity = P&L per share is no longer derivable."
        )

    def test_exit_row_decision_and_leg(self):
        s = _make_strategy()
        self._setup_open_premium_position(s)
        s._exit_premium("Time exit")
        snap: DecisionSnapshot = s._decision_logger.log.call_args[0][0]
        assert snap.decision == "EXIT"
        assert snap.leg == "PREMIUM"
        assert snap.mode == "iron_condor"
        assert snap.exit_reason == "Time exit"


class TestTrendExitDecisionRow:
    """Mirror coverage for the trend leg."""

    def _setup_open_trend_position(self, s: PortfolioStrategy) -> None:
        s._trend_entered = True
        s._trend_quantity = 75
        s._entry_debit = Decimal("85.0")
        s._max_spread_value = Decimal("400.0")
        s._peak_spread_value = Decimal("85.0")
        s._trend_entry_time = datetime(2026, 4, 20, 10, 0)
        s._trend_buy_token, s._trend_buy_symbol = 2001, "NIFTY24400CE"
        s._trend_sell_token, s._trend_sell_symbol = 2002, "NIFTY24800CE"
        s._trend_direction = "UP"
        s._trend_realized_pnl = 0.0

    def test_trend_exit_row_carries_entry_debit_as_entry_premium(self):
        s = _make_strategy()
        self._setup_open_trend_position(s)
        s._exit_trend("Trend trail: pullback 25%")
        s._decision_logger.log.assert_called_once()
        snap: DecisionSnapshot = s._decision_logger.log.call_args[0][0]
        # For debit spreads, the "entry_premium" column carries entry_debit —
        # column name is mode-agnostic; semantic is "what we paid/received at
        # entry, in per-share units".
        assert snap.entry_premium == 85.0
        assert snap.quantity == 75
        assert snap.leg == "TREND"
        assert snap.mode == "debit_spread"
