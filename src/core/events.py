"""Event bus — the nervous system of the trading system.

Uses asyncio queues for in-process event dispatch with optional
Redis pub/sub for cross-process communication.
"""

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Coroutine

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    # Market data
    TICK = "tick"
    CANDLE_CLOSED = "candle_closed"

    # Strategy
    SIGNAL = "signal"
    STRATEGY_STARTED = "strategy_started"
    STRATEGY_STOPPED = "strategy_stopped"
    STRATEGY_ERROR = "strategy_error"

    # Orders
    ORDER_PLACED = "order_placed"
    ORDER_FILLED = "order_filled"
    ORDER_REJECTED = "order_rejected"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_UPDATED = "order_updated"

    # Positions
    POSITION_UPDATED = "position_updated"

    # Risk
    RISK_BREACH = "risk_breach"
    CIRCUIT_BREAKER_TRIGGERED = "circuit_breaker_triggered"
    CIRCUIT_BREAKER_RESET = "circuit_breaker_reset"
    KILL_SWITCH = "kill_switch"

    # System
    MARKET_OPEN = "market_open"
    MARKET_CLOSE = "market_close"
    CONNECTION_LOST = "connection_lost"
    CONNECTION_RESTORED = "connection_restored"


class Event(BaseModel):
    """Immutable event flowing through the system."""

    type: EventType
    timestamp: datetime
    source: str
    payload: dict[str, Any] = {}

    @classmethod
    def create(cls, event_type: EventType, source: str, **payload: Any) -> "Event":
        return cls(type=event_type, timestamp=datetime.now(), source=source, payload=payload)


# Type alias for event handlers
EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """Async event bus for decoupled module communication.

    All modules publish/subscribe through this bus instead of
    calling each other directly. This makes testing trivial
    (subscribe a mock handler) and keeps modules decoupled.
    """

    def __init__(self, redis_client: Any | None = None):
        self._handlers: dict[EventType, list[EventHandler]] = defaultdict(list)
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._redis = redis_client

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """Register a handler for an event type."""
        self._handlers[event_type].append(handler)
        logger.debug(f"Subscribed {handler.__qualname__} to {event_type.value}")

    def unsubscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """Remove a handler for an event type."""
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    async def publish(self, event: Event) -> None:
        """Publish an event to all subscribers."""
        await self._queue.put(event)

        # Also publish to Redis for cross-process subscribers
        if self._redis:
            try:
                channel = f"events:{event.type.value}"
                await self._redis.publish(channel, event.model_dump_json())
            except Exception as e:
                logger.warning(f"Failed to publish to Redis: {e}")

    async def start(self) -> None:
        """Start the event dispatch loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._dispatch_loop())
        logger.info("EventBus started")

    async def stop(self) -> None:
        """Stop the event dispatch loop."""
        self._running = False
        if self._task:
            # Put a sentinel to unblock the queue
            await self._queue.put(None)  # type: ignore[arg-type]
            await self._task
            self._task = None
        logger.info("EventBus stopped")

    async def _dispatch_loop(self) -> None:
        """Main dispatch loop — pulls events from queue and calls handlers."""
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if event is None:  # Sentinel for shutdown
                break

            handlers = self._handlers.get(event.type, [])
            if not handlers:
                continue

            # Dispatch to all handlers concurrently
            tasks = []
            for handler in handlers:
                tasks.append(asyncio.create_task(self._safe_handle(handler, event)))

            if tasks:
                await asyncio.gather(*tasks)

    async def _safe_handle(self, handler: EventHandler, event: Event) -> None:
        """Call handler with error protection."""
        try:
            await handler(event)
        except Exception:
            logger.exception(
                f"Error in event handler {handler.__qualname__} for {event.type.value}"
            )
