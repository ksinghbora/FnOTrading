"""Tests for the Apr 17 charges-visibility fix.

Trader analysis flagged: a +200 paper-P&L day can be a -800 net-of-charges day
once Zerodha brokerage + STT + GST eat the spread. Display must show:
  Gross (pre-charges) AND Net (post-charges) separately, not just net.
"""

from decimal import Decimal
from unittest.mock import MagicMock

from src.core.models import PnL, Position
from src.portfolio.pnl import PnLCalculator


def _pos(strategy: str, token: int, qty: int, avg: str, ltp: str, realized: str = "0") -> Position:
    return Position(
        instrument_token=token,
        tradingsymbol=f"SYM{token}",
        strategy_id=strategy,
        quantity=qty,
        average_price=Decimal(avg),
        ltp=Decimal(ltp),
        pnl=Decimal(realized),
    )


def _calc(positions: list[Position]) -> PnLCalculator:
    tracker = MagicMock()
    tracker.get_positions.return_value = positions
    return PnLCalculator(tracker)


class TestPnLModelHasGross:
    def test_gross_field_defaults_zero(self):
        assert PnL().gross == Decimal("0")

    def test_gross_is_independent_of_net(self):
        p = PnL(gross=Decimal("200"), charges=Decimal("180"), net=Decimal("20"))
        assert p.gross == Decimal("200")
        assert p.net == Decimal("20")
        assert p.charges == Decimal("180")


class TestGrossEqualsRealizedPlusUnrealized:
    def test_only_realized(self):
        calc = _calc([_pos("s1", 1, 0, "100", "0", realized="500")])
        pnl = calc.get_pnl()
        assert pnl.gross == Decimal("500.00")
        assert pnl.realized == Decimal("500.00")
        assert pnl.unrealized == Decimal("0.00")

    def test_only_unrealized_long(self):
        # 75 lot bought @ 100, now 110 → +750 unrealized
        calc = _calc([_pos("s1", 1, 75, "100", "110")])
        pnl = calc.get_pnl()
        assert pnl.unrealized == Decimal("750.00")
        assert pnl.gross == Decimal("750.00")

    def test_only_unrealized_short(self):
        # 75 lot sold @ 100, now 90 → +750 unrealized
        calc = _calc([_pos("s1", 1, -75, "100", "90")])
        pnl = calc.get_pnl()
        assert pnl.unrealized == Decimal("750.00")
        assert pnl.gross == Decimal("750.00")

    def test_realized_plus_unrealized(self):
        positions = [
            _pos("s1", 1, 0, "100", "0", realized="500"),
            _pos("s1", 2, 75, "100", "110"),  # +750
        ]
        calc = _calc(positions)
        pnl = calc.get_pnl()
        assert pnl.gross == Decimal("1250.00")
        assert pnl.net == Decimal("1250.00")  # no charges added


class TestNetEqualsGrossMinusCharges:
    def test_charges_subtracted_from_gross(self):
        calc = _calc([_pos("s1", 1, 75, "100", "110")])  # +750 gross
        calc.add_charges("s1", Decimal("180"))
        pnl = calc.get_pnl("s1")
        assert pnl.gross == Decimal("750.00")
        assert pnl.charges == Decimal("180.00")
        assert pnl.net == Decimal("570.00")

    def test_charges_can_flip_winner_to_loser(self):
        # Apr 17 trader insight: +200 gross → -800 net is real on a small win
        calc = _calc([_pos("s1", 1, 75, "100", "100.0267")])  # ≈ +2 underflow toy
        calc.add_charges("s1", Decimal("1000"))
        pnl = calc.get_pnl("s1")
        assert pnl.net < Decimal("0")
        assert pnl.gross > Decimal("0")  # gross still positive — UI must show both

    def test_aggregate_charges_across_strategies(self):
        positions = [
            _pos("s1", 1, 0, "100", "0", realized="600"),
            _pos("s2", 2, 0, "100", "0", realized="400"),
        ]
        calc = _calc(positions)
        calc.add_charges("s1", Decimal("100"))
        calc.add_charges("s2", Decimal("80"))
        pnl = calc.get_pnl()  # global view
        assert pnl.gross == Decimal("1000.00")
        assert pnl.charges == Decimal("180.00")
        assert pnl.net == Decimal("820.00")


class TestSnapshotIncludesGrossAndCharges:
    def test_snapshot_records_gross_and_charges(self):
        calc = _calc([_pos("s1", 1, 75, "100", "110")])
        calc.add_charges("s1", Decimal("180"))
        calc.snapshot_pnl()
        curve = calc.get_pnl_curve()
        assert len(curve) == 1
        point = curve[0]
        assert point["gross"] == 750.0
        assert point["charges"] == 180.0
        assert point["net"] == 570.0
        assert "timestamp" in point
