"""KiteTicker WebSocket wrapper for real-time market data."""

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable

from kiteconnect import KiteTicker

from src.core.events import Event, EventBus, EventType
from src.core.models import Tick

logger = logging.getLogger(__name__)


class TickerManager:
    """Wraps KiteTicker WebSocket for async integration with the EventBus."""

    HEARTBEAT_TIMEOUT = 30  # seconds without ticks to consider connection dead

    def __init__(
        self,
        api_key: str,
        access_token: str,
        event_bus: EventBus,
    ):
        self._api_key = api_key
        self._access_token = access_token
        self._event_bus = event_bus
        self._ticker: KiteTicker | None = None
        self._subscribed_tokens: set[int] = set()
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_tick_time: datetime | None = None
        self._heartbeat_task: asyncio.Task | None = None
        # Token -> tradingsymbol lookup (Kite ticks don't include tradingsymbol)
        self._token_symbols: dict[int, str] = {}

    def set_symbol_map(self, token_symbols: dict[int, str]) -> None:
        """Set token -> tradingsymbol mapping for tick enrichment."""
        self._token_symbols.update(token_symbols)

    async def start(self) -> None:
        """Start the WebSocket ticker in a background thread."""
        self._loop = asyncio.get_event_loop()
        self._ticker = KiteTicker(self._api_key, self._access_token)

        # Register callbacks
        self._ticker.on_ticks = self._on_ticks
        self._ticker.on_connect = self._on_connect
        self._ticker.on_close = self._on_close
        self._ticker.on_error = self._on_error
        self._ticker.on_reconnect = self._on_reconnect

        self._running = True
        # KiteTicker.connect() is blocking, run in thread
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._ticker.connect(threaded=True)
        )
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("KiteTicker started")

    async def stop(self) -> None:
        """Stop the WebSocket ticker."""
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        if self._ticker:
            self._ticker.close()
            self._ticker = None
        logger.info("KiteTicker stopped")

    def subscribe(self, tokens: list[int], mode: str = "full") -> None:
        """Subscribe to instrument tokens.

        Modes: 'full' (all data), 'quote' (no depth), 'ltp' (only LTP)

        Tokens are always stored and (re-)subscribed on WebSocket connect,
        so it's safe to call this before the connection is established.
        """
        self._subscribed_tokens.update(tokens)
        if not self._ticker:
            return

        try:
            self._ticker.subscribe(list(tokens))
            self._ticker.set_mode(mode, list(tokens))
            logger.info(f"Subscribed to {len(tokens)} tokens (total: {len(self._subscribed_tokens)})")
        except Exception:
            # WebSocket not connected yet — tokens stored, will subscribe on connect
            pass

    def unsubscribe(self, tokens: list[int]) -> None:
        """Unsubscribe from instrument tokens."""
        if self._ticker:
            self._ticker.unsubscribe(tokens)
        self._subscribed_tokens -= set(tokens)

    def _on_ticks(self, ws: Any, ticks: list[dict]) -> None:
        """Callback: received tick data from WebSocket."""
        if not self._loop or not self._running:
            return

        self._last_tick_time = datetime.now()

        for tick_data in ticks:
            try:
                tick = self._parse_tick(tick_data)
                event = Event.create(
                    EventType.TICK,
                    source="ticker",
                    tick=tick.model_dump(),
                )
                asyncio.run_coroutine_threadsafe(
                    self._event_bus.publish(event), self._loop
                )
            except Exception:
                logger.exception("Error parsing tick data")

    def _on_connect(self, ws: Any, response: Any) -> None:
        """Callback: WebSocket connected."""
        logger.info("KiteTicker connected")
        # Re-subscribe to all tokens
        if self._subscribed_tokens:
            token_list = list(self._subscribed_tokens)
            try:
                ws.subscribe(token_list)
                ws.set_mode(ws.MODE_FULL, token_list)
                logger.info(f"Re-subscribed to {len(token_list)} tokens")
            except Exception:
                logger.exception(
                    f"Failed to re-subscribe {len(token_list)} tokens on reconnect"
                )

        if self._loop:
            event = Event.create(EventType.CONNECTION_RESTORED, source="ticker")
            asyncio.run_coroutine_threadsafe(
                self._event_bus.publish(event), self._loop
            )

    def _on_close(self, ws: Any, code: int, reason: str) -> None:
        """Callback: WebSocket disconnected."""
        logger.warning(f"KiteTicker disconnected: {code} - {reason}")
        if self._loop and self._running:
            event = Event.create(
                EventType.CONNECTION_LOST,
                source="ticker",
                code=code,
                reason=reason,
            )
            asyncio.run_coroutine_threadsafe(
                self._event_bus.publish(event), self._loop
            )

    def _on_error(self, ws: Any, code: int, reason: str) -> None:
        """Callback: WebSocket error."""
        logger.error(f"KiteTicker error: {code} - {reason}")

    def _on_reconnect(self, ws: Any, attempts: int) -> None:
        """Callback: WebSocket reconnecting."""
        logger.info(f"KiteTicker reconnecting (attempt {attempts})")

    async def _heartbeat_loop(self) -> None:
        """Monitor tick flow — publish CONNECTION_LOST if ticks stop arriving."""
        while self._running:
            try:
                await asyncio.sleep(self.HEARTBEAT_TIMEOUT)
                if not self._running or not self._subscribed_tokens:
                    continue
                if self._last_tick_time:
                    elapsed = (datetime.now() - self._last_tick_time).total_seconds()
                    if elapsed > self.HEARTBEAT_TIMEOUT:
                        logger.warning(
                            f"No ticks received for {elapsed:.0f}s — possible silent disconnect"
                        )
                        event = Event.create(
                            EventType.CONNECTION_LOST,
                            source="ticker_heartbeat",
                            reason=f"No ticks for {elapsed:.0f}s",
                        )
                        await self._event_bus.publish(event)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in heartbeat loop")

    def _parse_tick(self, tick_data: dict) -> Tick:
        """Parse raw Kite tick data into our Tick model."""
        depth = tick_data.get("depth", {})
        buy_depth = depth.get("buy", [{}])
        sell_depth = depth.get("sell", [{}])

        token = tick_data["instrument_token"]
        symbol = tick_data.get("tradingsymbol") or self._token_symbols.get(token, "")
        return Tick(
            instrument_token=token,
            tradingsymbol=symbol,
            timestamp=tick_data.get("exchange_timestamp", datetime.now()),
            ltp=Decimal(str(tick_data.get("last_price", 0))),
            volume=tick_data.get("volume_traded", 0),
            oi=tick_data.get("oi", 0),
            bid_price=Decimal(str(buy_depth[0].get("price", 0))) if buy_depth else Decimal("0"),
            ask_price=Decimal(str(sell_depth[0].get("price", 0))) if sell_depth else Decimal("0"),
            bid_qty=buy_depth[0].get("quantity", 0) if buy_depth else 0,
            ask_qty=sell_depth[0].get("quantity", 0) if sell_depth else 0,
            high=Decimal(str(tick_data.get("ohlc", {}).get("high", 0))),
            low=Decimal(str(tick_data.get("ohlc", {}).get("low", 0))),
            open=Decimal(str(tick_data.get("ohlc", {}).get("open", 0))),
            close=Decimal(str(tick_data.get("ohlc", {}).get("close", 0))),
        )
