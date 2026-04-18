"""Strategy runner — manages lifecycle and event dispatch for all strategies."""

import asyncio
import logging
from datetime import datetime

from src.core.events import Event, EventBus, EventType
from src.core.models import OHLC, Order, OrderRequest, Signal, Tick
from src.core.types import OrderSide, ProductType, StrategyState
from src.market_data.aggregator import OHLCAggregator
from src.market_data.feed import TickFeedManager
from src.market_data.option_chain import OptionChainBuilder
from src.core.clock import MarketClock
from src.strategy.base import BaseStrategy
from src.strategy.context import StrategyContext
from src.strategy.state_store import StrategyStateStore

logger = logging.getLogger(__name__)


class StrategyRunner:
    """Manages strategy lifecycle and dispatches market events to strategies.

    - Starts/stops strategies
    - Dispatches TICK and CANDLE_CLOSED events to subscribed strategies
    - Handles ORDER_FILLED/REJECTED events
    - Catches strategy errors to prevent cascade failures
    """

    def __init__(
        self,
        event_bus: EventBus,
        feed: TickFeedManager,
        option_chain_builder: OptionChainBuilder,
        aggregator: OHLCAggregator,
        clock: MarketClock,
        order_callback,    # async callable: Signal -> list[Order]
        portfolio_getter,  # callable: (what, strategy_id) -> data
        state_store: StrategyStateStore | None = None,
    ):
        self._event_bus = event_bus
        self._feed = feed
        self._chain_builder = option_chain_builder
        self._aggregator = aggregator
        self._clock = clock
        self._order_callback = order_callback
        self._portfolio_getter = portfolio_getter
        # state_store may be None in tests/backtests; runner falls back to no-op
        self._state_store = state_store or StrategyStateStore(None)

        self._strategies: dict[str, BaseStrategy] = {}
        self._strategy_tokens: dict[str, set[int]] = {}  # strategy_id -> subscribed tokens

        # Subscribe to events
        self._event_bus.subscribe(EventType.TICK, self._on_tick)
        self._event_bus.subscribe(EventType.CANDLE_CLOSED, self._on_candle)
        self._event_bus.subscribe(EventType.ORDER_FILLED, self._on_order_update)
        self._event_bus.subscribe(EventType.ORDER_REJECTED, self._on_order_update)

    async def add_strategy(self, strategy: BaseStrategy) -> None:
        """Add and start a strategy."""
        sid = strategy.strategy_id

        # Create context
        context = StrategyContext(
            strategy_id=sid,
            feed=self._feed,
            option_chain_builder=self._chain_builder,
            aggregator=self._aggregator,
            clock=self._clock,
            order_callback=self._order_callback,
            portfolio_getter=self._portfolio_getter,
        )
        strategy.set_context(context)

        # Get subscriptions
        subs = strategy.get_subscriptions()
        # Always subscribe to India VIX for VIX filter
        from src.core.constants import INDIA_VIX_TOKEN
        tokens = list(subs.instrument_tokens)
        if INDIA_VIX_TOKEN not in tokens:
            tokens.append(INDIA_VIX_TOKEN)

        # Auto-subscribe to the underlying's spot token so strategies
        # receive regular ticks even if get_subscriptions() returns empty.
        underlying = getattr(strategy.params, "underlying", None)
        if underlying:
            for spot_token, name in self._chain_builder._spot_tokens.items():
                if name == underlying and spot_token not in tokens:
                    tokens.append(spot_token)
                    logger.info(f"Auto-subscribed {sid} to {name} spot token {spot_token}")

        new_tokens = self._feed.subscribe(tokens, sid)
        self._strategy_tokens[sid] = set(tokens)

        # Restore today's state BEFORE on_start so flags like _entered survive restart.
        # Loading by today's date guarantees yesterday's stale state can't leak forward.
        today = self._clock.now().date()
        saved = await self._state_store.load_state(sid, today)
        if saved:
            try:
                strategy.load_state_data(saved)
                logger.info(
                    f"[STATE_STORE] {sid} restored state for {today}: "
                    f"keys={list(saved.keys())}"
                )
            except Exception as e:
                logger.warning(f"[STATE_STORE] {sid} load_state_data failed: {e}")

        # Start strategy
        strategy.state = StrategyState.STARTING
        try:
            await strategy.on_start()
            strategy.state = StrategyState.RUNNING
            self._strategies[sid] = strategy
            logger.info(f"Strategy {sid} started (subscriptions: {len(subs.instrument_tokens)} tokens)")

            await self._event_bus.publish(
                Event.create(EventType.STRATEGY_STARTED, source="runner", strategy_id=sid)
            )
        except Exception as e:
            strategy.state = StrategyState.ERROR
            logger.exception(f"Strategy {sid} failed to start: {e}")

    async def remove_strategy(self, strategy_id: str) -> None:
        """Stop and remove a strategy."""
        strategy = self._strategies.get(strategy_id)
        if not strategy:
            return

        strategy.state = StrategyState.STOPPING
        # Persist final state on stop so the next restart can resume.
        await self._persist_state(strategy)
        try:
            await strategy.on_stop()
        except Exception:
            logger.exception(f"Error stopping strategy {strategy_id}")

        strategy.state = StrategyState.STOPPED
        self._feed.unsubscribe(strategy_id)
        self._strategy_tokens.pop(strategy_id, None)
        self._strategies.pop(strategy_id, None)

        await self._event_bus.publish(
            Event.create(EventType.STRATEGY_STOPPED, source="runner", strategy_id=strategy_id)
        )
        logger.info(f"Strategy {strategy_id} stopped")

    async def stop_all(self) -> None:
        """Stop all running strategies."""
        for sid in list(self._strategies.keys()):
            await self.remove_strategy(sid)

    def reset_strategies_daily(self) -> None:
        """Reset day-level flags on all strategies at start of new trading day."""
        for sid, strategy in self._strategies.items():
            strategy.reset_day_state()
            logger.info(f"Strategy {sid} day state reset")

    def get_strategy(self, strategy_id: str) -> BaseStrategy | None:
        return self._strategies.get(strategy_id)

    def get_all_strategies(self) -> dict[str, BaseStrategy]:
        return dict(self._strategies)

    async def _on_tick(self, event: Event) -> None:
        """Dispatch tick to subscribed strategies."""
        tick_data = event.payload.get("tick")
        if not tick_data:
            return

        tick = Tick(**tick_data)
        token = tick.instrument_token

        for sid, tokens in self._strategy_tokens.items():
            if token not in tokens:
                continue

            strategy = self._strategies.get(sid)
            if not strategy or strategy.state != StrategyState.RUNNING:
                continue

            try:
                signal = await asyncio.wait_for(
                    strategy.on_tick(tick), timeout=0.5
                )
                if signal:
                    await self._process_signal(signal)
            except asyncio.TimeoutError:
                logger.warning(f"Strategy {sid} on_tick timed out (>500ms)")
            except Exception as e:
                logger.exception(f"Strategy {sid} on_tick error: {e}")
                await strategy.on_error(e)

    async def _on_candle(self, event: Event) -> None:
        """Dispatch candle close to all running strategies."""
        candle_data = event.payload.get("candle")
        if not candle_data:
            return

        candle = OHLC(**candle_data)

        for strategy in self._strategies.values():
            if strategy.state != StrategyState.RUNNING:
                continue
            try:
                signal = await strategy.on_candle(candle)
                if signal:
                    await self._process_signal(signal)
            except Exception as e:
                logger.exception(f"Strategy {strategy.strategy_id} on_candle error: {e}")

    async def _on_order_update(self, event: Event) -> None:
        """Dispatch order updates to the relevant strategy."""
        order_data = event.payload.get("order")
        if not order_data:
            return

        order = Order(**order_data)
        strategy = self._strategies.get(order.strategy_id)
        if strategy:
            try:
                await strategy.on_order_update(order)
            except Exception as e:
                logger.exception(f"Strategy {order.strategy_id} on_order_update error: {e}")
            # Order updates change day flags (entered, trades_today, stopped_for_day).
            # Persist after every update so a crash between fills can't lose state.
            await self._persist_state(strategy)

    async def _process_signal(self, signal: Signal) -> None:
        """Convert signal to order requests and route through OMS."""
        # Guard: never place orders on holidays or outside market hours
        if not self._clock.is_market_open():
            logger.warning(f"Signal from {signal.strategy_id} ignored — market not open")
            return
        # Shadow-only gate (Apr 18 plan-review fix): if the strategy is flagged
        # shadow_only, log the would-be order and return WITHOUT touching the
        # OMS. The strategy still flips its internal "_entered" flags so its
        # subsequent on_tick exit logic fires; it just never moves capital.
        # The decision logger has already captured the entry/exit context, so
        # offline reconstruction can compare champion vs. shadow P&L.
        strategy = self._strategies.get(signal.strategy_id)
        shadow = bool(getattr(strategy.params, "shadow_only", False)) if strategy else False
        if shadow:
            leg_summary = ", ".join(
                f"{leg.order_side.value if hasattr(leg.order_side, 'value') else leg.order_side}"
                f" {leg.quantity}@{leg.tradingsymbol}"
                for leg in signal.legs
            )
            logger.info(
                f"[SHADOW_ORDER] {signal.strategy_id} signal_type={signal.signal_type} "
                f"legs=[{leg_summary}] — no OMS routing (shadow_only=True)"
            )
            # Persist state so the strategy's "entered" flags survive a crash;
            # without this a shadow-restart would re-fire the same entry signal.
            await self._persist_state(strategy)
            return
        try:
            await self._order_callback(signal)
            # Persist state after a signal-driven entry/exit lands. Catches
            # the gap where a strategy flipped its flags but no fill arrives yet.
            strategy = self._strategies.get(signal.strategy_id)
            if strategy:
                await self._persist_state(strategy)
        except Exception as e:
            logger.exception(f"Failed to process signal from {signal.strategy_id}: {e}")
            # Notify the strategy so it can react (e.g., revert state changes)
            strategy = self._strategies.get(signal.strategy_id)
            if strategy:
                try:
                    await strategy.on_error(e)
                except Exception:
                    logger.exception(f"Strategy {signal.strategy_id} on_error also failed")

    async def _persist_state(self, strategy: BaseStrategy) -> None:
        """Snapshot the strategy's day state into the store. No-op if disabled."""
        if not self._state_store.enabled:
            return
        try:
            data = strategy.get_state_data()
            if not data:
                return
            today = self._clock.now().date()
            await self._state_store.save_state(strategy.strategy_id, today, data)
        except Exception as e:
            logger.warning(
                f"[STATE_STORE] persist failed for {strategy.strategy_id}: {e}"
            )
