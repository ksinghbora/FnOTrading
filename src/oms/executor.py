"""Order execution — routes orders to broker with rate limiting and slippage tracking."""

import logging
import time
from decimal import Decimal

from src.broker.base import BrokerClient
from src.core.exceptions import BrokerOrderError, BrokerRateLimitError
from src.core.models import OrderRequest
from src.core.structured_logger import get_structured_logger
from src.core.types import OrderStatus
from src.utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


class OrderExecutor:
    """Handles the actual execution of orders against the broker.

    - Rate limits API calls
    - Tracks slippage and latency
    - Handles execution errors
    """

    def __init__(self, broker: BrokerClient, rate_limiter: RateLimiter | None = None):
        self._broker = broker
        self._rate_limiter = rate_limiter or RateLimiter(rate=5, burst=10)

    async def execute(self, order: OrderRequest) -> dict:
        """Execute an order against the broker.

        Returns:
            Dict with 'broker_order_id', 'status', 'message', 'submit_time_ms'.
        """
        await self._rate_limiter.acquire()

        logger.info(
            f"[SUBMIT] strategy={order.strategy_id} symbol={order.tradingsymbol} "
            f"side={order.order_side.value} qty={order.quantity} "
            f"type={order.order_type.value} price={order.price}"
        )

        t_start = time.monotonic()

        try:
            broker_order_id = await self._broker.place_order(
                tradingsymbol=order.tradingsymbol,
                exchange="NFO",
                side=order.order_side,
                quantity=order.quantity,
                order_type=order.order_type,
                product=order.product,
                price=float(order.price),
                trigger_price=float(order.trigger_price),
                tag=order.tag[:20] if order.tag else "",
            )

            submit_ms = (time.monotonic() - t_start) * 1000

            logger.info(
                f"[SUBMIT_OK] broker_order_id={broker_order_id} "
                f"symbol={order.tradingsymbol} side={order.order_side.value} "
                f"qty={order.quantity} submit_ms={submit_ms:.0f}"
            )

            slog = get_structured_logger()
            slog.log(
                "SUBMIT",
                strategy_id=order.strategy_id,
                symbol=order.tradingsymbol,
                side=order.order_side.value,
                qty=order.quantity,
                order_type=order.order_type.value,
                price=float(order.price),
                broker_order_id=broker_order_id,
                submit_ms=round(submit_ms, 1),
            )

            return {
                "broker_order_id": broker_order_id,
                "status": OrderStatus.SUBMITTED,
                "message": "Order submitted successfully",
                "submit_time_ms": submit_ms,
            }

        except BrokerRateLimitError:
            logger.warning(f"[SUBMIT_FAIL] symbol={order.tradingsymbol} reason=rate_limit")
            return {
                "broker_order_id": "",
                "status": OrderStatus.FAILED,
                "message": "Rate limit exceeded",
            }
        except BrokerOrderError as e:
            logger.error(
                f"[SUBMIT_FAIL] symbol={order.tradingsymbol} "
                f"side={order.order_side.value} reason={e}"
            )
            return {
                "broker_order_id": "",
                "status": OrderStatus.REJECTED,
                "message": str(e),
            }

    async def get_order_status(self, broker_order_id: str) -> dict:
        """Poll broker for current order status."""
        await self._rate_limiter.acquire()
        return await self._broker.get_order_status(broker_order_id)

    def compute_slippage(
        self, expected_price: Decimal, fill_price: Decimal
    ) -> Decimal:
        """Calculate slippage between expected and actual fill price."""
        if expected_price == 0:
            return Decimal("0")
        return fill_price - expected_price
