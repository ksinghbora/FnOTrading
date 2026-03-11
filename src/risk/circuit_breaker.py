"""Circuit breaker — automatically halts trading on anomalies."""

import logging
from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal

from src.core.events import Event, EventBus, EventType
from src.core.types import CircuitBreakerState

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Circuit breaker pattern for trading safety.

    States:
    - CLOSED: Normal operation, all trading allowed.
    - OPEN: Trading halted, all new orders blocked.
    - HALF_OPEN: Limited trading (only risk-reducing orders).

    Triggers:
    - Day loss exceeds threshold
    - Rapid loss (X amount in Y minutes)
    - Network disconnection lasting too long
    - Manual activation
    """

    def __init__(
        self,
        event_bus: EventBus,
        max_day_loss: Decimal = Decimal("15000"),
        rapid_loss_amount: Decimal = Decimal("5000"),
        rapid_loss_window_minutes: int = 5,
        disconnect_threshold_seconds: int = 120,
        auto_reset_minutes: int = 0,  # 0 = manual reset only
    ):
        self._event_bus = event_bus
        self._state = CircuitBreakerState.CLOSED
        self._max_day_loss = max_day_loss
        self._rapid_loss_amount = rapid_loss_amount
        self._rapid_loss_window = timedelta(minutes=rapid_loss_window_minutes)
        self._disconnect_threshold = timedelta(seconds=disconnect_threshold_seconds)
        self._auto_reset_minutes = auto_reset_minutes

        self._triggered_at: datetime | None = None
        self._trigger_reason: str = ""
        self._pnl_snapshots: deque[tuple[datetime, Decimal]] = deque(maxlen=100)
        self._disconnect_since: datetime | None = None

        # Subscribe to events
        self._event_bus.subscribe(EventType.CONNECTION_LOST, self._on_disconnect)
        self._event_bus.subscribe(EventType.CONNECTION_RESTORED, self._on_reconnect)

    @property
    def state(self) -> CircuitBreakerState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state != CircuitBreakerState.CLOSED

    @property
    def trigger_reason(self) -> str:
        return self._trigger_reason

    def check_pnl(self, current_day_pnl: Decimal) -> None:
        """Check P&L-based triggers."""
        now = datetime.now()
        self._pnl_snapshots.append((now, current_day_pnl))

        # Auto-reset after configured interval
        if (
            self._state == CircuitBreakerState.OPEN
            and self._auto_reset_minutes > 0
            and self._triggered_at
        ):
            elapsed = (now - self._triggered_at).total_seconds() / 60
            if elapsed >= self._auto_reset_minutes:
                logger.info(
                    f"Circuit breaker auto-reset after {self._auto_reset_minutes} minutes"
                )
                self.reset()
                return

        # Check absolute day loss
        if current_day_pnl < -self._max_day_loss:
            self._trip(f"Day loss {current_day_pnl} exceeded limit {-self._max_day_loss}")
            return

        # Check rapid loss — compare current PnL to the peak within the window
        # to detect drops even if PnL recovered mid-window before crashing
        cutoff = now - self._rapid_loss_window
        recent = [(t, pnl) for t, pnl in self._pnl_snapshots if t >= cutoff]
        if len(recent) >= 2:
            peak_pnl = max(pnl for _, pnl in recent)
            pnl_drop = recent[-1][1] - peak_pnl
            if pnl_drop < -self._rapid_loss_amount:
                self._trip(
                    f"Rapid loss: {pnl_drop} from peak {peak_pnl} "
                    f"in {self._rapid_loss_window.seconds // 60} minutes"
                )

    def _trip(self, reason: str) -> None:
        """Activate the circuit breaker."""
        if self._state == CircuitBreakerState.OPEN:
            return  # Already tripped

        self._state = CircuitBreakerState.OPEN
        self._triggered_at = datetime.now()
        self._trigger_reason = reason
        logger.critical(f"CIRCUIT BREAKER TRIPPED: {reason}")

        # Publish event (async handled by caller)
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(
                    self._event_bus.publish(
                        Event.create(
                            EventType.CIRCUIT_BREAKER_TRIGGERED,
                            source="circuit_breaker",
                            reason=reason,
                        )
                    )
                )
        except RuntimeError:
            pass

    def reset(self) -> None:
        """Manually reset the circuit breaker to CLOSED state."""
        if self._state == CircuitBreakerState.CLOSED:
            return
        self._state = CircuitBreakerState.CLOSED
        self._trigger_reason = ""
        self._triggered_at = None
        self._pnl_snapshots.clear()
        logger.info("Circuit breaker RESET to CLOSED")

    def half_open(self) -> None:
        """Transition to HALF_OPEN (allow risk-reducing orders only)."""
        self._state = CircuitBreakerState.HALF_OPEN
        logger.info("Circuit breaker moved to HALF_OPEN")

    async def _on_disconnect(self, event: Event) -> None:
        """Track WebSocket disconnection duration."""
        self._disconnect_since = datetime.now()
        logger.warning("Tracking disconnection for circuit breaker")

    async def _on_reconnect(self, event: Event) -> None:
        """Reset disconnection tracking."""
        if self._disconnect_since:
            duration = datetime.now() - self._disconnect_since
            if duration > self._disconnect_threshold:
                self._trip(f"Network disconnection lasted {duration.seconds}s")
            self._disconnect_since = None
