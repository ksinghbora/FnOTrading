"""Notification Manager — subscribes to EventBus and dispatches alerts.

Listens to key events (order fills, risk breaches, circuit breaker,
kill switch) and routes them through the Telegram notifier.
"""

import asyncio
import logging
from decimal import Decimal

from src.core.events import Event, EventBus, EventType
from src.notifications.telegram import TelegramNotifier
from src.notifications import templates

logger = logging.getLogger(__name__)


class NotificationManager:
    """Central notification dispatcher.

    Subscribes to EventBus events and sends formatted notifications
    through the Telegram channel.
    """

    def __init__(self, event_bus: EventBus, telegram: TelegramNotifier):
        self._event_bus = event_bus
        self._telegram = telegram

        # Subscribe to events
        self._event_bus.subscribe(EventType.ORDER_FILLED, self._on_order_filled)
        self._event_bus.subscribe(EventType.ORDER_REJECTED, self._on_order_rejected)
        self._event_bus.subscribe(EventType.RISK_BREACH, self._on_risk_breach)
        self._event_bus.subscribe(
            EventType.CIRCUIT_BREAKER_TRIGGERED, self._on_circuit_breaker
        )
        self._event_bus.subscribe(EventType.KILL_SWITCH, self._on_kill_switch)
        self._event_bus.subscribe(EventType.STRATEGY_STARTED, self._on_strategy_started)
        self._event_bus.subscribe(EventType.STRATEGY_STOPPED, self._on_strategy_stopped)
        self._event_bus.subscribe(EventType.STRATEGY_ERROR, self._on_strategy_error)
        self._event_bus.subscribe(EventType.CONNECTION_LOST, self._on_connection_lost)
        self._event_bus.subscribe(
            EventType.CONNECTION_RESTORED, self._on_connection_restored
        )

        logger.info("NotificationManager initialized")

    async def _on_order_filled(self, event: Event) -> None:
        """Send trade execution notification."""
        order = event.payload.get("order", {})
        text = templates.trade_executed(
            tradingsymbol=order.get("tradingsymbol", "?"),
            side=order.get("order_side", "?"),
            quantity=order.get("fill_quantity", order.get("quantity", 0)),
            price=Decimal(str(order.get("fill_price", 0))),
            strategy_id=order.get("strategy_id", "?"),
            order_id=str(order.get("id", "")),
        )
        await self._send(text)

    async def _on_order_rejected(self, event: Event) -> None:
        """Send order rejection notification."""
        order = event.payload.get("order", {})
        text = templates.order_rejected(
            tradingsymbol=order.get("tradingsymbol", "?"),
            side=order.get("order_side", "?"),
            quantity=order.get("quantity", 0),
            strategy_id=order.get("strategy_id", "?"),
            reason=event.payload.get("message", ""),
        )
        await self._send(text)

    async def _on_risk_breach(self, event: Event) -> None:
        """Send risk breach alert."""
        text = templates.risk_breach(
            breach_type=event.payload.get("breach_type", "Unknown"),
            details=event.payload.get("details", ""),
        )
        await self._send(text)

    async def _on_circuit_breaker(self, event: Event) -> None:
        """Send circuit breaker triggered alert (critical — retries on failure)."""
        text = templates.circuit_breaker_triggered(
            reason=event.payload.get("reason", "Unknown"),
        )
        await self._send_critical(text)

    async def _on_kill_switch(self, event: Event) -> None:
        """Send kill switch activation alert (critical — retries on failure)."""
        results = event.payload.get("results", {})
        text = templates.kill_switch_activated(
            reason=event.payload.get("reason", "Unknown"),
            orders_cancelled=results.get("orders_cancelled", 0),
            positions_closed=results.get("positions_closed", 0),
        )
        await self._send_critical(text)

    async def _on_strategy_started(self, event: Event) -> None:
        """Send strategy started notification."""
        text = templates.strategy_started(
            strategy_id=event.payload.get("strategy_id", "?"),
        )
        await self._send(text)

    async def _on_strategy_stopped(self, event: Event) -> None:
        """Send strategy stopped notification."""
        text = templates.strategy_stopped(
            strategy_id=event.payload.get("strategy_id", "?"),
        )
        await self._send(text)

    async def _on_strategy_error(self, event: Event) -> None:
        """Send strategy error notification."""
        text = templates.strategy_error(
            strategy_id=event.payload.get("strategy_id", "?"),
            error=event.payload.get("error", "Unknown error"),
        )
        await self._send(text)

    async def _on_connection_lost(self, event: Event) -> None:
        """Send connection lost notification."""
        text = templates.connection_lost(source=event.source)
        await self._send(text)

    async def _on_connection_restored(self, event: Event) -> None:
        """Send connection restored notification."""
        text = templates.connection_restored(source=event.source)
        await self._send(text)

    async def _send(self, text: str) -> None:
        """Send message via Telegram, swallowing errors."""
        try:
            await self._telegram.send_message(text)
        except Exception:
            logger.exception("Failed to send notification")

    async def _send_critical(self, text: str, max_retries: int = 3) -> None:
        """Send critical message with retries. These must reach the user."""
        for attempt in range(1, max_retries + 1):
            try:
                await self._telegram.send_message(text)
                return
            except Exception:
                logger.exception(
                    f"Failed to send CRITICAL notification (attempt {attempt}/{max_retries})"
                )
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)  # 2s, 4s backoff
        logger.critical(
            f"CRITICAL NOTIFICATION DROPPED after {max_retries} retries: {text[:200]}"
        )

    async def close(self) -> None:
        """Cleanup resources."""
        await self._telegram.close()
