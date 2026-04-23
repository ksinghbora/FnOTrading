"""Backtesting engine — GDFL real-tick replay.

Uses the same components as live trading (TickFeedManager, OptionChainBuilder,
PaperBrokerClient, PortfolioManager) but drives them synchronously via direct
method calls instead of the EventBus. Strategies run identically through the
same StrategyContext interface.

Requires a :class:`GDFLMarketSource` (or equivalent) to drive market data;
the legacy synthetic Black-Scholes data-generation path has been removed.
For shared infrastructure (``BacktestClock``, ``import_strategies``,
``trading_days``) see :mod:`src.backtest.common`.

Usage::

    from src.backtest.engine import BacktestEngine
    from src.backtest.gdfl_market_source import GDFLMarketSource

    engine = BacktestEngine()
    market_source = GDFLMarketSource(parquet_dir, "NIFTY", NIFTY_SPOT_TOKEN)
    result = await engine.run("portfolio", market_source=market_source)
"""

import logging
import math
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import numpy as np
import pytz

from src.backtest.common import (
    BacktestClock,
    _FUT_TOKEN,
    _NUM_STRIKES,
    _SPOT_TOKENS,
    _STRIKE_STEPS,
    _TOKEN_BASE,
    _fit_daily_skew,
    _register_options,
    _update_market,
    import_strategies,
    trading_days,
)
from src.backtest.metrics import calculate_metrics
from src.broker.paper.client import PaperBrokerClient
from src.core.constants import INDIA_VIX_TOKEN, LOT_SIZES
from src.core.events import EventBus
from src.core.models import Order, Tick
from src.core.types import OrderStatus, ProductType
from src.market_data.aggregator import OHLCAggregator
from src.market_data.feed import TickFeedManager
from src.market_data.option_chain import OptionChainBuilder
from src.market_data.simulator import NIFTY_SPOT_TOKEN
from src.options.skew import ParametricSkew
from src.portfolio.charges import calculate_charges
from src.portfolio.manager import PortfolioManager
from src.strategy.context import StrategyContext
from src.strategy.registry import create_strategy

# ─── Backward-compat private aliases ───────────────────────────────
# A few modules we intentionally don't touch in Phase 1 still import
# these under their legacy private names from ``src.backtest.engine``.
# Keep the aliases so those imports continue to resolve; the helpers
# themselves now live in ``src.backtest.common``.
_import_strategies = import_strategies
_trading_days = trading_days

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

_DEFAULT_SPOTS = {"NIFTY": 22500.0, "BANKNIFTY": 48000.0}


class BacktestEngine:
    """Replays synthetic market data through any registered strategy."""

    async def run(
        self,
        strategy_name: str,
        strategy_id: str = "",
        strategy_params: dict | None = None,
        num_days: int = 30,
        start_date: date | None = None,
        initial_capital: float = 1_000_000,
        seed: int = 42,
        tick_interval_minutes: int = 1,
        fat_tails: bool = False,
        market_source=None,
    ) -> dict:
        """Run a backtest with synthetic data (or real GDFL data if market_source given).

        Args:
            strategy_name: Registered strategy name (e.g., 'short_straddle').
            strategy_id: Unique ID (auto-generated if empty).
            strategy_params: Strategy parameter overrides.
            num_days: Number of trading days to simulate.
            start_date: First trading day (defaults to ~45 days ago).
            initial_capital: Starting capital.
            seed: Random seed for reproducibility.
            tick_interval_minutes: Minutes between ticks (1=accurate, 5=fast).
            fat_tails: Use Student-t(df=5) instead of Gaussian for returns (heavier tails).
            market_source: Optional GDFLMarketSource. When provided, real tick
                data drives the backtest instead of synthetic BS prices. Trading
                days come from the parquet index unless start_date is explicit.

        Returns:
            Dict with metrics, daily_results, equity_curve, trades.
        """
        strategy_id = strategy_id or f"{strategy_name}_bt"
        params = strategy_params or {}
        underlying = params.get("underlying", "NIFTY")
        start_date = start_date or (date.today() - timedelta(days=45))

        # Ensure strategy modules are registered
        _import_strategies()

        # ─── Create infrastructure ────────────────────────────────
        event_bus = EventBus()  # Not started — only for constructor params
        clock = BacktestClock()
        feed = TickFeedManager(event_bus)
        aggregator = OHLCAggregator(event_bus)
        chain_builder = OptionChainBuilder(event_bus, clock)

        # Quote provider for the paper broker (F2).
        # Only wired when we have a real market_source (GDFL). Returns
        # (bid, ask) for a tradingsymbol by reverse-looking-up the
        # instrument token via chain_builder's symbol_map and reading
        # the latest tick's bid/ask. When the paper broker sees
        # non-zero bid/ask it fills BUY @ ask and SELL @ bid, mirroring
        # how live Kite market orders cross the spread. For the
        # synthetic BS path we leave quote_provider=None so the tiered
        # SlippageModel (premium tier × time × VIX × size) is used as
        # before — BS bid/ask are cosmetic ±1% placeholders and not a
        # realistic fill model.
        quote_provider = None
        if market_source is not None:
            # Build reverse symbol → token map lazily; the chain
            # builder populates _symbol_map as options are registered.
            # Cache the inverse and refresh when we see a symbol we
            # don't know yet (new strikes appear daily in GDFL).
            _sym_to_token: dict[str, int] = {}

            def _resolve_token(tradingsymbol: str) -> int | None:
                tok = _sym_to_token.get(tradingsymbol)
                if tok is not None:
                    return tok
                for t, s in chain_builder._symbol_map.items():
                    if s == tradingsymbol:
                        _sym_to_token[tradingsymbol] = t
                        return t
                return None

            def _quote_provider(tradingsymbol: str) -> tuple[float | None, float | None]:
                tok = _resolve_token(tradingsymbol)
                if tok is None:
                    return None, None
                tick = feed._latest_ticks.get(tok)
                if tick is None:
                    return None, None
                bid = float(tick.bid_price) if tick.bid_price else 0.0
                ask = float(tick.ask_price) if tick.ask_price else 0.0
                if bid <= 0 or ask <= 0 or ask < bid:
                    return None, None
                return bid, ask

            quote_provider = _quote_provider

        broker = PaperBrokerClient(
            initial_capital=initial_capital,
            quote_provider=quote_provider,
        )
        portfolio = PortfolioManager(event_bus, broker, chain_builder)

        # IV skew — default NIFTY-typical. Refreshed daily from observed
        # chain IVs via `_fit_daily_skew` if chain has ≥5 valid IV points.
        skew_model: ParametricSkew = ParametricSkew.nifty_typical()

        await broker.connect()

        # ─── Create strategy ──────────────────────────────────────
        strategy = create_strategy(strategy_name, strategy_id, params)

        # ─── Trading days ─────────────────────────────────────────
        if market_source is not None:
            available = market_source.available_days()
            if not available:
                return {"error": "GDFL market_source has no available days"}
            if start_date is not None:
                available = [d for d in available if d >= start_date]
            trading_days = available[:num_days]
            if not trading_days:
                return {"error": "No GDFL days in requested range"}
        else:
            trading_days = _trading_days(start_date, num_days, clock)
            if not trading_days:
                return {"error": "No trading days in range"}

        # ─── Market setup ─────────────────────────────────────────
        spot_token = _SPOT_TOKENS.get(underlying, NIFTY_SPOT_TOKEN)
        step = _STRIKE_STEPS.get(underlying, 50)
        spot = _DEFAULT_SPOTS.get(underlying, 22500.0)
        rng = np.random.default_rng(seed)
        # Fat tails: Student-t(df=5) scaled to match normal variance
        _T_DF = 5
        _T_SCALE = math.sqrt((_T_DF - 2) / _T_DF)  # ~0.775
        def _rand(sigma: float) -> float:
            if fat_tails:
                return float(rng.standard_t(_T_DF)) * _T_SCALE * sigma
            return float(rng.normal(0, sigma))
        vix = 14.5

        chain_builder.register_spot(spot_token, underlying)

        # Token allocator
        next_token = [_TOKEN_BASE]
        option_tokens: dict[tuple[str, float, str], int] = {}

        def alloc_token(ul: str, strike: float, ot: str) -> int:
            key = (ul, strike, ot)
            if key not in option_tokens:
                option_tokens[key] = next_token[0]
                next_token[0] += 1
            return option_tokens[key]

        # Initial expiry and options
        clock.set_time(IST.localize(datetime.combine(trading_days[0], time(9, 15))))
        if market_source is not None:
            # Load day 1 now so we can register options with REAL expiries/strikes
            market_source.load_day(trading_days[0])
            option_tokens = market_source.register_options(chain_builder, alloc_token)
            expiries = market_source.expiries_for_day()
            clock.set_expiries(underlying, expiries)
            expiry = expiries[0] if expiries else clock.next_expiry(underlying)
        else:
            expiry = clock.next_expiry(underlying)
            _register_options(chain_builder, underlying, spot, step, _NUM_STRIKES, expiry, alloc_token)

        # ─── Wire order callback ──────────────────────────────────
        # F2: No pre-slippage here. The paper broker applies the single
        # slippage source (cross-spread if quote_provider returns a real
        # bid/ask, else SlippageModel). Using the fill price reported
        # back by the broker keeps entry/exit bookkeeping consistent
        # with what the broker actually booked.
        async def order_callback(signal_obj):
            orders = []
            for leg in signal_obj.legs:
                ltp = feed.get_ltp(leg.instrument_token)
                price = float(leg.price) if float(leg.price) > 0 else float(ltp or 0)
                if price <= 0:
                    continue

                # Keep broker's LTP cache in sync with the theoretical
                # mark so its fallback path has a value to work with.
                broker.set_ltp(leg.tradingsymbol, price)

                order_id = await broker.place_order(
                    tradingsymbol=leg.tradingsymbol,
                    exchange="NFO",
                    side=leg.order_side,
                    quantity=leg.quantity,
                    order_type=leg.order_type,
                    price=price,
                )

                # Read back the actual fill price the broker booked so
                # positions, P&L and charges all use the real post-
                # slippage number (not the pre-slippage LTP).
                fill_price = price
                if broker._trades:
                    last_trade = broker._trades[-1]
                    if last_trade.get("order_id") == order_id:
                        fill_price = float(last_trade.get("average_price", price))

                order = Order(
                    broker_order_id=order_id,
                    strategy_id=signal_obj.strategy_id,
                    instrument_token=leg.instrument_token,
                    tradingsymbol=leg.tradingsymbol,
                    order_side=leg.order_side,
                    order_type=leg.order_type,
                    product=ProductType.NRML,
                    quantity=leg.quantity,
                    fill_price=Decimal(str(round(fill_price, 2))),
                    fill_quantity=leg.quantity,
                    status=OrderStatus.FILLED,
                )
                portfolio._positions.update_from_fill(order)

                inst = "CE" if "CE" in leg.tradingsymbol else (
                    "PE" if "PE" in leg.tradingsymbol else "FUT"
                )
                charges = calculate_charges(
                    Decimal(str(round(fill_price, 2))), leg.quantity, leg.order_side, inst
                )
                portfolio._pnl.add_charges(signal_obj.strategy_id, charges.total)
                orders.append(order_id)
            return orders

        def portfolio_getter(what, sid):
            if what == "positions":
                return portfolio.get_positions(sid)
            elif what == "pnl":
                return portfolio.get_pnl(sid)
            return None

        # ─── Wire strategy context ────────────────────────────────
        context = StrategyContext(
            strategy_id=strategy_id,
            feed=feed,
            option_chain_builder=chain_builder,
            aggregator=aggregator,
            clock=clock,
            order_callback=order_callback,
            portfolio_getter=portfolio_getter,
        )
        strategy.set_context(context)
        feed.subscribe([spot_token, INDIA_VIX_TOKEN], strategy_id)

        await strategy.on_start()

        # ─── Main replay loop ─────────────────────────────────────
        equity_curve = []
        daily_results = []
        running_pnl = Decimal("0")

        logger.info(
            f"[BACKTEST] Starting: strategy={strategy_name} underlying={underlying} "
            f"days={num_days} capital={initial_capital:,.0f}"
        )

        for day_idx, day in enumerate(trading_days):
            # Day boundary reset (except first day)
            if day_idx > 0:
                strategy.reset_day_state()
                portfolio.reset_daily()
                # Clear intraday candle builders so morning range is fresh
                aggregator._builders.clear()

            # Expiry rollover
            clock.set_time(IST.localize(datetime.combine(day, time(9, 15))))
            if market_source is not None:
                if day_idx > 0:  # day 0 already loaded above
                    market_source.load_day(day)
                    new_tokens = market_source.register_options(chain_builder, alloc_token)
                    option_tokens.update(new_tokens)
                exps = market_source.expiries_for_day()
                if exps:
                    clock.set_expiries(underlying, exps)
                    expiry = exps[0]
            else:
                new_expiry = clock.next_expiry(underlying)
                if new_expiry != expiry:
                    expiry = new_expiry
                    _register_options(
                        chain_builder, underlying, spot, step, _NUM_STRIKES, expiry, alloc_token
                    )

            daily_drift = _rand(0.003)
            day_open = spot
            day_trades_start = len(broker._trades)

            # Attempt to refit skew from yesterday's observed chain IVs.
            # Falls back to nifty_typical() if chain has <5 valid IVs.
            skew_model = _fit_daily_skew(chain_builder, underlying, expiry, spot, skew_model)

            # Minute-by-minute replay
            start_dt = datetime.combine(day, time(9, 15))

            for i in range(0, 375, tick_interval_minutes):
                now = IST.localize(start_dt + timedelta(minutes=i))
                clock.set_time(now)

                if market_source is not None:
                    # Real tick data path — market_source writes all caches
                    gdfl_spot, gdfl_vix = market_source.apply(
                        now, feed, broker, chain_builder, portfolio, option_tokens,
                    )
                    if gdfl_spot is None:
                        continue  # No data this minute (pre-open, holiday gap)
                    spot = gdfl_spot
                    vix = gdfl_vix if gdfl_vix is not None else vix
                else:
                    # Synthetic path — unchanged
                    vol_mult = 1.0 + 0.5 * (
                        math.exp(-i / 30) + math.exp(-(375 - i) / 30)
                    )
                    ret = daily_drift / 375 + _rand(0.0003) * vol_mult
                    spot *= (1 + ret)

                    vix += 0.02 * (14.5 - vix) + float(rng.normal(0, 0.15))
                    vix = max(8.0, min(40.0, vix))

                    T = max(
                        1 / (365 * 24),
                        (expiry - day).days / 365 - i / (375 * 365),
                    )
                    iv_base = vix / 100

                    _update_market(
                        feed, broker, chain_builder, portfolio,
                        underlying, expiry, spot, vix, now,
                        spot_token, step, _NUM_STRIKES, T, iv_base,
                        option_tokens, alloc_token,
                        skew_model=skew_model,
                    )

                # Dispatch to strategy
                spot_tick = Tick.model_construct(
                    instrument_token=spot_token,
                    tradingsymbol=underlying,
                    timestamp=now,
                    ltp=Decimal(str(round(spot, 2))),
                    volume=0, oi=0,
                    bid_price=Decimal("0"), ask_price=Decimal("0"),
                    bid_qty=0, ask_qty=0,
                    high=Decimal("0"), low=Decimal("0"),
                    open=Decimal("0"), close=Decimal("0"),
                )
                # Feed spot tick into candle aggregator so breakout / M5
                # indicators receive real OHLC data (else trend leg never fires).
                aggregator.process_tick_direct(spot_tick)

                try:
                    signal = await strategy.on_tick(spot_tick)
                    if signal:
                        await order_callback(signal)
                except Exception as e:
                    logger.debug(f"[BACKTEST] Strategy tick error: {e}")

            # ─── Day end ──────────────────────────────────────────
            pnl = portfolio.get_pnl(strategy_id)
            day_pnl = pnl.net
            running_pnl += day_pnl
            day_trades = len(broker._trades) - day_trades_start

            daily_results.append({
                "date": day.isoformat(),
                "day_of_week": day.strftime("%A"),
                "spot_open": round(day_open, 2),
                "spot_close": round(spot, 2),
                "pnl": round(float(day_pnl), 2),
                "charges": round(float(pnl.charges), 2),
                "trades": day_trades,
                "equity": round(initial_capital + float(running_pnl), 2),
            })
            equity_curve.append({
                "date": day.isoformat(),
                "equity": round(initial_capital + float(running_pnl), 2),
                "pnl": round(float(day_pnl), 2),
            })

            if day_idx % 5 == 0 or day_idx == len(trading_days) - 1:
                logger.info(
                    f"[BACKTEST] Day {day_idx + 1}/{num_days}: {day} "
                    f"spot={spot:.0f} pnl={float(day_pnl):+.0f} "
                    f"cumulative={float(running_pnl):+.0f} trades={day_trades}"
                )

        # ─── Finalize ─────────────────────────────────────────────
        await strategy.on_stop()

        pnl_values = [d["pnl"] for d in daily_results]
        metrics = calculate_metrics(pnl_values, broker._trades, initial_capital)
        metrics["total_charges"] = round(sum(d["charges"] for d in daily_results), 2)
        metrics["num_days"] = num_days

        result = {
            "strategy": strategy_name,
            "strategy_id": strategy_id,
            "underlying": underlying,
            "period": f"{trading_days[0]} to {trading_days[-1]}",
            "num_days": num_days,
            "lots": params.get("quantity_lots", 1),
            "lot_size": LOT_SIZES.get(underlying, 75),
            "initial_capital": initial_capital,
            "params": strategy.params.model_dump(mode="json"),
            "metrics": metrics,
            "daily_results": daily_results,
            "equity_curve": equity_curve,
            "final_pnl": round(float(running_pnl), 2),
        }

        logger.info(
            f"[BACKTEST] Complete: {strategy_name} {num_days}d "
            f"P&L={float(running_pnl):+,.0f} "
            f"Sharpe={metrics.get('sharpe_ratio', 0):.2f} "
            f"WinRate={metrics.get('win_rate', 0):.1f}%"
        )

        return result
