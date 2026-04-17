"""Open order tracking — monitors pending orders for fills/rejections."""

import asyncio
import logging
from datetime import datetime

from src.broker.base import BrokerClient
from src.core.clock import now_ist
from src.core.events import Event, EventBus, EventType
from src.core.models import Order
from src.core.structured_logger import get_structured_logger
from src.core.types import OrderStatus
from src.utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# Kite order statuses that map to our statuses
KITE_STATUS_MAP = {
    "COMPLETE": OrderStatus.FILLED,
    "TRADED": OrderStatus.FILLED,  # Kite alias for COMPLETE
    "REJECTED": OrderStatus.REJECTED,
    "CANCELLED": OrderStatus.CANCELLED,
    "OPEN": OrderStatus.OPEN,
    "TRIGGER PENDING": OrderStatus.OPEN,
    "OPEN PENDING": OrderStatus.SUBMITTED,
    "VALIDATION PENDING": OrderStatus.VALIDATING,
    "PUT ORDER REQ RECEIVED": OrderStatus.SUBMITTED,
    "MODIFY VALIDATION PENDING": OrderStatus.OPEN,
    "MODIFY ORDER REQ RECEIVED": OrderStatus.OPEN,
    "CANCEL ORDER REQ RECEIVED": OrderStatus.OPEN,
    "AMO REQ RECEIVED": OrderStatus.SUBMITTED,  # After-market orders
}


class OrderTracker:
    """Tracks open orders and polls for status updates.

    Runs a background task that periodically checks all pending
    orders and publishes fill/rejection events.
    """

    def __init__(
        self,
        broker: BrokerClient,
        event_bus: EventBus,
        poll_interval: float = 1.0,
    ):
        self._broker = broker
        self._event_bus = event_bus
        self._poll_interval = poll_interval
        self._pending_orders: dict[str, Order] = {}  # broker_order_id -> Order
        self._running = False
        self._task: asyncio.Task | None = None
        self._rate_limiter = RateLimiter(rate=3, burst=5)

    async def start(self) -> None:
        """Start the order tracking loop."""
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("OrderTracker started")

    async def stop(self) -> None:
        """Stop the order tracking loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("OrderTracker stopped")

    def track(self, order: Order) -> None:
        """Add an order to tracking."""
        if order.broker_order_id:
            self._pending_orders[order.broker_order_id] = order

    def untrack(self, broker_order_id: str) -> None:
        """Remove an order from tracking."""
        self._pending_orders.pop(broker_order_id, None)

    @property
    def pending_count(self) -> int:
        return len(self._pending_orders)

    async def _poll_loop(self) -> None:
        """Periodically check all pending orders."""
        while self._running:
            try:
                if self._pending_orders:
                    await self._check_orders()
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in order tracking loop")
                await asyncio.sleep(self._poll_interval)

    async def _check_orders(self) -> None:
        """Check status of all pending orders."""
        await self._rate_limiter.acquire()

        try:
            broker_orders = await self._broker.get_orders()
        except Exception:
            logger.exception("Failed to fetch orders from broker")
            return

        broker_order_map = {str(o.get("order_id", "")): o for o in broker_orders}

        completed = []
        for broker_id, order in self._pending_orders.items():
            broker_data = broker_order_map.get(broker_id)
            if not broker_data:
                continue

            kite_status = broker_data.get("status", "")
            new_status = KITE_STATUS_MAP.get(kite_status, OrderStatus.OPEN)

            if new_status in (OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED):
                order.status = new_status
                if new_status == OrderStatus.FILLED:
                    fill_price = broker_data.get("average_price")
                    fill_qty = broker_data.get("filled_quantity")

                    # Validate fill data — reject if missing or zero
                    if not fill_price or float(fill_price) <= 0:
                        logger.error(
                            f"CRITICAL: Order {broker_id} marked FILLED but fill_price={fill_price}. "
                            f"Keeping in SUBMITTED state for next poll cycle."
                        )
                        order.status = OrderStatus.SUBMITTED
                        continue
                    if not fill_qty or int(fill_qty) <= 0:
                        logger.error(
                            f"CRITICAL: Order {broker_id} marked FILLED but fill_quantity={fill_qty}. "
                            f"Keeping in SUBMITTED state for next poll cycle."
                        )
                        order.status = OrderStatus.SUBMITTED
                        continue

                    from decimal import Decimal
                    order.fill_price = Decimal(str(fill_price))
                    order.fill_quantity = int(fill_qty)
                    order.filled_at = now_ist()

                    # Detect partial fills — warn so strategies can react
                    if order.fill_quantity < order.quantity:
                        logger.warning(
                            f"[PARTIAL_FILL] order_id={broker_id} symbol={order.tradingsymbol} "
                            f"requested={order.quantity} filled={order.fill_quantity} "
                            f"shortfall={order.quantity - order.fill_quantity}"
                        )

                    slippage = float(order.fill_price - order.price) if order.price > 0 else 0.0
                    slippage_pct = (slippage / float(order.price) * 100) if order.price > 0 else 0.0
                    ttf_ms = (
                        (order.filled_at - order.placed_at).total_seconds() * 1000
                        if order.placed_at else 0
                    )
                    logger.info(
                        f"[FILL] order_id={broker_id} symbol={order.tradingsymbol} "
                        f"side={order.order_side.value} qty={order.fill_quantity} "
                        f"fill_price={order.fill_price} slippage={slippage:.2f} "
                        f"slippage_pct={slippage_pct:.2f}% ttf_ms={ttf_ms:.0f}"
                    )

                    slog = get_structured_logger()
                    slog.log(
                        "FILL",
                        broker_order_id=broker_id,
                        strategy_id=order.strategy_id,
                        symbol=order.tradingsymbol,
                        side=order.order_side.value,
                        qty=order.fill_quantity,
                        requested_qty=order.quantity,
                        expected_price=float(order.price),
                        fill_price=float(order.fill_price),
                        slippage=round(slippage, 2),
                        slippage_pct=round(slippage_pct, 4),
                        ttf_ms=round(ttf_ms, 1),
                        partial=(order.fill_quantity < order.quantity),
                    )

                    event_type = EventType.ORDER_FILLED
                elif new_status == OrderStatus.REJECTED:
                    logger.warning(
                        f"[REJECTED] order_id={broker_id} symbol={order.tradingsymbol} "
                        f"reason={broker_data.get('status_message', 'unknown')}"
                    )
                    event_type = EventType.ORDER_REJECTED
                else:
                    logger.info(f"[CANCELLED] order_id={broker_id} symbol={order.tradingsymbol}")
                    event_type = EventType.ORDER_CANCELLED

                await self._event_bus.publish(
                    Event.create(
                        event_type,
                        source="order_tracker",
                        order=order.model_dump(mode="json"),
                        broker_data=broker_data,
                    )
                )
                completed.append(broker_id)

        for broker_id in completed:
            self._pending_orders.pop(broker_id, None)
