"""Portfolio Manager — central hub for position tracking and P&L."""

import logging
from decimal import Decimal

from src.broker.base import BrokerClient
from src.core.events import Event, EventBus, EventType
from src.core.models import Order, PnL, Position
from src.portfolio.charges import calculate_charges
from src.portfolio.pnl import PnLCalculator
from src.portfolio.positions import PositionTracker
from src.portfolio.reconciliation import Reconciler

logger = logging.getLogger(__name__)


class PortfolioManager:
    """Central portfolio management — wires positions, P&L, and charges together.

    Listens to ORDER_FILLED events to update positions and calculate charges.
    Listens to TICK events to update mark-to-market P&L.
    """

    def __init__(self, event_bus: EventBus, broker: BrokerClient):
        self._event_bus = event_bus
        self._broker = broker
        self._positions = PositionTracker()
        self._pnl = PnLCalculator(self._positions)
        self._reconciler = Reconciler(self._positions, broker)

        # Subscribe to events
        self._event_bus.subscribe(EventType.ORDER_FILLED, self._on_order_filled)
        self._event_bus.subscribe(EventType.TICK, self._on_tick)

    @property
    def position_tracker(self) -> PositionTracker:
        return self._positions

    @property
    def pnl_calculator(self) -> PnLCalculator:
        return self._pnl

    async def _on_order_filled(self, event: Event) -> None:
        """Handle filled order — update position and calculate charges."""
        order_data = event.payload.get("order")
        if not order_data:
            return

        order = Order(**order_data)

        # Update position
        position = self._positions.update_from_fill(order)

        # Calculate and record charges
        # Determine instrument type from tradingsymbol
        inst_type = "CE" if "CE" in order.tradingsymbol else (
            "PE" if "PE" in order.tradingsymbol else "FUT"
        )
        charges = calculate_charges(
            order.fill_price, order.fill_quantity, order.order_side, inst_type
        )
        self._pnl.add_charges(order.strategy_id, charges.total)

        # Publish position update
        await self._event_bus.publish(
            Event.create(
                EventType.POSITION_UPDATED,
                source="portfolio_manager",
                position=position.model_dump(mode="json"),
                charges=charges.model_dump(mode="json"),
            )
        )

    async def _on_tick(self, event: Event) -> None:
        """Update LTP for open positions on each tick."""
        tick_data = event.payload.get("tick")
        if not tick_data:
            return
        token = tick_data.get("instrument_token")
        ltp = tick_data.get("ltp")
        if token and ltp:
            self._positions.update_ltp(token, Decimal(str(ltp)))

    def get_positions(self, strategy_id: str | None = None) -> list[Position]:
        return self._positions.get_positions(strategy_id)

    def get_open_positions(self, strategy_id: str | None = None) -> list[Position]:
        return self._positions.get_open_positions(strategy_id)

    def get_pnl(self, strategy_id: str | None = None) -> PnL:
        return self._pnl.get_pnl(strategy_id)

    def get_day_pnl(self) -> Decimal:
        return self._pnl.get_day_pnl()

    def get_pnl_curve(self) -> list[dict]:
        return self._pnl.get_pnl_curve()

    async def reconcile(self) -> dict:
        return await self._reconciler.reconcile()

    def snapshot_pnl(self) -> None:
        self._pnl.snapshot_pnl()

    def reset_daily(self) -> None:
        """Reset daily tracking — call at start of each trading day."""
        self._pnl.reset_daily()
        logger.info("Portfolio daily reset completed")
