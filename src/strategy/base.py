"""Base strategy abstract class — the contract all strategies implement."""

import logging
from abc import ABC, abstractmethod
from typing import Any

from src.core.models import OHLC, Order, Signal, Subscription, Tick
from src.core.types import StrategyState
from src.strategy.params import BaseStrategyParams

logger = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """Abstract base class for all trading strategies.

    Lifecycle:
    1. __init__() — receives params, creates strategy
    2. on_start() — subscribe to instruments, load state
    3. on_tick() / on_candle() — receive market data, generate signals
    4. on_order_update() — handle fill/rejection notifications
    5. on_stop() — cleanup, save state

    Strategies never directly touch the broker. They return Signal objects
    which are validated by risk management before execution.
    """

    def __init__(self, strategy_id: str, params: BaseStrategyParams):
        self.strategy_id = strategy_id
        self.params = params
        self.state = StrategyState.IDLE
        self._context: Any = None  # Set by StrategyRunner

    def set_context(self, context: "StrategyContext") -> None:
        """Inject the strategy context (called by runner, not by strategy)."""
        self._context = context

    @property
    def ctx(self) -> "StrategyContext":
        """Shortcut to strategy context."""
        if self._context is None:
            raise RuntimeError(f"Strategy {self.strategy_id} context not set")
        return self._context

    @abstractmethod
    def get_subscriptions(self) -> Subscription:
        """Declare what instruments and timeframes this strategy needs."""

    @abstractmethod
    async def on_start(self) -> None:
        """Called when strategy starts. Initialize state, load positions."""

    @abstractmethod
    async def on_tick(self, tick: Tick) -> Signal | None:
        """Called on every tick for subscribed instruments.

        Return a Signal to place orders, or None to do nothing.
        """

    async def on_candle(self, candle: OHLC) -> Signal | None:
        """Called on candle close for subscribed timeframes.

        Override if strategy uses candle-based logic. Default: no-op.
        """
        return None

    async def on_order_update(self, order: Order) -> None:
        """Called when an order for this strategy is filled/rejected.

        Override to handle execution updates. Default: no-op.
        """
        pass

    @abstractmethod
    async def on_stop(self) -> None:
        """Called on strategy shutdown. Save state, optionally close positions."""

    async def on_error(self, error: Exception) -> None:
        """Called on unhandled error. Default: log."""
        logger.exception(f"Strategy {self.strategy_id} error: {error}")

    def _check_vix_filter(self) -> str | None:
        """Check if VIX is too high for entry.

        Returns None if OK to enter, or a reason string if entry should be skipped.
        """
        vix = self.ctx.get_vix()
        if vix <= 0:
            return None  # VIX data unavailable, allow entry
        result = "block" if vix > self.params.vix_entry_max else "pass"
        logger.debug(
            f"[FILTER] strategy={self.strategy_id} filter=vix "
            f"value={vix:.1f} threshold={self.params.vix_entry_max} result={result}"
        )
        if vix > self.params.vix_entry_max:
            return f"VIX {vix:.1f} exceeds max {self.params.vix_entry_max}"
        return None

    def _get_vix_adjusted_lots(self) -> int:
        """Return quantity_lots adjusted for VIX regime.

        If VIX is above vix_reduce_above, halve the lots (minimum 1).
        """
        vix = self.ctx.get_vix()
        lots = self.params.quantity_lots
        if vix > 0 and vix > self.params.vix_reduce_above:
            lots = max(1, lots // 2)
            logger.info(
                f"[{self.strategy_id}] VIX={vix:.1f} > {self.params.vix_reduce_above}, "
                f"reducing lots to {lots}"
            )
        return lots

    def _check_trend_filter(self, underlying: str) -> str | None:
        """Check if market is trending too strongly for premium selling.

        Uses morning range (9:15-9:30 open/close) vs current spot.
        If spot has moved > 0.7% from open, market is trending — skip entry.
        Returns None if OK, or a reason string to skip.
        """
        from src.core.types import Timeframe

        spot = self.ctx.get_spot_price(underlying)
        if spot <= 0:
            return None

        # Use 15-minute candles to check morning range
        chain_builder = self.ctx._chain_builder
        spot_token = None
        for token, name in chain_builder._spot_tokens.items():
            if name == underlying:
                spot_token = token
                break

        if not spot_token:
            return None

        candles = self.ctx.get_candles(spot_token, Timeframe.M15, limit=3)
        if not candles:
            return None

        # Use first candle's open as the session reference
        session_open = float(candles[0].open)
        if session_open <= 0:
            return None

        move_pct = abs(float(spot) - session_open) / session_open * 100
        logger.debug(
            f"[FILTER] strategy={self.strategy_id} filter=trend "
            f"session_open={session_open:.0f} spot={float(spot):.0f} "
            f"move_pct={move_pct:.2f} threshold=0.7 "
            f"result={'block' if move_pct > 0.7 else 'pass'}"
        )
        if move_pct > 0.7:
            direction = "up" if float(spot) > session_open else "down"
            return (
                f"Market trending {direction} {move_pct:.2f}% from open "
                f"({session_open:.0f} -> {float(spot):.0f})"
            )
        return None

    def _check_expiry_rollover(self, current_expiry: "date | None", underlying: str) -> "date | None":
        """If the current expiry is in the past, roll to the next one.

        Returns the new expiry if rolled, or None if no rollover needed.
        """
        if current_expiry is None:
            return None
        today = self.ctx.clock.now().date()
        if today > current_expiry:
            new_expiry = self.ctx.next_expiry(underlying)
            logger.info(
                f"[{self.strategy_id}] Expiry rollover: {current_expiry} -> {new_expiry}"
            )
            return new_expiry
        return None

    def reset_day_state(self) -> None:
        """Reset intraday flags at start of a new trading day.

        Override in subclasses that use _entered / _stopped_for_day flags.
        """
        if hasattr(self, "_entered"):
            self._entered = False
        if hasattr(self, "_stopped_for_day"):
            self._stopped_for_day = False

    def get_state_data(self) -> dict:
        """Serialize strategy-specific state for persistence.

        Override in subclasses to save custom state (e.g., legs, adjustments).
        """
        return {}

    def load_state_data(self, data: dict) -> None:
        """Restore strategy-specific state from persistence.

        Override in subclasses to restore custom state.
        """
        pass


# Forward reference for type hint
from src.strategy.context import StrategyContext  # noqa: E402
