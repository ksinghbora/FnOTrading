"""Tests for expiry-day auto-exercise detection in PortfolioManager.

Validates that closing fills on expiry day at/after 15:25 IST are charged the
higher 0.125% STT on intrinsic value, while normal square-offs use 0.0625% on
fill premium.
"""

from datetime import date, datetime, time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.events import EventBus, EventType
from src.core.models import Order, Position
from src.core.types import OptionType, OrderSide, OrderStatus, OrderType, ProductType
from src.portfolio.manager import PortfolioManager


@pytest.fixture
def event_bus():
    bus = MagicMock(spec=EventBus)
    bus.publish = AsyncMock()
    bus.subscribe = MagicMock()
    return bus


@pytest.fixture
def chain_builder():
    cb = MagicMock()
    cb._token_map = {}
    cb.get_spot_price = MagicMock(return_value=Decimal("0"))
    return cb


@pytest.fixture
def clock():
    c = MagicMock()
    c.is_expiry_day = MagicMock(return_value=False)
    return c


@pytest.fixture
def manager(event_bus, chain_builder, clock):
    return PortfolioManager(
        event_bus=event_bus, broker=MagicMock(),
        chain_builder=chain_builder, clock=clock,
    )


def _make_option_order(
    *, token: int = 12345, symbol: str = "NIFTY26APR22500CE",
    side: OrderSide = OrderSide.SELL, qty: int = 75,
    fill_price: Decimal = Decimal("100"), filled_at: datetime | None = None,
) -> Order:
    return Order(
        broker_order_id="ORD1", strategy_id="test",
        instrument_token=token, tradingsymbol=symbol,
        order_side=side, order_type=OrderType.MARKET, product=ProductType.NRML,
        quantity=qty, fill_quantity=qty, fill_price=fill_price,
        status=OrderStatus.FILLED,
        filled_at=filled_at or datetime(2026, 4, 21, 15, 30),  # Tue 15:30 IST
    )


def _flat_position() -> Position:
    p = MagicMock(spec=Position)
    p.quantity = 0
    p.average_price = Decimal("100")
    return p


class TestExpiryExerciseDetection:

    def test_itm_call_at_close_on_expiry_day_triggers_exercise(
        self, manager, chain_builder, clock
    ):
        clock.is_expiry_day.return_value = True
        chain_builder._token_map[12345] = (
            "NIFTY", date(2026, 4, 21), Decimal("22500"), OptionType.CE,
        )
        chain_builder.get_spot_price.return_value = Decimal("22650")  # 150 ITM

        order = _make_option_order()
        is_ex, intrinsic = manager._detect_expiry_exercise(order, "CE", _flat_position())

        assert is_ex is True
        assert intrinsic == Decimal("150")

    def test_itm_put_at_close_on_expiry_day_triggers_exercise(
        self, manager, chain_builder, clock
    ):
        clock.is_expiry_day.return_value = True
        chain_builder._token_map[99] = (
            "NIFTY", date(2026, 4, 21), Decimal("22500"), OptionType.PE,
        )
        chain_builder.get_spot_price.return_value = Decimal("22300")  # 200 ITM put

        order = _make_option_order(token=99, symbol="NIFTY26APR22500PE")
        is_ex, intrinsic = manager._detect_expiry_exercise(order, "PE", _flat_position())

        assert is_ex is True
        assert intrinsic == Decimal("200")

    def test_otm_at_expiry_does_not_trigger(self, manager, chain_builder, clock):
        clock.is_expiry_day.return_value = True
        chain_builder._token_map[12345] = (
            "NIFTY", date(2026, 4, 21), Decimal("22500"), OptionType.CE,
        )
        chain_builder.get_spot_price.return_value = Decimal("22400")  # OTM call

        order = _make_option_order()
        is_ex, intrinsic = manager._detect_expiry_exercise(order, "CE", _flat_position())

        assert is_ex is False
        assert intrinsic == Decimal("0")

    def test_non_expiry_day_does_not_trigger(self, manager, chain_builder, clock):
        clock.is_expiry_day.return_value = False
        chain_builder._token_map[12345] = (
            "NIFTY", date(2026, 4, 21), Decimal("22500"), OptionType.CE,
        )
        chain_builder.get_spot_price.return_value = Decimal("22650")

        order = _make_option_order()
        is_ex, _ = manager._detect_expiry_exercise(order, "CE", _flat_position())

        assert is_ex is False

    def test_early_fill_on_expiry_day_does_not_trigger(
        self, manager, chain_builder, clock
    ):
        # Same setup but fill at 14:00 — pre-cutoff intraday close, not exercise.
        clock.is_expiry_day.return_value = True
        chain_builder._token_map[12345] = (
            "NIFTY", date(2026, 4, 21), Decimal("22500"), OptionType.CE,
        )
        chain_builder.get_spot_price.return_value = Decimal("22650")

        order = _make_option_order(filled_at=datetime(2026, 4, 21, 14, 0))
        is_ex, _ = manager._detect_expiry_exercise(order, "CE", _flat_position())

        assert is_ex is False

    def test_partial_close_does_not_trigger(self, manager, chain_builder, clock):
        # Position not flat after fill — opening, adding, or partial close.
        clock.is_expiry_day.return_value = True
        chain_builder._token_map[12345] = (
            "NIFTY", date(2026, 4, 21), Decimal("22500"), OptionType.CE,
        )
        chain_builder.get_spot_price.return_value = Decimal("22650")

        pos = MagicMock(spec=Position)
        pos.quantity = -75  # short position still open after partial cover

        order = _make_option_order()
        is_ex, _ = manager._detect_expiry_exercise(order, "CE", pos)

        assert is_ex is False

    def test_futures_never_trigger(self, manager, chain_builder, clock):
        clock.is_expiry_day.return_value = True
        order = _make_option_order(symbol="NIFTY26APRFUT")
        is_ex, _ = manager._detect_expiry_exercise(order, "FUT", _flat_position())
        assert is_ex is False

    def test_unknown_token_does_not_trigger(self, manager, chain_builder, clock):
        clock.is_expiry_day.return_value = True
        # token_map empty — chain_builder doesn't know this option
        order = _make_option_order()
        is_ex, _ = manager._detect_expiry_exercise(order, "CE", _flat_position())
        assert is_ex is False

    def test_no_chain_builder_does_not_trigger(self, event_bus, clock):
        # Manager built without chain_builder (e.g., minimal test rig)
        mgr = PortfolioManager(
            event_bus=event_bus, broker=MagicMock(),
            chain_builder=None, clock=clock,
        )
        clock.is_expiry_day.return_value = True
        order = _make_option_order()
        is_ex, _ = mgr._detect_expiry_exercise(order, "CE", _flat_position())
        assert is_ex is False

    def test_zero_spot_does_not_trigger(self, manager, chain_builder, clock):
        # Defensive: if chain_builder hasn't received a spot tick yet
        clock.is_expiry_day.return_value = True
        chain_builder._token_map[12345] = (
            "NIFTY", date(2026, 4, 21), Decimal("22500"), OptionType.CE,
        )
        chain_builder.get_spot_price.return_value = Decimal("0")

        order = _make_option_order()
        is_ex, _ = manager._detect_expiry_exercise(order, "CE", _flat_position())
        assert is_ex is False
