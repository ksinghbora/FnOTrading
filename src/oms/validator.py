"""Pre-flight order validation — checks before any order reaches the broker."""

import logging
from decimal import Decimal

from src.core.clock import MarketClock
from src.core.constants import FREEZE_QUANTITIES, LOT_SIZES
from src.core.exceptions import OrderValidationError
from src.core.models import OrderRequest
from src.core.types import OrderType
from src.market_data.feed import TickFeedManager
from src.oms.dedup import OrderDeduplicator

logger = logging.getLogger(__name__)


class OrderValidator:
    """Validates orders before they are sent to the broker.

    Checks:
    - Market is open
    - Valid lot size
    - Quantity within freeze limit
    - Not a duplicate order
    - Price sanity (not too far from LTP)
    """

    def __init__(
        self,
        clock: MarketClock,
        dedup: OrderDeduplicator,
        feed: TickFeedManager,
    ):
        self._clock = clock
        self._dedup = dedup
        self._feed = feed

    def validate(self, order: OrderRequest) -> None:
        """Run all validation checks. Raises OrderValidationError on failure."""
        self._check_market_hours(order)
        self._check_lot_size(order)
        self._check_freeze_quantity(order)
        self._check_limit_price(order)
        self._check_price_sanity(order)
        self._dedup.check(order)

    def _check_market_hours(self, order: OrderRequest) -> None:
        """Ensure market is open."""
        if not self._clock.is_market_open():
            raise OrderValidationError(
                f"Market is closed. Cannot place order for {order.tradingsymbol}"
            )

    def _check_lot_size(self, order: OrderRequest) -> None:
        """Ensure quantity is a valid lot size multiple."""
        # Sort by longest name first so BANKNIFTY matches before NIFTY
        for underlying, lot_size in sorted(LOT_SIZES.items(), key=lambda x: -len(x[0])):
            if order.tradingsymbol.startswith(underlying):
                if order.quantity % lot_size != 0:
                    raise OrderValidationError(
                        f"Quantity {order.quantity} is not a multiple of lot size "
                        f"{lot_size} for {order.tradingsymbol}"
                    )
                return
        # Stock F&O — lot size checked against instrument master (caller's responsibility)

    def _check_freeze_quantity(self, order: OrderRequest) -> None:
        """Ensure order doesn't exceed exchange freeze quantity."""
        for underlying, freeze_qty in sorted(FREEZE_QUANTITIES.items(), key=lambda x: -len(x[0])):
            if order.tradingsymbol.startswith(underlying):
                if order.quantity > freeze_qty:
                    raise OrderValidationError(
                        f"Quantity {order.quantity} exceeds freeze limit "
                        f"{freeze_qty} for {order.tradingsymbol}. "
                        f"Split into multiple orders."
                    )
                return

    def _check_limit_price(self, order: OrderRequest) -> None:
        """Reject LIMIT/SL orders that have no price set."""
        if order.order_type in (OrderType.LIMIT, OrderType.SL) and order.price <= 0:
            raise OrderValidationError(
                f"{order.order_type.value} order for {order.tradingsymbol} "
                f"requires a price > 0, got {order.price}"
            )

    def _check_price_sanity(self, order: OrderRequest) -> None:
        """Fat-finger check: reject if price is too far from LTP."""
        if order.price <= 0:
            return  # Market order or SL-M, no price to check

        ltp = self._feed.get_ltp(order.instrument_token)
        if ltp is None or ltp == 0:
            return  # No LTP available, skip check

        deviation = abs(float(order.price) - float(ltp)) / float(ltp)
        if deviation > 0.10:  # More than 10% away
            raise OrderValidationError(
                f"Price {order.price} is {deviation:.1%} away from LTP {ltp} "
                f"for {order.tradingsymbol}. Possible fat-finger error."
            )
