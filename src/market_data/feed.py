"""Tick feed manager — manages subscriptions and dispatches ticks."""

import logging
from datetime import datetime
from decimal import Decimal

from src.core.events import Event, EventBus, EventType
from src.core.models import Tick

logger = logging.getLogger(__name__)


class TickFeedManager:
    """Central manager for real-time tick data.

    - Manages instrument subscriptions across strategies
    - Deduplicates subscriptions
    - Caches latest tick per instrument in Redis
    - Dispatches TICK events via EventBus
    """

    def __init__(self, event_bus: EventBus, redis_client=None):
        self._event_bus = event_bus
        self._redis = redis_client
        self._subscriptions: dict[int, set[str]] = {}  # token -> set of strategy_ids
        self._latest_ticks: dict[int, Tick] = {}
        self._running = False
        self._ticker = None  # Optional: Kite ticker for live subscriptions

        # Subscribe to TICK events to cache and forward
        self._event_bus.subscribe(EventType.TICK, self._on_tick)

    def set_ticker(self, ticker) -> None:
        """Set the Kite ticker so new subscriptions are forwarded to it."""
        self._ticker = ticker

    @property
    def latest_ticks(self) -> dict[int, Tick]:
        return self._latest_ticks

    def get_ltp(self, instrument_token: int) -> Decimal | None:
        """Get cached LTP for an instrument."""
        tick = self._latest_ticks.get(instrument_token)
        return tick.ltp if tick else None

    def get_tick(self, instrument_token: int) -> Tick | None:
        """Get the latest cached tick for an instrument."""
        return self._latest_ticks.get(instrument_token)

    def subscribe(self, tokens: list[int], strategy_id: str) -> list[int]:
        """Subscribe to instrument tokens for a strategy.

        Returns list of newly subscribed tokens (not previously tracked).
        """
        new_tokens = []
        for token in tokens:
            if token not in self._subscriptions:
                self._subscriptions[token] = set()
                new_tokens.append(token)
            self._subscriptions[token].add(strategy_id)

        if new_tokens:
            logger.info(
                f"Strategy {strategy_id} subscribed to {len(new_tokens)} new tokens "
                f"(total active: {len(self._subscriptions)})"
            )
            # Forward to Kite ticker if available
            if self._ticker:
                self._ticker.subscribe(new_tokens)
        return new_tokens

    def unsubscribe(self, strategy_id: str) -> list[int]:
        """Unsubscribe all tokens for a strategy.

        Returns list of tokens that are no longer needed by any strategy.
        """
        removed_tokens = []
        tokens_to_check = list(self._subscriptions.keys())

        for token in tokens_to_check:
            self._subscriptions[token].discard(strategy_id)
            if not self._subscriptions[token]:
                del self._subscriptions[token]
                removed_tokens.append(token)

        if removed_tokens:
            logger.info(
                f"Strategy {strategy_id} unsubscribed. "
                f"Removed {len(removed_tokens)} unused tokens"
            )
        return removed_tokens

    def get_subscribed_tokens(self) -> list[int]:
        """Get all currently subscribed instrument tokens."""
        return list(self._subscriptions.keys())

    def get_subscribers(self, token: int) -> set[str]:
        """Get strategy IDs subscribed to a token."""
        return self._subscriptions.get(token, set())

    async def _on_tick(self, event: Event) -> None:
        """Process incoming tick event — cache and update Redis."""
        tick_data = event.payload.get("tick")
        if not tick_data:
            return

        tick = Tick(**tick_data)
        self._latest_ticks[tick.instrument_token] = tick

        # Cache in Redis
        if self._redis:
            try:
                key = f"tick:{tick.instrument_token}"
                await self._redis.hset(key, mapping={
                    "ltp": str(tick.ltp),
                    "bid": str(tick.bid_price),
                    "ask": str(tick.ask_price),
                    "volume": str(tick.volume),
                    "oi": str(tick.oi),
                    "timestamp": tick.timestamp.isoformat(),
                })
                await self._redis.expire(key, 60)  # TTL 60 seconds
            except Exception:
                pass  # Non-critical, log at debug level
