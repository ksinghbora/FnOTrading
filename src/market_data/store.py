"""Persistence layer for tick and OHLC data to TimescaleDB."""

import logging
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.events import Event, EventBus, EventType
from src.core.models import OHLC, Tick

logger = logging.getLogger(__name__)


class MarketDataStore:
    """Persists market data to TimescaleDB.

    Uses raw SQL inserts for performance (bypassing ORM overhead
    on the hot path of tick ingestion).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], event_bus: EventBus):
        self._session_factory = session_factory
        self._event_bus = event_bus
        self._tick_buffer: list[Tick] = []
        self._candle_buffer: list[OHLC] = []
        self._buffer_size = 100  # Flush every N ticks

        self._event_bus.subscribe(EventType.TICK, self._on_tick)
        self._event_bus.subscribe(EventType.CANDLE_CLOSED, self._on_candle)

    async def _on_tick(self, event: Event) -> None:
        """Buffer ticks and flush in batches for performance."""
        tick_data = event.payload.get("tick")
        if not tick_data:
            return

        tick = Tick(**tick_data)
        self._tick_buffer.append(tick)

        if len(self._tick_buffer) >= self._buffer_size:
            await self._flush_ticks()

    async def _on_candle(self, event: Event) -> None:
        """Store completed candles immediately."""
        candle_data = event.payload.get("candle")
        if not candle_data:
            return

        ohlc = OHLC(**candle_data)
        await self._store_candle(ohlc)

    async def _flush_ticks(self) -> None:
        """Batch insert buffered ticks into TimescaleDB."""
        if not self._tick_buffer:
            return

        ticks = self._tick_buffer.copy()

        try:
            async with self._session_factory() as session:
                sql = text(
                    "INSERT INTO ticks (time, instrument_token, tradingsymbol, "
                    "ltp, volume, oi, bid_price, ask_price, bid_qty, ask_qty) "
                    "VALUES (:time, :token, :symbol, :ltp, :volume, :oi, "
                    ":bid_price, :ask_price, :bid_qty, :ask_qty) "
                    "ON CONFLICT DO NOTHING"
                )
                params = [
                    {
                        "time": t.timestamp.isoformat(),
                        "token": t.instrument_token,
                        "symbol": t.tradingsymbol,
                        "ltp": float(t.ltp),
                        "volume": t.volume,
                        "oi": t.oi,
                        "bid_price": float(t.bid_price),
                        "ask_price": float(t.ask_price),
                        "bid_qty": t.bid_qty,
                        "ask_qty": t.ask_qty,
                    }
                    for t in ticks
                ]
                if params:
                    await session.execute(sql, params)
                    await session.commit()
            # Clear only after successful commit
            self._tick_buffer = self._tick_buffer[len(ticks):]
        except Exception:
            logger.exception(f"Failed to flush {len(ticks)} ticks — will retry next cycle")

    async def _store_candle(self, ohlc: OHLC) -> None:
        """Insert a completed candle into TimescaleDB."""
        try:
            async with self._session_factory() as session:
                sql = text(
                    "INSERT INTO candles (time, instrument_token, timeframe, tradingsymbol, "
                    "open, high, low, close, volume, oi) "
                    "VALUES (:time, :token, :tf, :symbol, :o, :h, :l, :c, :v, :oi) "
                    "ON CONFLICT DO NOTHING"
                )
                await session.execute(
                    sql,
                    {
                        "time": ohlc.timestamp,
                        "token": ohlc.instrument_token,
                        "tf": ohlc.timeframe.value,
                        "symbol": ohlc.tradingsymbol,
                        "o": float(ohlc.open),
                        "h": float(ohlc.high),
                        "l": float(ohlc.low),
                        "c": float(ohlc.close),
                        "v": ohlc.volume,
                        "oi": ohlc.oi,
                    },
                )
                await session.commit()
        except Exception:
            logger.exception("Failed to store candle")

    async def flush(self) -> None:
        """Force flush all buffers (call on shutdown)."""
        await self._flush_ticks()
