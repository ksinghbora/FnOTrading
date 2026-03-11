"""Immutable market snapshot — point-in-time freeze of market state."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from src.core.models import OHLC, OptionChain, Tick
from src.core.types import Timeframe


@dataclass(frozen=True)
class MarketSnapshot:
    """Immutable point-in-time market state for strategy decision-making.

    Strategies receive a snapshot so they cannot accidentally modify
    shared market data state.
    """

    timestamp: datetime
    spot_prices: dict[str, Decimal]  # underlying -> LTP
    ticks: dict[int, Tick]  # instrument_token -> latest tick
    option_chains: dict[tuple[str, date], OptionChain]  # (underlying, expiry) -> chain
    candles: dict[tuple[int, Timeframe], list[OHLC]]  # (token, timeframe) -> recent candles

    def get_spot(self, underlying: str) -> Decimal:
        return self.spot_prices.get(underlying, Decimal("0"))

    def get_tick(self, instrument_token: int) -> Tick | None:
        return self.ticks.get(instrument_token)

    def get_ltp(self, instrument_token: int) -> Decimal:
        tick = self.ticks.get(instrument_token)
        return tick.ltp if tick else Decimal("0")

    def get_chain(self, underlying: str, expiry: date) -> OptionChain | None:
        return self.option_chains.get((underlying, expiry))

    def get_candles(
        self, instrument_token: int, timeframe: Timeframe, limit: int = 50
    ) -> list[OHLC]:
        candles = self.candles.get((instrument_token, timeframe), [])
        return candles[-limit:]
