"""Duplicate order prevention using a sliding time window."""

import hashlib
import logging
import time
from collections import OrderedDict

from src.core.exceptions import DuplicateOrderError
from src.core.models import OrderRequest

logger = logging.getLogger(__name__)


class OrderDeduplicator:
    """Prevents duplicate orders within a configurable time window.

    Uses an in-memory sliding window. Critical safety net for WebSocket
    reconnection scenarios where strategies might re-trigger.
    """

    def __init__(self, window_seconds: float = 5.0):
        self._window = window_seconds
        self._recent: OrderedDict[str, float] = OrderedDict()  # hash -> timestamp

    def check(self, order: OrderRequest) -> None:
        """Check if order is a duplicate. Raises DuplicateOrderError if so."""
        self._cleanup()

        order_hash = self._hash_order(order)
        if order_hash in self._recent:
            last_time = self._recent[order_hash]
            raise DuplicateOrderError(
                f"Duplicate order detected for {order.tradingsymbol} "
                f"({order.order_side.value} {order.quantity}) — "
                f"same order placed {time.time() - last_time:.1f}s ago"
            )

        self._recent[order_hash] = time.time()

    def _hash_order(self, order: OrderRequest) -> str:
        """Create a hash representing the order's identity.

        Includes order_type and price so that a LIMIT order at different
        prices is not flagged as duplicate of a previous order.
        """
        key = (
            f"{order.strategy_id}:{order.instrument_token}:"
            f"{order.order_side.value}:{order.quantity}:"
            f"{order.order_type.value}:{order.price}"
        )
        return hashlib.md5(key.encode()).hexdigest()

    def _cleanup(self) -> None:
        """Remove expired entries from the window."""
        cutoff = time.time() - self._window
        while self._recent:
            oldest_key = next(iter(self._recent))
            if self._recent[oldest_key] < cutoff:
                self._recent.pop(oldest_key)
            else:
                break
