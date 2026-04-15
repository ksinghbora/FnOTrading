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

    def _check_pcr_filter(self, underlying: str, expiry: "date | None") -> str | None:
        """Check if Put-Call Ratio (OI) is within healthy range for premium selling.

        Returns None if OK to enter, or a reason string if entry should be skipped.
        When pcr_filter_enabled=False, always returns None but still logs.
        """
        if not expiry:
            return None
        chain = self.ctx.get_option_chain(underlying, expiry)
        if not chain or not chain.strikes:
            return None
        pcr = chain.pcr_oi
        if pcr <= 0:
            return None  # Not enough OI data yet

        in_range = self.params.pcr_oi_min <= pcr <= self.params.pcr_oi_max
        action = "PASS" if in_range else "WOULD_BLOCK"
        if self.params.pcr_filter_enabled and not in_range:
            action = "BLOCK"

        logger.info(
            f"[FILTER] strategy={self.strategy_id} filter=pcr_oi "
            f"value={pcr:.2f} range=[{self.params.pcr_oi_min}-{self.params.pcr_oi_max}] "
            f"action={action}"
        )

        if self.params.pcr_filter_enabled and not in_range:
            return f"PCR_OI {pcr:.2f} outside range [{self.params.pcr_oi_min}-{self.params.pcr_oi_max}]"
        return None

    def _check_max_pain_filter(self, underlying: str, expiry: "date | None") -> str | None:
        """Check if spot is near max pain (favorable for premium sellers).

        Returns None if OK to enter, or a reason string if entry should be skipped.
        When max_pain_filter_enabled=False, always returns None but still logs.
        """
        if not expiry:
            return None
        chain = self.ctx.get_option_chain(underlying, expiry)
        if not chain or not chain.strikes:
            return None
        max_pain = chain.max_pain
        spot = chain.spot_price
        if max_pain <= 0 or spot <= 0:
            return None

        distance_pct = abs(float(spot) - float(max_pain)) / float(spot) * 100
        in_range = distance_pct <= self.params.max_pain_proximity_pct
        action = "PASS" if in_range else "WOULD_BLOCK"
        if self.params.max_pain_filter_enabled and not in_range:
            action = "BLOCK"

        logger.info(
            f"[FILTER] strategy={self.strategy_id} filter=max_pain "
            f"max_pain={max_pain} spot={spot} distance={distance_pct:.2f}% "
            f"threshold={self.params.max_pain_proximity_pct}% action={action}"
        )

        if self.params.max_pain_filter_enabled and not in_range:
            return (
                f"Spot {spot} is {distance_pct:.2f}% from max pain {max_pain} "
                f"(threshold: {self.params.max_pain_proximity_pct}%)"
            )
        return None

    def _log_iv_skew(self, underlying: str, expiry: "date | None") -> None:
        """Log IV skew data at entry time for research/analysis."""
        if not expiry:
            return
        chain = self.ctx.get_option_chain(underlying, expiry)
        if not chain or not chain.strikes:
            return
        from src.options.chain_analyzer import get_iv_skew
        skew = get_iv_skew(chain)
        atm_ce_iv = skew.get("atm_iv_ce", 0)
        atm_pe_iv = skew.get("atm_iv_pe", 0)

        otm_puts = skew.get("otm_puts", [])
        otm_calls = skew.get("otm_calls", [])
        avg_put_iv = sum(p["iv"] for p in otm_puts) / len(otm_puts) if otm_puts else 0
        avg_call_iv = sum(c["iv"] for c in otm_calls) / len(otm_calls) if otm_calls else 0

        ratio = avg_put_iv / avg_call_iv if avg_call_iv > 0 else 0
        bias = "PUT_HEAVY" if ratio > 1.15 else ("CALL_HEAVY" if ratio < 0.85 else "NEUTRAL")

        logger.info(
            f"[FILTER] strategy={self.strategy_id} filter=iv_skew "
            f"atm_ce_iv={atm_ce_iv:.3f} atm_pe_iv={atm_pe_iv:.3f} "
            f"avg_otm_put_iv={avg_put_iv:.3f} avg_otm_call_iv={avg_call_iv:.3f} "
            f"ratio={ratio:.2f} bias={bias}"
        )

    def _log_oi_levels(self, underlying: str, expiry: "date | None") -> None:
        """Log high-OI levels (support/resistance) at entry time for research."""
        if not expiry:
            return
        chain = self.ctx.get_option_chain(underlying, expiry)
        if not chain or not chain.strikes:
            return
        from src.options.chain_analyzer import get_high_oi_strikes
        oi_data = get_high_oi_strikes(chain, top_n=3)
        ce_levels = [f"{s['strike']}({s['oi']})" for s in oi_data.get("ce_high_oi", [])]
        pe_levels = [f"{s['strike']}({s['oi']})" for s in oi_data.get("pe_high_oi", [])]

        logger.info(
            f"[FILTER] strategy={self.strategy_id} filter=oi_levels "
            f"ce_resistance=[{','.join(ce_levels)}] "
            f"pe_support=[{','.join(pe_levels)}]"
        )

    def _check_trend_filter(self, underlying: str) -> str | None:
        """Check if market is trending too strongly for premium selling.

        Uses morning range (9:15-9:30 open/close) vs current spot.
        If spot has moved > 0.5% from open, market is trending — skip entry.
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
        threshold = 0.5  # Tightened from 0.7% — real data shows 0.5%+ moves lead to losses
        logger.debug(
            f"[FILTER] strategy={self.strategy_id} filter=trend "
            f"session_open={session_open:.0f} spot={float(spot):.0f} "
            f"move_pct={move_pct:.2f} threshold={threshold} "
            f"result={'block' if move_pct > threshold else 'pass'}"
        )
        if move_pct > threshold:
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
