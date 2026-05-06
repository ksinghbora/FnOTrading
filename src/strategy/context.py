"""Strategy context — controlled access to system services for strategies."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.core.clock import MarketClock
from src.core.models import OHLC, OptionChain, OrderRequest, PnL, Position, Signal, SignalLeg, Tick
from src.core.types import OrderSide, OrderType, ProductType
from src.market_data.aggregator import OHLCAggregator
from src.market_data.feed import TickFeedManager
from src.market_data.option_chain import OptionChainBuilder
from src.core.types import Timeframe


class StrategyContext:
    """Provides strategies with controlled access to system services.

    Strategies interact with the system exclusively through this context.
    This ensures:
    - All orders go through risk checks
    - Strategies can't directly modify shared state
    - Dependencies are clearly defined
    """

    def __init__(
        self,
        strategy_id: str,
        feed: TickFeedManager,
        option_chain_builder: OptionChainBuilder,
        aggregator: OHLCAggregator,
        clock: MarketClock,
        order_callback,  # Callable that routes signal through OMS
        portfolio_getter,  # Callable that returns positions/PnL
        historical_data_callback=None,  # async (token, from, to, interval) -> list[dict]
    ):
        self.strategy_id = strategy_id
        self._feed = feed
        self._chain_builder = option_chain_builder
        self._aggregator = aggregator
        self._clock = clock
        self._order_callback = order_callback
        self._portfolio_getter = portfolio_getter
        # May 6 2026: optional historical-data fetcher used by strategies
        # that need to warm up state-dependent detectors (e.g. RegimeDetector
        # daily-close deque for VRP/RV) at startup. Strategies should treat
        # None as "warmup unavailable" and fall back to gradual in-memory
        # accumulation. Live mode wires this from broker.get_historical_data;
        # backtests/tests can leave it None.
        self._historical_data_callback = historical_data_callback

    # ─── Market Data ─────────────────────────────────────────────

    def get_ltp(self, instrument_token: int) -> Decimal:
        return self._feed.get_ltp(instrument_token) or Decimal("0")

    def get_tick(self, instrument_token: int) -> Tick | None:
        return self._feed.get_tick(instrument_token)

    def get_spot_price(self, underlying: str) -> Decimal:
        return self._chain_builder.get_spot_price(underlying)

    def get_option_chain(self, underlying: str, expiry: date) -> OptionChain | None:
        return self._chain_builder.get_chain(underlying, expiry)

    def get_available_expiries(self, underlying: str) -> list[date]:
        return self._chain_builder.get_all_expiries(underlying)

    def get_candles(
        self, instrument_token: int, timeframe: Timeframe, limit: int = 50
    ) -> list[OHLC]:
        return self._aggregator.get_completed_candles(instrument_token, timeframe, limit)

    def get_vix(self) -> float:
        """Get current India VIX value. Returns 0 if unavailable."""
        from src.core.constants import INDIA_VIX_TOKEN
        ltp = self._feed.get_ltp(INDIA_VIX_TOKEN)
        return float(ltp) if ltp else 0.0

    async def get_historical_data(
        self,
        instrument_token: int,
        from_date,
        to_date,
        interval: str,
    ) -> list[dict]:
        """Fetch historical bars via the wired broker callback.

        Returns ``[]`` if no callback is wired (paper backtests, unit
        tests). Strategies that depend on this for warmup should treat
        an empty result as "no warmup available, fall back to live
        accumulation" rather than as a hard failure.
        """
        if self._historical_data_callback is None:
            return []
        return await self._historical_data_callback(
            instrument_token, from_date, to_date, interval
        )

    def get_spot_token(self, underlying: str) -> int | None:
        """Reverse-lookup the spot token for an underlying. None if not registered."""
        for token, name in self._chain_builder._spot_tokens.items():
            if name == underlying:
                return token
        return None

    # ─── Clock ───────────────────────────────────────────────────

    @property
    def clock(self) -> MarketClock:
        return self._clock

    def is_market_open(self) -> bool:
        return self._clock.is_market_open()

    def next_expiry(self, underlying: str) -> date:
        return self._clock.next_expiry(underlying)

    def is_expiry_day(self, underlying: str) -> bool:
        return self._clock.is_expiry_day(underlying)

    # ─── Order Placement ─────────────────────────────────────────

    async def place_signal(self, signal: Signal) -> list:
        """Place a trading signal through the OMS (with risk checks)."""
        return await self._order_callback(signal)

    async def place_order(
        self,
        tradingsymbol: str,
        instrument_token: int,
        side: OrderSide,
        quantity: int,
        order_type: OrderType = OrderType.MARKET,
        price: float = 0,
    ) -> object:
        """Convenience method to place a single-leg order."""
        from src.core.models import Signal, SignalLeg
        from src.core.types import SignalType

        signal = Signal(
            strategy_id=self.strategy_id,
            signal_type=SignalType.ENTRY,
            legs=[
                SignalLeg(
                    tradingsymbol=tradingsymbol,
                    instrument_token=instrument_token,
                    order_side=side,
                    quantity=quantity,
                    order_type=order_type,
                    price=Decimal(str(price)),
                )
            ],
        )
        results = await self._order_callback(signal)
        return results[0] if results else None

    # ─── Portfolio ───────────────────────────────────────────────

    def get_positions(self) -> list[Position]:
        return self._portfolio_getter("positions", self.strategy_id)

    def get_open_positions(self) -> list[Position]:
        return [p for p in self.get_positions() if p.quantity != 0]

    def get_pnl(self) -> PnL:
        return self._portfolio_getter("pnl", self.strategy_id)

    def get_net_quantity(self, instrument_token: int) -> int:
        for pos in self.get_positions():
            if pos.instrument_token == instrument_token:
                return pos.quantity
        return 0
