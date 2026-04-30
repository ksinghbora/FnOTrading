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
from datetime import date, datetime, time, timedelta
from decimal import Decimal

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
from src.broker.paper.client import BookLevel, DepthQuote, PaperBrokerClient
from src.core.constants import INDIA_VIX_TOKEN, LOT_SIZES
from src.core.events import EventBus
from src.core.models import Order, Tick
from src.core.types import OrderStatus, ProductType
from src.market_data.aggregator import OHLCAggregator
from src.market_data.feed import TickFeedManager
from src.market_data.option_chain import OptionChainBuilder
from src.market_data.simulator import NIFTY_SPOT_TOKEN
from src.portfolio.charges import calculate_charges
from src.portfolio.manager import PortfolioManager
from src.strategy.context import StrategyContext
from src.strategy.registry import create_strategy

# ─── Backward-compat private aliases ───────────────────────────────
# A few modules we intentionally don't touch in Phase 1 still import
# these under their legacy private names from ``src.backtest.engine``.
# Keep the aliases (and the re-exports from common above) so those
# imports continue to resolve; the helpers themselves now live in
# ``src.backtest.common``.
_import_strategies = import_strategies
_trading_days = trading_days

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")


def _select_trading_days(
    available: list[date],
    days: list[date] | None,
    start_date: date | None,
    num_days: int,
) -> tuple[list[date], list[date]]:
    """Select trading days for a backtest run.

    Two modes:

    1. **Explicit list** — when ``days`` is given, return *exactly* those
       days (filtered to ``available``), preserving order **and gaps**.
       This is the audit-correct path for CPCV/non-contiguous splits.

    2. **Legacy slice** — when ``days`` is None, return the first
       ``num_days`` of ``available`` after applying ``start_date``.
       Contiguous; matches the pre-Apr-25-2026 behaviour for callers
       that just want "first N available days".

    Returns ``(trading_days, missing_days)``. ``missing_days`` is non-empty
    only when an explicit list contained dates that aren't in
    ``available`` — the engine logs them at WARNING but otherwise drops
    them so the run can proceed deterministically.
    """
    if days is not None:
        available_set = set(available)
        trading_days_out = [d for d in days if d in available_set]
        missing = [d for d in days if d not in available_set]
        return trading_days_out, missing

    filtered = available
    if start_date is not None:
        filtered = [d for d in filtered if d >= start_date]
    return filtered[:num_days], []

_DEFAULT_SPOTS = {"NIFTY": 22500.0, "BANKNIFTY": 48000.0}


class BacktestEngine:
    """Replays a real-tick ``market_source`` through any registered strategy."""

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
        market_source=None,
        days: list[date] | None = None,
    ) -> dict:
        """Run a real-tick backtest against ``market_source``.

        Args:
            strategy_name: Registered strategy name (e.g., 'portfolio').
            strategy_id: Unique ID (auto-generated if empty).
            strategy_params: Strategy parameter overrides.
            num_days: Number of trading days to simulate (capped by the
                number of days available in ``market_source``). **Ignored
                when ``days`` is provided** — kept for backward compat with
                callers that just want "first N available days".
            start_date: Optional first trading day. If given, skips any
                earlier days in ``market_source``. **Ignored when ``days``
                is provided.**
            initial_capital: Starting capital.
            seed: Random seed (kept for reproducibility of any stochastic
                strategy internals; the market replay itself is deterministic).
            tick_interval_minutes: Minutes between ticks (1=accurate, 5=fast).
            market_source: REQUIRED. A :class:`GDFLMarketSource` (or equivalent)
                providing ``available_days()``, ``load_day()``, ``apply()``,
                ``register_options()``, and ``expiries_for_day()``.
            days: Optional explicit list of trading days to replay. When
                supplied this overrides ``start_date`` + ``num_days`` and
                the engine iterates **exactly the given days in order** —
                including non-contiguous lists with gaps. Required for
                CPCV correctness (Apr 25 2026): without it, CPCV passes
                non-contiguous train indices like ``[0,1,5,6,7,…]`` and
                the engine silently runs the contiguous slice
                ``[0,1,2,3,4,5,6,7,…]``, leaking test days into train.
                Days must already be filtered to ``market_source.available_days()``;
                missing days are dropped with a warning.

        Raises:
            ValueError: if ``market_source`` is ``None``. The synthetic
                Black-Scholes data-generation path has been removed — see
                Phase 1 of the BS removal refactor.

        Returns:
            Dict with metrics, daily_results, equity_curve, trades.
        """
        if market_source is None:
            raise ValueError(
                "BacktestEngine.run() requires market_source — the synthetic "
                "Black-Scholes path has been removed. Pass a GDFLMarketSource "
                "(see scripts/run_gdfl_backtest.py for a worked example)."
            )

        strategy_id = strategy_id or f"{strategy_name}_bt"
        params = strategy_params or {}
        underlying = params.get("underlying", "NIFTY")

        # Ensure strategy modules are registered
        _import_strategies()

        # ─── Create infrastructure ────────────────────────────────
        event_bus = EventBus()  # Not started — only for constructor params
        clock = BacktestClock()
        feed = TickFeedManager(event_bus)
        aggregator = OHLCAggregator(event_bus)
        chain_builder = OptionChainBuilder(event_bus, clock)

        # Quote provider for the paper broker (F2).
        # Returns (bid, ask) for a tradingsymbol by reverse-looking-up
        # the instrument token via chain_builder's symbol_map and reading
        # the latest tick's bid/ask. When the paper broker sees non-zero
        # bid/ask it fills BUY @ ask and SELL @ bid, mirroring how live
        # Kite market orders cross the spread.
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

        def _depth_provider(tradingsymbol: str) -> DepthQuote | None:
            # GDFL exposes only top-of-book; wrap each side as a single
            # BookLevel so the broker can emit a book_snapshot with
            # bid_qty/ask_qty. Falls through to quote_provider (top-of-
            # book with tick-per-lot penalty) when qty is missing.
            tok = _resolve_token(tradingsymbol)
            if tok is None:
                return None
            tick = feed._latest_ticks.get(tok)
            if tick is None:
                return None
            bid = float(tick.bid_price) if tick.bid_price else 0.0
            ask = float(tick.ask_price) if tick.ask_price else 0.0
            if bid <= 0 or ask <= 0 or ask < bid:
                return None
            bid_qty = int(tick.bid_qty or 0)
            ask_qty = int(tick.ask_qty or 0)
            if bid_qty <= 0 and ask_qty <= 0:
                return None
            return DepthQuote(
                bid_levels=[BookLevel(price=bid, size=bid_qty)] if bid_qty > 0 else [],
                ask_levels=[BookLevel(price=ask, size=ask_qty)] if ask_qty > 0 else [],
            )

        broker = PaperBrokerClient(
            initial_capital=initial_capital,
            quote_provider=_quote_provider,
            depth_provider=_depth_provider,
        )
        portfolio = PortfolioManager(event_bus, broker, chain_builder)

        await broker.connect()

        # ─── Create strategy ──────────────────────────────────────
        strategy = create_strategy(strategy_name, strategy_id, params)

        # ─── Trading days (from market_source) ────────────────────
        available = market_source.available_days()
        if not available:
            return {"error": "GDFL market_source has no available days"}
        trading_days, missing = _select_trading_days(
            available=available, days=days, start_date=start_date, num_days=num_days,
        )
        if missing:
            logger.warning(
                "[BACKTEST] explicit days: %d of %d days missing in market_source — "
                "dropping (e.g. %s)",
                len(missing), len(days or []), missing[:3],
            )
        if not trading_days:
            return {"error": "No GDFL days in requested range"}

        # ─── Market setup ─────────────────────────────────────────
        spot_token = _SPOT_TOKENS.get(underlying, NIFTY_SPOT_TOKEN)
        step = _STRIKE_STEPS.get(underlying, 50)
        spot = _DEFAULT_SPOTS.get(underlying, 22500.0)
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

        # Initial expiry and options — load day 1 so we can register
        # options with REAL expiries/strikes from the market source.
        clock.set_time(IST.localize(datetime.combine(trading_days[0], time(9, 15))))
        market_source.load_day(trading_days[0])
        option_tokens = market_source.register_options(chain_builder, alloc_token)
        expiries = market_source.expiries_for_day()
        clock.set_expiries(underlying, expiries)
        expiry = expiries[0] if expiries else clock.next_expiry(underlying)

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

        # Apr 30 2026 Phase 4B: snapshot of cumulative ``pnl.realized``
        # at each day's START. realized_today = current_realized -
        # day_start_realized. Without this baseline, multi-day positions
        # double-count: on day N the running_pnl includes (N-1)'s MTM
        # plus today's MTM plus today's realized — three things,
        # summed across days. With it, daily_results carries a true
        # incremental realised P&L that the MC + bootstrap CI gates
        # can use without contamination from open-position price
        # swings that aren't actually money in the bank.
        day_start_realized: float = 0.0

        for day_idx, day in enumerate(trading_days):
            # Day boundary reset (except first day)
            if day_idx > 0:
                strategy.reset_day_state()
                portfolio.reset_daily()
                # Clear ALL aggregator per-day state — both in-progress
                # builders AND completed candles. Apr 25 2026 audit:
                # without clearing the completed list, callers that take
                # ``candles[:N]`` (oldest of last N) silently mix
                # yesterday's late-afternoon candles into today's morning
                # window, corrupting move_from_open_pct, the trend
                # filter, and the trend-leg's morning range detection.
                aggregator.clear_day()
            # Snapshot the day-start realized AFTER reset_daily so it
            # reflects the post-_closed.clear baseline. For intraday
            # strategies this is always 0; for multi-day open positions
            # whose pos.pnl is still 0 (no fills yet), also 0.
            day_start_realized = float(portfolio.get_pnl(strategy_id).realized)

            # Expiry rollover (GDFL only — the synthetic BS path is gone).
            clock.set_time(IST.localize(datetime.combine(day, time(9, 15))))
            if day_idx > 0:  # day 0 already loaded above
                market_source.load_day(day)
                new_tokens = market_source.register_options(chain_builder, alloc_token)
                option_tokens.update(new_tokens)
            exps = market_source.expiries_for_day()
            if exps:
                clock.set_expiries(underlying, exps)
                expiry = exps[0]

            day_open = spot
            day_trades_start = len(broker._trades)

            # Minute-by-minute replay
            start_dt = datetime.combine(day, time(9, 15))

            for i in range(0, 375, tick_interval_minutes):
                now = IST.localize(start_dt + timedelta(minutes=i))
                clock.set_time(now)

                # Real tick data path — market_source writes all caches
                gdfl_spot, gdfl_vix = market_source.apply(
                    now, feed, broker, chain_builder, portfolio, option_tokens,
                )
                if gdfl_spot is None:
                    continue  # No data this minute (pre-open, holiday gap)
                spot = gdfl_spot
                vix = gdfl_vix if gdfl_vix is not None else vix

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
            day_pnl = pnl.net  # legacy field — preserved for back-compat
            running_pnl += day_pnl
            day_trades = len(broker._trades) - day_trades_start

            # Apr 30 2026 Phase 4B: split into incremental realised vs
            # current-MTM unrealized. The MC + bootstrap CI gates
            # (validate_strategy.py:332) consume this series; using
            # ``realized_pnl`` instead of ``pnl`` (mtm-contaminated)
            # makes those gates measure execution variance rather than
            # market price-noise on open positions.
            current_realized = float(pnl.realized)
            realized_today = current_realized - day_start_realized
            unrealized_mtm = float(pnl.unrealized)

            daily_results.append({
                "date": day.isoformat(),
                "day_of_week": day.strftime("%A"),
                "spot_open": round(day_open, 2),
                "spot_close": round(spot, 2),
                # Legacy field — kept for any consumer that hasn't
                # migrated to realized_pnl/unrealized_mtm. For
                # intraday strategies (positions close EOD) this
                # equals realized_pnl; for multi-day strategies
                # (long_calendar) it includes today's MTM.
                "pnl": round(float(day_pnl), 2),
                "realized_pnl": round(realized_today, 2),
                "unrealized_mtm": round(unrealized_mtm, 2),
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
            # Per-fill records with spread_half + book_snapshot metadata.
            # Consumed by the validation harness (cost sensitivity +
            # capacity modules) without re-reading GDFL parquet files.
            "trades": list(broker._trades),
        }

        logger.info(
            f"[BACKTEST] Complete: {strategy_name} {num_days}d "
            f"P&L={float(running_pnl):+,.0f} "
            f"Sharpe={metrics.get('sharpe_ratio', 0):.2f} "
            f"WinRate={metrics.get('win_rate', 0):.1f}%"
        )

        return result
