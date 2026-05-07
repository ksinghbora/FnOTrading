"""Tests for orchestrator-child PnL rollup in PositionTracker / PnLCalculator.

May 7 2026 — V5 orchestrator children carry composite strategy_ids like
``orchestrator_1/iron_condor``. Without prefix-match support in
``PositionTracker.get_positions`` and ``PnLCalculator.get_pnl``, querying
the orchestrator's parent id returns an empty list / ₹0 even when the
children have placed (and closed) trades. This bug masked the
orchestrator's true PnL in the first 173-day backtest as ₹0 across 76
fills.

Lock the rollup contract in: a parent-id query MUST include child
positions and child charges.
"""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime

from src.core.models import Order, Position
from src.core.types import OrderSide, OrderStatus, OrderType, ProductType
from src.portfolio.pnl import PnLCalculator
from src.portfolio.positions import PositionTracker


def _make_fill(strategy_id: str, symbol: str, side: OrderSide, qty: int, price: float) -> Order:
    """Build a filled Order suitable for PositionTracker.update_from_fill."""
    return Order(
        broker_order_id=f"O-{symbol}-{side.value}",
        strategy_id=strategy_id,
        instrument_token=hash(symbol) & 0xFFFFFFFF,
        tradingsymbol=symbol,
        order_side=side,
        order_type=OrderType.LIMIT,
        product=ProductType.NRML,
        quantity=qty,
        fill_price=Decimal(str(price)),
        fill_quantity=qty,
        status=OrderStatus.FILLED,
    )


# ─── PositionTracker prefix-match ──────────────────────────────────


def test_get_positions_exact_match_unchanged():
    """Standalone strategies (no '/' in id) keep exact-match behaviour."""
    tracker = PositionTracker()
    tracker.update_from_fill(_make_fill("ic_1", "NIFTY24500CE", OrderSide.SELL, 75, 100.0))
    tracker.update_from_fill(_make_fill("ib_1", "NIFTY24500PE", OrderSide.SELL, 75, 90.0))

    ic_positions = tracker.get_positions("ic_1")
    ib_positions = tracker.get_positions("ib_1")
    assert len(ic_positions) == 1
    assert len(ib_positions) == 1
    assert ic_positions[0].tradingsymbol == "NIFTY24500CE"
    assert ib_positions[0].tradingsymbol == "NIFTY24500PE"


def test_get_positions_orchestrator_parent_rollup():
    """Querying parent id returns child positions too."""
    tracker = PositionTracker()
    # Three children of orchestrator_1 + one unrelated standalone
    tracker.update_from_fill(_make_fill("orchestrator_1/iron_condor", "S1", OrderSide.SELL, 75, 100.0))
    tracker.update_from_fill(_make_fill("orchestrator_1/short_strangle", "S2", OrderSide.SELL, 75, 80.0))
    tracker.update_from_fill(_make_fill("orchestrator_1/iron_butterfly", "S3", OrderSide.SELL, 75, 60.0))
    tracker.update_from_fill(_make_fill("standalone_strategy", "S4", OrderSide.SELL, 75, 40.0))

    # Parent query rolls up all 3 children
    parent_positions = tracker.get_positions("orchestrator_1")
    assert len(parent_positions) == 3
    assert {p.tradingsymbol for p in parent_positions} == {"S1", "S2", "S3"}
    # Standalone unaffected
    assert len(tracker.get_positions("standalone_strategy")) == 1


def test_get_positions_orchestrator_child_id_still_exact_match():
    """Querying a child id directly returns ONLY that child's positions
    — does not roll up siblings."""
    tracker = PositionTracker()
    tracker.update_from_fill(_make_fill("orchestrator_1/iron_condor", "S1", OrderSide.SELL, 75, 100.0))
    tracker.update_from_fill(_make_fill("orchestrator_1/short_strangle", "S2", OrderSide.SELL, 75, 80.0))

    ic_only = tracker.get_positions("orchestrator_1/iron_condor")
    assert len(ic_only) == 1
    assert ic_only[0].tradingsymbol == "S1"


def test_get_positions_no_false_prefix_match():
    """Substring of another id must NOT match. ``orchestrator_1`` should
    NOT pick up positions tracked under ``orchestrator_10/...``."""
    tracker = PositionTracker()
    tracker.update_from_fill(_make_fill("orchestrator_10/iron_condor", "S1", OrderSide.SELL, 75, 100.0))
    tracker.update_from_fill(_make_fill("orchestrator_1/iron_condor", "S2", OrderSide.SELL, 75, 80.0))

    parent_1 = tracker.get_positions("orchestrator_1")
    # Only "orchestrator_1/..." matches, not "orchestrator_10/..."
    assert len(parent_1) == 1
    assert parent_1[0].tradingsymbol == "S2"


# ─── PnLCalculator charges rollup ──────────────────────────────────


def test_get_pnl_rolls_up_child_charges():
    """Charges added to child ids count toward the parent's total."""
    tracker = PositionTracker()
    pnl = PnLCalculator(tracker)

    pnl.add_charges("orchestrator_1/iron_condor", Decimal("12.50"))
    pnl.add_charges("orchestrator_1/short_strangle", Decimal("8.25"))
    pnl.add_charges("standalone_strategy", Decimal("99.00"))

    parent = pnl.get_pnl("orchestrator_1")
    assert parent.charges == Decimal("20.75")  # 12.50 + 8.25
    # Standalone unaffected
    assert pnl.get_pnl("standalone_strategy").charges == Decimal("99.00")


def test_get_pnl_child_query_returns_only_that_child():
    """Direct child query is exact-match for charges too."""
    tracker = PositionTracker()
    pnl = PnLCalculator(tracker)
    pnl.add_charges("orchestrator_1/iron_condor", Decimal("12.50"))
    pnl.add_charges("orchestrator_1/short_strangle", Decimal("8.25"))

    ic = pnl.get_pnl("orchestrator_1/iron_condor")
    assert ic.charges == Decimal("12.50")


def test_get_pnl_global_aggregate_unchanged():
    """``get_pnl(None)`` (no strategy_id) sums everything — pre-V5 behavior."""
    tracker = PositionTracker()
    pnl = PnLCalculator(tracker)
    pnl.add_charges("orchestrator_1/iron_condor", Decimal("10"))
    pnl.add_charges("standalone_strategy", Decimal("20"))

    total = pnl.get_pnl()
    assert total.charges == Decimal("30")


def test_get_pnl_orchestrator_with_realized_trades():
    """End-to-end: parent's realized PnL aggregates across children."""
    tracker = PositionTracker()
    pnl = PnLCalculator(tracker)

    # Child 1: open + close a winning trade
    tracker.update_from_fill(_make_fill("orchestrator_1/iron_condor", "WIN", OrderSide.SELL, 75, 100.0))
    tracker.update_from_fill(_make_fill("orchestrator_1/iron_condor", "WIN", OrderSide.BUY, 75, 80.0))
    # SELL high, BUY back low → +20 × 75 = +1500 realized

    # Child 2: open + close a losing trade
    tracker.update_from_fill(_make_fill("orchestrator_1/short_strangle", "LOSE", OrderSide.SELL, 75, 50.0))
    tracker.update_from_fill(_make_fill("orchestrator_1/short_strangle", "LOSE", OrderSide.BUY, 75, 60.0))
    # SELL low, BUY back high → -10 × 75 = -750 realized

    parent = pnl.get_pnl("orchestrator_1")
    # Combined: +1500 - 750 = +750
    assert parent.realized == Decimal("750.00")
