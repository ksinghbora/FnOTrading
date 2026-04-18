"""KiteTicker WebSocket wrapper for real-time market data."""

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable

from kiteconnect import KiteTicker

from src.core.clock import now_ist
from src.core.events import Event, EventBus, EventType
from src.core.models import Tick
from src.utils.log_tags import Tag

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
        self._tick_log_count: int = 0

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
        """
        if not self._ticker:
            # Store for subscription on connect
            self._subscribed_tokens.update(tokens)
            return

        new_tokens = [t for t in tokens if t not in self._subscribed_tokens]
        if new_tokens:
            self._ticker.subscribe(new_tokens)
            self._ticker.set_mode(mode, new_tokens)
            self._subscribed_tokens.update(new_tokens)
            logger.info(f"Subscribed to {len(new_tokens)} tokens (total: {len(self._subscribed_tokens)})")

    def unsubscribe(self, tokens: list[int]) -> None:
        """Unsubscribe from instrument tokens."""
        if self._ticker:
            self._ticker.unsubscribe(tokens)
        self._subscribed_tokens -= set(tokens)

    def _on_ticks(self, ws: Any, ticks: list[dict]) -> None:
        """Callback: received tick data from WebSocket."""
        if not self._loop or not self._running:
            logger.warning(f"Dropping {len(ticks)} ticks: loop={self._loop is not None} running={self._running}")
            return

        self._last_tick_time = now_ist()
        if self._tick_log_count < 3:
            self._tick_log_count += 1
            # Typed tag — JSONL queries filter on tag="TICKER" instead of grepping
            # for "[TICKER]" in free text (DATA_RELIABILITY_PLAN §8.2).
            logger.info(
                "tick batch received",
                extra={
                    "tag": Tag.TICKER,
                    "batch_size": len(ticks),
                    "batch_number": self._tick_log_count,
                },
            )

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
        logger.info(
            "ticker connected",
            extra={"tag": Tag.TICKER, "subscribed_tokens": len(self._subscribed_tokens)},
        )
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
        """Callback: WebSocket disconnected.

        Does NOT publish CONNECTION_LOST — the heartbeat loop handles reconnect
        detection and notification to avoid duplicate/spammy Telegram alerts.
        """
        logger.warning(
            "ticker disconnected",
            extra={"tag": Tag.TICKER, "code": code, "reason": reason},
        )

    def _on_error(self, ws: Any, code: int, reason: str) -> None:
        """Callback: WebSocket error."""
        logger.error(
            "ticker error",
            extra={"tag": Tag.TICKER, "code": code, "reason": reason},
        )

    def _on_reconnect(self, ws: Any, attempts: int) -> None:
        """Callback: WebSocket reconnecting."""
        logger.info(
            "ticker reconnecting",
            extra={"tag": Tag.WS_RECONNECT, "attempt": attempts, "trigger": "auto"},
        )

    async def _heartbeat_loop(self) -> None:
        """Monitor tick flow — force reconnect if ticks stop arriving.

        Outside market hours: sleeps in 10s increments, reconnecting at 9:10 AM
        (5 min before open) to ensure a fresh connection before market open.
        During market hours: checks every HEARTBEAT_TIMEOUT seconds and reconnects
        immediately if no ticks received.
        """
        await asyncio.sleep(60)
        _market_open_reconnect_done = False

        while self._running:
            try:
                now = now_ist()
                hour, minute = now.hour, now.minute
                is_weekday = now.weekday() < 5
                is_market_hours = (
                    is_weekday
                    and ((hour == 9 and minute >= 15) or (9 < hour < 15) or (hour == 15 and minute <= 30))
                )
                # Reset the market-open flag after close
                if hour >= 15 and minute > 30:
                    _market_open_reconnect_done = False

                if not is_market_hours:
                    # Reconnect at 9:10 AM — 5 min before open, gives WebSocket time to stabilise.
                    # Window is 9:10-9:14 so the 10s sleep loop reliably catches it.
                    if is_weekday and hour == 9 and 10 <= minute < 15 and not _market_open_reconnect_done:
                        logger.info(f"9:{minute:02d} AM — forcing pre-market reconnect (5 min before open)")
                        await self._force_reconnect()
                        _market_open_reconnect_done = True
                    await asyncio.sleep(10)
                    continue

                # During market hours: check every HEARTBEAT_TIMEOUT seconds.
                # No grace period — if the 9:10 reconnect worked and market opened normally,
                # ticks arrive within seconds of 9:15 and elapsed stays < HEARTBEAT_TIMEOUT.
                # If ticks are absent, we should reconnect immediately — not wait 3 minutes.
                await asyncio.sleep(self.HEARTBEAT_TIMEOUT)
                if not self._running or not self._subscribed_tokens:
                    continue

                if self._last_tick_time:
                    elapsed = (now_ist() - self._last_tick_time).total_seconds()
                else:
                    elapsed = 999

                if elapsed > self.HEARTBEAT_TIMEOUT:
                    logger.warning(
                        "tick gap exceeded heartbeat — forcing reconnect",
                        extra={
                            "tag": Tag.WS_RECONNECT,
                            "elapsed_seconds": round(elapsed, 1),
                            "timeout_seconds": self.HEARTBEAT_TIMEOUT,
                            "trigger": "heartbeat",
                        },
                    )
                    event = Event.create(
                        EventType.CONNECTION_LOST,
                        source="ticker_heartbeat",
                        reason=f"No ticks for {elapsed:.0f}s",
                    )
                    await self._event_bus.publish(event)
                    await self._force_reconnect()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in heartbeat loop")

    def update_token(self, access_token: str) -> None:
        """Update access token for next reconnect."""
        self._access_token = access_token
        logger.info("Ticker access token updated")

    async def _force_reconnect(self) -> None:
        """Close and reopen the WebSocket connection."""
        try:
            # Read latest token from file (auto-auth may have refreshed it)
            from pathlib import Path
            token_file = Path(".kite_access_token")
            if token_file.exists():
                fresh_token = token_file.read_text().strip()
                if fresh_token and fresh_token != self._access_token:
                    self._access_token = fresh_token
                    logger.info("Ticker picked up fresh token from .kite_access_token")

            if self._ticker:
                logger.info("Force-closing ticker for reconnect...")
                self._ticker.close()
                await asyncio.sleep(2)

            self._ticker = KiteTicker(self._api_key, self._access_token)
            self._ticker.on_ticks = self._on_ticks
            self._ticker.on_connect = self._on_connect
            self._ticker.on_close = self._on_close
            self._ticker.on_error = self._on_error
            self._ticker.on_reconnect = self._on_reconnect

            await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._ticker.connect(threaded=True)
            )
            logger.info("Ticker reconnected — waiting for ticks")
        except Exception:
            logger.exception("Failed to force reconnect ticker")

    def _parse_tick(self, tick_data: dict) -> Tick:
        """Parse raw Kite tick data into our Tick model."""
        depth = tick_data.get("depth", {})
        buy_depth = depth.get("buy", [{}])
        sell_depth = depth.get("sell", [{}])

        return Tick(
            instrument_token=tick_data["instrument_token"],
            tradingsymbol=tick_data.get("tradingsymbol", ""),
            timestamp=tick_data.get("exchange_timestamp", now_ist()),
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
