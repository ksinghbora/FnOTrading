"""Signal type definitions for strategy communication."""

from src.core.models import Signal, SignalLeg
from src.core.types import OrderSide, OrderType, SignalType


def entry_signal(
    strategy_id: str,
    legs: list[SignalLeg],
    reason: str = "",
) -> Signal:
    """Create an ENTRY signal."""
    return Signal(
        strategy_id=strategy_id,
        signal_type=SignalType.ENTRY,
        legs=legs,
        reason=reason,
    )


def exit_signal(
    strategy_id: str,
    legs: list[SignalLeg],
    reason: str = "",
) -> Signal:
    """Create an EXIT signal."""
    return Signal(
        strategy_id=strategy_id,
        signal_type=SignalType.EXIT,
        legs=legs,
        reason=reason,
    )


def adjust_signal(
    strategy_id: str,
    legs: list[SignalLeg],
    reason: str = "",
) -> Signal:
    """Create an ADJUST signal."""
    return Signal(
        strategy_id=strategy_id,
        signal_type=SignalType.ADJUST,
        legs=legs,
        reason=reason,
    )


def make_leg(
    tradingsymbol: str,
    instrument_token: int,
    side: OrderSide,
    quantity: int,
    order_type: OrderType = OrderType.MARKET,
    price: float = 0,
) -> SignalLeg:
    """Helper to create a signal leg."""
    from decimal import Decimal
    return SignalLeg(
        tradingsymbol=tradingsymbol,
        instrument_token=instrument_token,
        order_side=side,
        quantity=quantity,
        order_type=order_type,
        price=Decimal(str(price)),
    )
