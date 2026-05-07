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
            logger.info(
                f"[REALIZED_PNL] symbol={pos.tradingsymbol} strategy={pos.strategy_id} "
                f"close_qty={close_qty} avg_price={pos.average_price} "
                f"fill_price={fill_price} realized={realized:.2f} cumulative={pos.pnl:.2f}"
            )

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
            logger.info(
                f"[POSITION_CLOSED] symbol={pos.tradingsymbol} strategy={pos.strategy_id} "
                f"total_realized_pnl={pos.pnl:.2f}"
            )
            # Trim closed archive if too large (keep most recent)
            if len(self._closed) > self.MAX_CLOSED_POSITIONS:
                oldest_key = next(iter(self._closed))
                del self._closed[oldest_key]

        logger.info(
            f"[POSITION] symbol={pos.tradingsymbol} strategy={pos.strategy_id} "
            f"old_qty={old_qty} new_qty={pos.quantity} avg={pos.average_price} "
            f"side={order.order_side.value}"
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
        """Return positions for ``strategy_id``.

        Matching rules (May 7 2026 — V5 orchestrator support):
          1. Exact match: positions whose strategy_id equals ``strategy_id``
          2. Orchestrator-child match: positions whose strategy_id starts
             with ``f"{strategy_id}/"`` (e.g. querying ``"orchestrator_1"``
             also returns positions tracked under ``"orchestrator_1/iron_condor"``,
             ``"orchestrator_1/short_strangle"``, etc.)

        The prefix-match path matters because OrchestratorStrategy creates
        children with composite ids like ``f"{self.strategy_id}/{name}"``;
        children's positions/charges are recorded under those composite
        ids, but PnL aggregation typically asks for the parent's id.
        Without prefix matching, ``get_pnl("orchestrator_1")`` returns
        ₹0 even when the children have placed (and closed) trades.
        """
        all_pos = {**self._positions, **self._closed}
        if strategy_id:
            prefix = strategy_id + "/"
            return [
                p for (sid, _), p in all_pos.items()
                if sid == strategy_id or sid.startswith(prefix)
            ]
        return list(all_pos.values())

    def get_open_positions(self, strategy_id: str | None = None) -> list[Position]:
        return [p for p in self.get_positions(strategy_id) if p.quantity != 0]

    def reset_daily(self) -> None:
        """Clear closed positions archive at start of each trading day."""
        self._closed.clear()

    def get_net_quantity(self, strategy_id: str, instrument_token: int) -> int:
        pos = self.get_position(strategy_id, instrument_token)
        return pos.quantity if pos else 0
