"""Position tracking — maintains current positions per strategy."""

import logging
from decimal import Decimal

from src.core.models import Order, Position
from src.core.types import OrderSide, ProductType

logger = logging.getLogger(__name__)


class PositionTracker:
    """Tracks open positions across all strategies.

    Positions are keyed by (strategy_id, instrument_token).
    """

    MAX_CLOSED_POSITIONS = 500

    def __init__(self):
        self._positions: dict[tuple[str, int], Position] = {}
        self._closed: dict[tuple[str, int], Position] = {}  # Archived closed positions

    def update_from_fill(self, order: Order) -> Position:
        """Update position based on a filled order."""
        key = (order.strategy_id, order.instrument_token)

        if key not in self._positions:
            self._positions[key] = Position(
                instrument_token=order.instrument_token,
                tradingsymbol=order.tradingsymbol,
                strategy_id=order.strategy_id,
                quantity=0,
                average_price=Decimal("0"),
                product=order.product,
            )

        pos = self._positions[key]
        fill_qty = order.fill_quantity
        fill_price = order.fill_price

        if order.order_side == OrderSide.BUY:
            signed_qty = fill_qty
        else:
            signed_qty = -fill_qty

        old_qty = pos.quantity
        new_qty = old_qty + signed_qty

        if old_qty == 0:
            # New position
            pos.average_price = fill_price
        elif (old_qty > 0 and signed_qty > 0) or (old_qty < 0 and signed_qty < 0):
            # Adding to position — update average price
            total_value = abs(old_qty) * pos.average_price + fill_qty * fill_price
            pos.average_price = total_value / abs(new_qty) if new_qty != 0 else Decimal("0")
        else:
            # Reducing or closing position
            close_qty = min(abs(old_qty), fill_qty)
            if old_qty > 0:
                realized = close_qty * (fill_price - pos.average_price)
            else:
                realized = close_qty * (pos.average_price - fill_price)
            pos.pnl += realized

            if new_qty == 0:
                pos.average_price = Decimal("0")
            elif abs(signed_qty) > abs(old_qty):
                # Position reversed
                pos.average_price = fill_price

        pos.quantity = new_qty

        # Archive closed positions to prevent unbounded memory growth
        if pos.quantity == 0:
            self._closed[key] = pos
            del self._positions[key]
            # Trim closed archive if too large (keep most recent)
            if len(self._closed) > self.MAX_CLOSED_POSITIONS:
                oldest_key = next(iter(self._closed))
                del self._closed[oldest_key]

        logger.info(
            f"Position updated: {pos.tradingsymbol} [{pos.strategy_id}] "
            f"qty={pos.quantity} avg={pos.average_price}"
        )
        return pos

    def update_ltp(self, instrument_token: int, ltp: Decimal) -> None:
        """Update LTP and unrealized P&L for all positions in an instrument."""
        for key, pos in self._positions.items():
            if key[1] == instrument_token:
                pos.ltp = ltp

    def get_position(self, strategy_id: str, instrument_token: int) -> Position | None:
        return self._positions.get((strategy_id, instrument_token))

    def get_positions(self, strategy_id: str | None = None) -> list[Position]:
        all_pos = {**self._positions, **self._closed}
        if strategy_id:
            return [p for (sid, _), p in all_pos.items() if sid == strategy_id]
        return list(all_pos.values())

    def get_open_positions(self, strategy_id: str | None = None) -> list[Position]:
        return [p for p in self.get_positions(strategy_id) if p.quantity != 0]

    def get_net_quantity(self, strategy_id: str, instrument_token: int) -> int:
        pos = self.get_position(strategy_id, instrument_token)
        return pos.quantity if pos else 0
