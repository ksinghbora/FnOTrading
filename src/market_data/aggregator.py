"""OHLC candle aggregation from tick data."""

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from src.core.events import Event, EventBus, EventType
from src.core.models import OHLC, Tick
from src.core.types import Timeframe

logger = logging.getLogger(__name__)

# Timeframe durations in seconds
TIMEFRAME_SECONDS = {
    Timeframe.M1: 60,
    Timeframe.M3: 180,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.M30: 1800,
    Timeframe.H1: 3600,
    Timeframe.D1: 86400,
}


class CandleBuilder:
    """Builds a single candle from ticks."""

    def __init__(self, instrument_token: int, tradingsymbol: str, timeframe: Timeframe):
        self.instrument_token = instrument_token
        self.tradingsymbol = tradingsymbol
        self.timeframe = timeframe
        self.open: Decimal | None = None
        self.high: Decimal = Decimal("-Infinity")
        self.low: Decimal = Decimal("Infinity")
        self.close: Decimal = Decimal("0")
        self.volume: int = 0
        self.oi: int = 0
        self.start_time: datetime | None = None
        self._tick_count = 0
        self._first_cumulative_volume: int | None = None  # Kite sends cumulative volume

    def update(self, tick: Tick) -> None:
        """Update candle with a new tick."""
        if self.open is None:
            self.open = tick.ltp
            self.start_time = self._align_time(tick.timestamp)
            self._first_cumulative_volume = tick.volume

        self.high = max(self.high, tick.ltp)
        self.low = min(self.low, tick.ltp)
        self.close = tick.ltp
        # Kite sends cumulative day volume; compute delta for this candle
        if self._first_cumulative_volume is not None:
            self.volume = max(0, tick.volume - self._first_cumulative_volume)
        self.oi = tick.oi
        self._tick_count += 1

    def to_ohlc(self) -> OHLC | None:
        """Convert to OHLC model. Returns None if no ticks received."""
        if self.open is None or self.start_time is None:
            return None
        return OHLC(
            instrument_token=self.instrument_token,
            tradingsymbol=self.tradingsymbol,
            timestamp=self.start_time,
            timeframe=self.timeframe,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            oi=self.oi,
        )

    def reset(self) -> None:
        """Reset for next candle."""
        self.open = None
        self.high = Decimal("-Infinity")
        self.low = Decimal("Infinity")
        self.close = Decimal("0")
        self.volume = 0
        self.oi = 0
        self.start_time = None
        self._tick_count = 0
        self._first_cumulative_volume = None

    def _align_time(self, dt: datetime) -> datetime:
        """Align timestamp to candle boundary."""
        seconds = TIMEFRAME_SECONDS[self.timeframe]
        ts = int(dt.timestamp())
        aligned = ts - (ts % seconds)
        return datetime.fromtimestamp(aligned, tz=dt.tzinfo)


class OHLCAggregator:
    """Aggregates ticks into OHLC candles at multiple timeframes.

    Listens for TICK events, builds candles, and publishes CANDLE_CLOSED events.
    """

    MAX_COMPLETED_CANDLES = 10_000  # Cap to prevent unbounded memory growth

    def __init__(
        self,
        event_bus: EventBus,
        timeframes: list[Timeframe] | None = None,
    ):
        self._event_bus = event_bus
        self._timeframes = timeframes or [Timeframe.M1, Timeframe.M5, Timeframe.M15]
        # {(instrument_token, timeframe) -> CandleBuilder}
        self._builders: dict[tuple[int, Timeframe], CandleBuilder] = {}
        self._completed_candles: list[OHLC] = []

        self._event_bus.subscribe(EventType.TICK, self._on_tick)

    async def _on_tick(self, event: Event) -> None:
        """Handle tick event — update all candle builders."""
        tick_data = event.payload.get("tick")
        if not tick_data:
            return

        tick = Tick(**tick_data)

        for tf in self._timeframes:
            key = (tick.instrument_token, tf)

            if key not in self._builders:
                self._builders[key] = CandleBuilder(
                    tick.instrument_token, tick.tradingsymbol, tf
                )

            builder = self._builders[key]

            # Check if we've crossed into a new candle period
            if builder.start_time is not None:
                seconds = TIMEFRAME_SECONDS[tf]
                candle_end = builder.start_time + timedelta(seconds=seconds)
                if tick.timestamp >= candle_end:
                    # Close current candle
                    tick_count = builder._tick_count
                    ohlc = builder.to_ohlc()
                    if ohlc:
                        self._completed_candles.append(ohlc)
                        logger.debug(
                            f"[CANDLE] symbol={ohlc.tradingsymbol} "
                            f"tf={tf.value} O={ohlc.open} H={ohlc.high} "
                            f"L={ohlc.low} C={ohlc.close} vol={ohlc.volume} "
                            f"ticks={tick_count}"
                        )
                        # Trim oldest candles to prevent unbounded memory growth
                        if len(self._completed_candles) > self.MAX_COMPLETED_CANDLES:
                            self._completed_candles = self._completed_candles[-self.MAX_COMPLETED_CANDLES:]
                        await self._event_bus.publish(
                            Event.create(
                                EventType.CANDLE_CLOSED,
                                source="aggregator",
                                candle=ohlc.model_dump(),
                            )
                        )
                    builder.reset()

            builder.update(tick)

    def process_tick_direct(self, tick: Tick) -> None:
        """Process a tick directly without EventBus (for backtesting).

        Same logic as _on_tick but synchronous and without publishing events.
        """
        for tf in self._timeframes:
            key = (tick.instrument_token, tf)

            if key not in self._builders:
                self._builders[key] = CandleBuilder(
                    tick.instrument_token, tick.tradingsymbol, tf
                )

            builder = self._builders[key]

            if builder.start_time is not None:
                seconds = TIMEFRAME_SECONDS[tf]
                candle_end = builder.start_time + timedelta(seconds=seconds)
                if tick.timestamp >= candle_end:
                    ohlc = builder.to_ohlc()
                    if ohlc:
                        self._completed_candles.append(ohlc)
                        if len(self._completed_candles) > self.MAX_COMPLETED_CANDLES:
                            self._completed_candles = self._completed_candles[-self.MAX_COMPLETED_CANDLES:]
                    builder.reset()

            builder.update(tick)

    def get_latest_candle(
        self, instrument_token: int, timeframe: Timeframe
    ) -> OHLC | None:
        """Get the current (building) candle."""
        key = (instrument_token, timeframe)
        builder = self._builders.get(key)
        if builder:
            return builder.to_ohlc()
        return None

    def get_completed_candles(
        self,
        instrument_token: int | None = None,
        timeframe: Timeframe | None = None,
        limit: int = 100,
    ) -> list[OHLC]:
        """Get recent completed candles, optionally filtered."""
        candles = self._completed_candles
        if instrument_token is not None:
            candles = [c for c in candles if c.instrument_token == instrument_token]
        if timeframe is not None:
            candles = [c for c in candles if c.timeframe == timeframe]
        return candles[-limit:]

    def clear_day(self) -> None:
        """Reset all per-day state at the trading-day boundary.

        Both the in-progress builders **and** the completed-candle history
        are wiped. Apr 25 2026 audit (``memory/validation_audit_apr25.md``)
        found that callers like ``BaseStrategy._move_from_open_pct`` and
        ``momentum_breakout`` take ``candles[:N]`` (oldest of a limit-N
        slice) assuming those are today's first candles. Without clearing
        the completed list at day start, the slice mixes yesterday's
        late-afternoon candles with today's first — silently corrupting
        ``move_from_open_pct``, the trend filter, the trend leg's morning
        range, and any feature that asks "what's today's range so far?"

        Engine callers should invoke this in the day-boundary reset block
        BEFORE the first tick of the new day is processed.
        """
        self._builders.clear()
        self._completed_candles.clear()
