"""Order Manager — central order lifecycle management."""

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from src.core.clock import now_ist
from src.core.events import Event, EventBus, EventType
from src.core.exceptions import OrderValidationError, RiskLimitBreachError
from src.core.models import Order, OrderRequest
from src.core.types import OrderStatus
from src.oms.executor import OrderExecutor
from src.oms.tracker import OrderTracker
from src.oms.validator import OrderValidator

logger = logging.getLogger(__name__)


class OrderManager:
    """Central order lifecycle manager.

    Flow: OrderRequest -> Validate -> Risk Check -> Execute -> Track -> Fill/Reject

    Every state transition is logged and published as an event.
    """

    def __init__(
        self,
        validator: OrderValidator,
        executor: OrderExecutor,
        tracker: OrderTracker,
        event_bus: EventBus,
        risk_manager=None,  # Injected later to avoid circular imports
        paper_trading: bool = False,
    ):
        self._validator = validator
        self._executor = executor
        self._tracker = tracker
        self._event_bus = event_bus
        self._risk_manager = risk_manager
        self._paper_trading = paper_trading
        self._orders: dict[UUID, Order] = {}
        self._order_history: list[Order] = []
        self._max_history = 5000

    def set_risk_manager(self, risk_manager) -> None:
        """Set risk manager (deferred injection to avoid circular deps)."""
        self._risk_manager = risk_manager

    async def place_order(self, request: OrderRequest) -> Order:
        """Place an order through the full validation -> execution pipeline.

        Returns the Order object with its current status.
        """
        # Create order from request
        order = Order(
            strategy_id=request.strategy_id,
            instrument_token=request.instrument_token,
            tradingsymbol=request.tradingsymbol,
            order_side=request.order_side,
            order_type=request.order_type,
            product=request.product,
            quantity=request.quantity,
            price=request.price,
            trigger_price=request.trigger_price,
            tag=request.tag,
            group_id=request.group_id,
        )

        # Step 1: Validate
        order.status = OrderStatus.VALIDATING
        try:
            self._validator.validate(request)
        except OrderValidationError as e:
            order.status = OrderStatus.REJECTED
            logger.warning(f"Order validation failed: {e}")
            await self._publish_order_event(order, EventType.ORDER_REJECTED, str(e))
            self._order_history.append(order)
            return order

        # Step 2: Margin check (skipped in paper mode — paper broker has no real margins)
        if not self._paper_trading:
            try:
                await self._check_margin(request)
            except OrderValidationError as e:
                order.status = OrderStatus.REJECTED
                logger.warning(f"Margin check failed: {e}")
                await self._publish_order_event(order, EventType.ORDER_REJECTED, str(e))
                self._order_history.append(order)
                return order

        # Step 3: Risk check
        if self._risk_manager:
            try:
                self._risk_manager.validate_order(request)
            except RiskLimitBreachError as e:
                order.status = OrderStatus.REJECTED
                logger.warning(f"Risk check failed: {e}")
                await self._publish_order_event(order, EventType.ORDER_REJECTED, str(e))
                self._order_history.append(order)
                return order

        # Step 4: Execute
        order.status = OrderStatus.SUBMITTED
        order.placed_at = now_ist()
        await self._publish_order_event(order, EventType.ORDER_PLACED)

        result = await self._executor.execute(request)

        if result["status"] == OrderStatus.SUBMITTED:
            order.broker_order_id = result["broker_order_id"]
            order.status = OrderStatus.SUBMITTED
            self._orders[order.id] = order
            self._tracker.track(order)
        else:
            order.status = result["status"]
            await self._publish_order_event(
                order, EventType.ORDER_REJECTED, result.get("message", "")
            )

        self._order_history.append(order)
        if len(self._order_history) > self._max_history:
            self._order_history = self._order_history[-self._max_history:]
        return order

    async def place_multi_leg(self, requests: list[OrderRequest]) -> list[Order]:
        """Place multiple orders as a group (e.g., iron condor = 4 legs).

        All legs share a group_id. Pre-validates all legs before placing any.
        If any leg fails during execution, previously filled legs are reversed.
        """
        import uuid
        group_id = str(uuid.uuid4())[:8]

        # Pre-validate ALL legs before placing any — prevents partial execution
        if self._risk_manager:
            for req in requests:
                try:
                    self._validator.validate(req)
                    self._risk_manager.validate_order(req)
                except (OrderValidationError, RiskLimitBreachError) as e:
                    logger.warning(
                        f"Multi-leg pre-validation failed for {req.tradingsymbol}: {e} "
                        f"— rejecting entire group (group: {group_id})"
                    )
                    # Return all legs as REJECTED without placing any
                    rejected_orders = []
                    for r in requests:
                        order = Order(
                            strategy_id=r.strategy_id,
                            instrument_token=r.instrument_token,
                            tradingsymbol=r.tradingsymbol,
                            order_side=r.order_side,
                            order_type=r.order_type,
                            product=r.product,
                            quantity=r.quantity,
                            price=r.price,
                            trigger_price=r.trigger_price,
                            tag=r.tag,
                            group_id=group_id,
                        )
                        order.status = OrderStatus.REJECTED
                        rejected_orders.append(order)
                    return rejected_orders

        orders = []
        failed = False

        for req in requests:
            req.group_id = group_id
            order = await self.place_order(req)
            orders.append(order)

            if order.status in (OrderStatus.REJECTED, OrderStatus.FAILED):
                logger.warning(
                    f"Multi-leg order: leg {req.tradingsymbol} failed "
                    f"(group: {group_id}). Reversing filled legs."
                )
                failed = True
                break

        # Reverse all filled legs if any leg failed — with retries
        if failed:
            from src.core.types import OrderSide

            # Cancel SUBMITTED (not yet filled) orders — don't reverse them
            for o in orders:
                if o.status == OrderStatus.SUBMITTED and o.broker_order_id:
                    try:
                        await self.cancel_order(o.id)
                        logger.info(f"Multi-leg: cancelled pending leg {o.tradingsymbol} (group: {group_id})")
                    except Exception as e:
                        logger.warning(f"Failed to cancel pending leg {o.tradingsymbol}: {e}")

            # Only reverse FILLED orders (actual exposure that needs unwinding)
            orders_to_reverse = [
                o for o in orders
                if o.status == OrderStatus.FILLED
            ]
            if orders_to_reverse:
                logger.critical(
                    f"Multi-leg order {group_id} failed. "
                    f"Reversing {len(orders_to_reverse)} filled legs..."
                )

            for filled_order in orders_to_reverse:
                reverse_side = (
                    OrderSide.BUY if filled_order.order_side == OrderSide.SELL
                    else OrderSide.SELL
                )
                # Use fill_quantity (actual filled amount), not quantity (original request).
                # On partial fills, reversing the original quantity would overshoot.
                reverse_qty = filled_order.fill_quantity or filled_order.quantity
                reverse_req = OrderRequest(
                    strategy_id=filled_order.strategy_id,
                    instrument_token=filled_order.instrument_token,
                    tradingsymbol=filled_order.tradingsymbol,
                    order_side=reverse_side,
                    order_type=filled_order.order_type,
                    product=filled_order.product,
                    quantity=reverse_qty,
                    tag=f"reverse_{group_id}",
                    group_id=group_id,
                )

                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        reversed_order = await self.place_order(reverse_req)
                        if reversed_order.status not in (OrderStatus.REJECTED, OrderStatus.FAILED):
                            logger.info(
                                f"Reversed leg {filled_order.tradingsymbol} "
                                f"(group: {group_id}, attempt {attempt + 1})"
                            )
                            break
                        logger.warning(
                            f"Reversal rejected for {filled_order.tradingsymbol}, "
                            f"attempt {attempt + 1}/{max_retries}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"Reversal attempt {attempt + 1}/{max_retries} failed "
                            f"for {filled_order.tradingsymbol}: {e}"
                        )

                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)
                    else:
                        logger.critical(
                            f"UNHEDGED POSITION: Failed to reverse "
                            f"{filled_order.tradingsymbol} after {max_retries} attempts. "
                            f"Group: {group_id}. Manual intervention REQUIRED!"
                        )
                        await self._event_bus.publish(
                            Event.create(
                                EventType.RISK_BREACH,
                                source="order_manager",
                                message=f"UNHEDGED: Failed to reverse {filled_order.tradingsymbol}",
                                group_id=group_id,
                            )
                        )

        return orders

    async def cancel_order(self, order_id: UUID) -> bool:
        """Cancel an open order."""
        order = self._orders.get(order_id)
        if not order or not order.broker_order_id:
            return False

        try:
            from src.broker.base import BrokerClient
            # Executor has the broker reference
            await self._executor._broker.cancel_order(order.broker_order_id)
            order.status = OrderStatus.CANCELLED
            self._tracker.untrack(order.broker_order_id)
            await self._publish_order_event(order, EventType.ORDER_CANCELLED)
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    async def cancel_all(self, strategy_id: str | None = None) -> int:
        """Cancel all open orders, optionally filtered by strategy."""
        cancelled = 0
        for order_id, order in list(self._orders.items()):
            if strategy_id and order.strategy_id != strategy_id:
                continue
            if order.status in (OrderStatus.SUBMITTED, OrderStatus.OPEN):
                if await self.cancel_order(order_id):
                    cancelled += 1
        return cancelled

    def get_order(self, order_id: UUID) -> Order | None:
        return self._orders.get(order_id)

    def get_open_orders(self, strategy_id: str | None = None) -> list[Order]:
        orders = [
            o for o in self._orders.values()
            if o.status in (OrderStatus.SUBMITTED, OrderStatus.OPEN)
        ]
        if strategy_id:
            orders = [o for o in orders if o.strategy_id == strategy_id]
        return orders

    def get_order_history(
        self, strategy_id: str | None = None, limit: int = 100
    ) -> list[Order]:
        orders = self._order_history
        if strategy_id:
            orders = [o for o in orders if o.strategy_id == strategy_id]
        return orders[-limit:]

    async def _check_margin(self, request: OrderRequest) -> None:
        """Check if sufficient margin is available for the order."""
        broker = self._executor._broker
        try:
            margins = await broker.get_margins()
            available = float(
                margins.get("equity", {}).get("available", {}).get("cash", 0)
            )

            order_margins = await broker.get_order_margins([{
                "tradingsymbol": request.tradingsymbol,
                "exchange": "NFO",
                "transaction_type": request.order_side.value,
                "quantity": request.quantity,
                "order_type": request.order_type.value,
                "product": request.product.value,
                "price": float(request.price),
            }])

            if order_margins:
                required = float(order_margins[0].get("total", 0))
                if required > 0 and required > available:
                    raise OrderValidationError(
                        f"Insufficient margin: required={required:,.0f} "
                        f"available={available:,.0f} for {request.tradingsymbol}"
                    )
        except OrderValidationError:
            raise
        except Exception as e:
            # Fail closed: reject if margin API fails (safety first)
            raise OrderValidationError(
                f"Margin check failed (API error): {e}. "
                f"Cannot verify margin for {request.tradingsymbol}"
            )

    async def _publish_order_event(
        self, order: Order, event_type: EventType, message: str = ""
    ) -> None:
        await self._event_bus.publish(
            Event.create(
                event_type,
                source="order_manager",
                order=order.model_dump(mode="json"),
                message=message,
            )
        )
