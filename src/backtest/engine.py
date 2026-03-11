"""Backtesting engine — replays historical data through strategies."""

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

from src.core.clock import MarketClock
from src.core.events import EventBus
from src.core.models import Tick
from src.broker.paper.client import PaperBrokerClient
from src.market_data.aggregator import OHLCAggregator
from src.market_data.feed import TickFeedManager
from src.market_data.option_chain import OptionChainBuilder
from src.portfolio.manager import PortfolioManager
from src.strategy.base import BaseStrategy
from src.strategy.context import StrategyContext
from src.backtest.metrics import calculate_metrics

logger = logging.getLogger(__name__)


class BacktestEngine:
    """Replays historical data through a strategy using the same interfaces as live trading.

    The same BaseStrategy subclass runs identically in backtest and live mode —
    only the data source and broker differ.
    """

    def __init__(self):
        self._event_bus = EventBus()
        self._clock = MarketClock()
        self._broker = PaperBrokerClient()
        self._feed = TickFeedManager(self._event_bus)
        self._aggregator = OHLCAggregator(self._event_bus)
        self._chain_builder = OptionChainBuilder(self._event_bus, self._clock)
        self._portfolio = PortfolioManager(self._event_bus, self._broker)

    async def run(
        self,
        strategy: BaseStrategy,
        historical_data: list[dict],
        initial_capital: float = 1_000_000,
    ) -> dict:
        """Run a backtest.

        Args:
            strategy: Strategy instance to test.
            historical_data: List of OHLC dicts with keys:
                date, open, high, low, close, volume, oi (optional).
            initial_capital: Starting capital.

        Returns:
            Dict with results, metrics, trades, and equity curve.
        """
        self._broker = PaperBrokerClient(initial_capital=initial_capital)
        self._portfolio = PortfolioManager(self._event_bus, self._broker)
        await self._broker.connect()
        await self._event_bus.start()

        # Create context
        async def order_callback(signal_obj):
            from src.core.models import OrderRequest
            orders = []
            for leg in signal_obj.legs:
                req = OrderRequest(
                    strategy_id=signal_obj.strategy_id,
                    instrument_token=leg.instrument_token,
                    tradingsymbol=leg.tradingsymbol,
                    order_side=leg.order_side,
                    order_type=leg.order_type,
                    quantity=leg.quantity,
                    price=leg.price,
                )
                from src.oms.executor import OrderExecutor
                # In backtest, execute directly through paper broker
                self._broker.set_ltp(leg.tradingsymbol, float(leg.price) or float(self._feed.get_ltp(leg.instrument_token) or 0))
                order = await self._broker.place_order(
                    tradingsymbol=leg.tradingsymbol,
                    exchange="NFO",
                    side=leg.order_side,
                    quantity=leg.quantity,
                    price=float(leg.price),
                )
                orders.append(order)
            return orders

        def portfolio_getter(what, sid):
            if what == "positions":
                return self._portfolio.get_positions(sid)
            elif what == "pnl":
                return self._portfolio.get_pnl(sid)
            return None

        context = StrategyContext(
            strategy_id=strategy.strategy_id,
            feed=self._feed,
            option_chain_builder=self._chain_builder,
            aggregator=self._aggregator,
            clock=self._clock,
            order_callback=order_callback,
            portfolio_getter=portfolio_getter,
        )
        strategy.set_context(context)

        # Start strategy
        await strategy.on_start()

        # Replay data
        equity_curve = []
        for candle in historical_data:
            # Simulate tick from OHLC
            tick = Tick(
                instrument_token=0,
                tradingsymbol=strategy.params.underlying,
                timestamp=candle.get("date", datetime.now()),
                ltp=Decimal(str(candle["close"])),
                volume=candle.get("volume", 0),
                oi=candle.get("oi", 0),
                open=Decimal(str(candle["open"])),
                high=Decimal(str(candle["high"])),
                low=Decimal(str(candle["low"])),
                close=Decimal(str(candle["close"])),
            )

            self._broker.set_ltp(strategy.params.underlying, float(tick.ltp))

            signal = await strategy.on_tick(tick)
            if signal:
                await order_callback(signal)

            # Record equity
            pnl = self._portfolio.get_pnl()
            equity_curve.append({
                "timestamp": str(candle.get("date", "")),
                "pnl": float(pnl.net),
            })

        # Stop strategy
        await strategy.on_stop()
        await self._event_bus.stop()

        # Calculate metrics
        trades = await self._broker.get_trades()
        pnl_values = [e["pnl"] for e in equity_curve]
        metrics = calculate_metrics(pnl_values, trades, initial_capital)

        return {
            "strategy_id": strategy.strategy_id,
            "params": strategy.params.model_dump(),
            "metrics": metrics,
            "trades": trades,
            "equity_curve": equity_curve,
            "final_pnl": float(self._portfolio.get_pnl().net),
        }
