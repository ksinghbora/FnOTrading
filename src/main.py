"""Application entry point — orchestrates all services."""

import asyncio
import logging
import signal
import sys

import uvicorn

from src.config import Settings
from src.core.clock import MarketClock
from src.core.events import EventBus, EventType
from src.broker.zerodha.client import ZerodhaClient
from src.broker.zerodha.instruments import InstrumentManager
from src.broker.zerodha.ticker import TickerManager
from src.broker.paper.client import PaperBrokerClient
from src.db.session import create_db_engine, create_session_factory
from src.market_data.aggregator import OHLCAggregator
from src.market_data.feed import TickFeedManager
from src.market_data.option_chain import OptionChainBuilder
from src.market_data.store import MarketDataStore
from src.oms.dedup import OrderDeduplicator
from src.oms.executor import OrderExecutor
from src.oms.manager import OrderManager
from src.oms.tracker import OrderTracker
from src.oms.validator import OrderValidator
from src.portfolio.manager import PortfolioManager
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.greeks_risk import GreeksRiskMonitor
from src.risk.kill_switch import KillSwitch
from src.risk.limits import RiskLimits
from src.risk.manager import RiskManager
from src.strategy.runner import StrategyRunner
from src.utils.logging import setup_logging

logger = logging.getLogger(__name__)

# Kite instrument tokens for index spot
NIFTY_SPOT_TOKEN = 256265
BANKNIFTY_SPOT_TOKEN = 260105


def _register_instruments_from_db(
    instrument_manager: "InstrumentManager",
    chain_builder: "OptionChainBuilder",
    clock: "MarketClock",
) -> list[int]:
    """Register spot and option instruments from DB into the chain builder.

    Returns list of all instrument tokens that should be subscribed on the ticker.
    """
    from decimal import Decimal
    from src.core.types import InstrumentType, OptionType

    # Register spot tokens
    chain_builder.register_spot(NIFTY_SPOT_TOKEN, "NIFTY")
    chain_builder.register_spot(BANKNIFTY_SPOT_TOKEN, "BANKNIFTY")

    tokens = [NIFTY_SPOT_TOKEN, BANKNIFTY_SPOT_TOKEN]

    # Register F&O instruments for upcoming expiries
    registered = 0
    for underlying in ("NIFTY", "BANKNIFTY"):
        expiry = clock.next_expiry(underlying)
        instruments = instrument_manager.get_option_chain_instruments(underlying, expiry)
        if not instruments:
            # Try monthly expiry
            expiry = clock.next_monthly_expiry()
            instruments = instrument_manager.get_option_chain_instruments(underlying, expiry)

        for inst in instruments:
            opt_type = (
                OptionType.CE if inst.instrument_type == InstrumentType.CE else OptionType.PE
            )
            chain_builder.register_option(
                instrument_token=inst.instrument_token,
                underlying=underlying,
                expiry=inst.expiry,
                strike=Decimal(str(inst.strike)),
                option_type=opt_type,
                tradingsymbol=inst.tradingsymbol,
            )
            tokens.append(inst.instrument_token)
            registered += 1

    logger.info(f"Registered {registered} option instruments from DB into chain builder")
    return tokens


async def create_app(settings: Settings):
    """Create and wire all application components."""

    # ─── Infrastructure ──────────────────────────────────────────
    db_engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(db_engine)

    redis_client = None
    try:
        from redis.asyncio import Redis
        redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
        await redis_client.ping()
        logger.info("Redis connected")
    except Exception as e:
        logger.warning(f"Redis not available: {e}. Running without cache.")

    # ─── Core Services ───────────────────────────────────────────
    event_bus = EventBus(redis_client=redis_client)
    clock = MarketClock()

    # ─── Broker ──────────────────────────────────────────────────
    # Detect hybrid mode: paper trading + real Kite data
    has_kite_creds = bool(settings.kite_api_key and settings.kite_access_token)
    use_hybrid = settings.paper_trading and settings.use_live_data and has_kite_creds

    # Create a Kite client for API access (instruments, LTP) in hybrid/live mode
    kite_client = None
    if has_kite_creds:
        kite_client = ZerodhaClient(settings.kite_api_key, settings.kite_access_token)
        try:
            await kite_client.connect()
            logger.info("Kite API connected")
        except Exception as e:
            logger.warning(f"Kite API connection failed: {e}. Falling back to simulator.")
            kite_client = None
            use_hybrid = False

    if settings.paper_trading:
        broker = PaperBrokerClient(initial_capital=1_000_000)
        logger.info(f"Using PAPER TRADING broker (data: {'Kite live' if use_hybrid else 'simulator'})")

        # Feed tick LTPs into paper broker for realistic fills
        from src.core.events import Event as _Event

        async def _feed_paper_ltp(event: _Event) -> None:
            tick = event.payload.get("tick")
            if tick and tick.get("tradingsymbol") and tick.get("ltp"):
                broker.set_ltp(tick["tradingsymbol"], float(tick["ltp"]))

        event_bus.subscribe(EventType.TICK, _feed_paper_ltp)
    else:
        broker = kite_client or ZerodhaClient(settings.kite_api_key, settings.kite_access_token)
        if not kite_client:
            await broker.connect()
        logger.info("Using LIVE Zerodha broker")

    await broker.connect()

    # ─── Instruments ─────────────────────────────────────────────
    # Use Kite client for instruments if available, else fall back to broker
    instrument_broker = kite_client if kite_client else broker
    instrument_manager = InstrumentManager(instrument_broker, session_factory)
    try:
        await instrument_manager.load_from_db()
        if not instrument_manager._instruments and kite_client:
            logger.info("No instruments in DB — downloading from Kite...")
            await instrument_manager.download_and_store()
    except Exception:
        if kite_client:
            try:
                logger.info("Instruments DB not ready — downloading from Kite...")
                await instrument_manager.download_and_store()
            except Exception as dl_err:
                logger.warning(f"Could not download instruments: {dl_err}")
        else:
            logger.warning("Could not load instruments from DB. Run download_instruments.py first.")

    # ─── Market Data Pipeline ────────────────────────────────────
    feed = TickFeedManager(event_bus, redis_client)
    aggregator = OHLCAggregator(event_bus)
    chain_builder = OptionChainBuilder(event_bus, clock, redis_client)
    data_store = MarketDataStore(session_factory, event_bus)

    # ─── Ticker / Simulator ───────────────────────────────────────
    ticker = None
    simulator = None
    if use_hybrid or not settings.paper_trading:
        # Hybrid mode or live mode: use Kite WebSocket for real market data
        ticker = TickerManager(settings.kite_api_key, settings.kite_access_token, event_bus)
        feed.set_ticker(ticker)  # Forward subscriptions to Kite WebSocket

        # Build token -> tradingsymbol map for tick enrichment
        symbol_map = {token: inst.tradingsymbol for token, inst in instrument_manager._instruments.items()}
        symbol_map[NIFTY_SPOT_TOKEN] = "NIFTY"
        symbol_map[BANKNIFTY_SPOT_TOKEN] = "BANKNIFTY"
        ticker.set_symbol_map(symbol_map)
        logger.info(f"Using Kite ticker for real-time market data ({len(symbol_map)} symbols mapped)")

        # Register real instruments from DB and subscribe on ticker
        option_tokens = _register_instruments_from_db(instrument_manager, chain_builder, clock)
        ticker.subscribe(option_tokens)
    else:
        # Pure paper mode (no Kite credentials): use simulator
        from src.market_data.simulator import SimulationEngine
        simulator = SimulationEngine(event_bus, chain_builder, tick_interval=1.0)
        logger.info("Using simulator for synthetic market data")

    # ─── Portfolio ───────────────────────────────────────────────
    portfolio = PortfolioManager(event_bus, broker, chain_builder)

    # ─── OMS ─────────────────────────────────────────────────────
    dedup = OrderDeduplicator()
    validator = OrderValidator(clock, dedup, feed, paper_trading=settings.paper_trading)
    executor = OrderExecutor(broker)
    tracker = OrderTracker(broker, event_bus)
    order_manager = OrderManager(validator, executor, tracker, event_bus, paper_trading=settings.paper_trading)

    # ─── Risk Management ─────────────────────────────────────────
    limits = RiskLimits(
        max_day_loss=settings.max_day_loss,
        max_strategy_loss=settings.max_strategy_loss,
        max_total_lots=settings.max_total_lots,
        max_open_orders=settings.max_open_orders,
    )
    circuit_breaker = CircuitBreaker(
        event_bus,
        max_day_loss=settings.max_day_loss,
        auto_reset_minutes=10 if settings.paper_trading else 0,
    )
    kill_switch = KillSwitch(broker, event_bus)
    greeks_monitor = GreeksRiskMonitor()
    risk_manager = RiskManager(
        limits, circuit_breaker, kill_switch, greeks_monitor, portfolio, event_bus
    )
    order_manager.set_risk_manager(risk_manager)
    risk_manager.set_order_manager(order_manager)

    # ─── Strategy Runner ─────────────────────────────────────────
    async def order_callback(signal_obj):
        """Route signals through OMS."""
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
            order = await order_manager.place_order(req)
            orders.append(order)
        return orders

    def portfolio_getter(what: str, strategy_id: str):
        if what == "positions":
            return portfolio.get_positions(strategy_id)
        elif what == "pnl":
            return portfolio.get_pnl(strategy_id)
        return None

    strategy_runner = StrategyRunner(
        event_bus, feed, chain_builder, aggregator, clock,
        order_callback, portfolio_getter
    )
    kill_switch.set_strategy_runner(strategy_runner)

    # ─── Reconcile on startup ────────────────────────────────────
    try:
        result = await portfolio.reconcile()
        logger.info(f"Startup reconciliation: {result['status']}")
    except Exception as e:
        logger.warning(f"Startup reconciliation failed: {e}")

    # ─── Notifications ───────────────────────────────────────────
    notifier = None
    if settings.telegram_bot_token:
        try:
            from src.notifications.manager import NotificationManager
            from src.notifications.telegram import TelegramNotifier
            telegram = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
            notifier = NotificationManager(event_bus, telegram)
            logger.info("Telegram notifications enabled")
        except ImportError:
            logger.warning("Notification module not available")

    return {
        "event_bus": event_bus,
        "broker": broker,
        "feed": feed,
        "ticker": ticker,
        "simulator": simulator,
        "aggregator": aggregator,
        "chain_builder": chain_builder,
        "data_store": data_store,
        "order_manager": order_manager,
        "portfolio": portfolio,
        "risk_manager": risk_manager,
        "strategy_runner": strategy_runner,
        "tracker": tracker,
        "db_engine": db_engine,
        "redis": redis_client,
        "settings": settings,
        "instrument_manager": instrument_manager,
    }


async def run():
    """Main application run loop."""
    settings = Settings()
    setup_logging(settings.log_level, json_output=settings.is_production)

    logger.info("=" * 60)
    logger.info("F&O Trading System Starting")
    logger.info(f"Environment: {settings.environment}")
    logger.info(f"Paper Trading: {settings.paper_trading}")
    logger.info("=" * 60)

    app = await create_app(settings)

    # Start services
    await app["event_bus"].start()
    await app["tracker"].start()

    # Subscribe option chain builder to tick events
    app["event_bus"].subscribe(EventType.TICK, app["chain_builder"].on_tick)

    # Schedule daily P&L reset at market open
    async def daily_reset_task():
        """Reset daily P&L and circuit breaker at 9:10 AM each trading day."""
        from datetime import datetime, time as _time
        last_reset_date = None
        while True:
            now = datetime.now()
            if (
                now.time() >= _time(9, 10)
                and now.time() < _time(9, 15)
                and not clock.is_trading_holiday(now.date())
                and last_reset_date != now.date()
            ):
                app["portfolio"].reset_daily()
                app["risk_manager"]._circuit_breaker.reset()
                app["strategy_runner"].reset_strategies_daily()
                last_reset_date = now.date()
                logger.info(f"Daily reset completed for {now.date()}")
            await asyncio.sleep(30)

    asyncio.create_task(daily_reset_task())

    # Periodic operational summary
    async def operational_summary_task():
        """Log system health summary every 60 seconds."""
        tick_counter = 0

        async def _count_tick(event):
            nonlocal tick_counter
            tick_counter += 1

        app["event_bus"].subscribe(EventType.TICK, _count_tick)
        last_tick_count = 0

        while True:
            await asyncio.sleep(60)
            try:
                portfolio = app["portfolio"]
                runner = app["strategy_runner"]
                risk_mgr = app["risk_manager"]

                open_positions = portfolio.get_open_positions()
                pnl = portfolio.get_pnl()
                ticks_since_last = tick_counter - last_tick_count
                last_tick_count = tick_counter

                strategies = runner.get_all_strategies()
                strategy_states = {
                    sid: s.state.value for sid, s in strategies.items()
                }

                cb_state = risk_mgr.circuit_breaker.state.value
                pending_orders = app["tracker"].pending_count
                queue_depth = app["event_bus"]._queue.qsize()

                logger.info(
                    f"[SUMMARY] positions={len(open_positions)} "
                    f"realized={pnl.realized} unrealized={pnl.unrealized} "
                    f"day_pnl={pnl.net} charges={pnl.charges} "
                    f"ticks_60s={ticks_since_last} "
                    f"strategies={strategy_states} "
                    f"circuit_breaker={cb_state} "
                    f"pending_orders={pending_orders} "
                    f"queue_depth={queue_depth}"
                )

                # Take P&L snapshot for intraday curve
                portfolio.snapshot_pnl()

            except Exception:
                logger.exception("Error in operational summary")

    asyncio.create_task(operational_summary_task())

    if app["ticker"]:
        await app["ticker"].start()
    elif app["simulator"]:
        # Only run simulator during market hours to avoid confusion
        async def _simulator_market_hours_task():
            """Start/stop simulator based on market hours."""
            sim = app["simulator"]
            _clock = app["chain_builder"]._clock
            was_running = False
            while True:
                if _clock.is_market_open():
                    if not was_running:
                        await sim.start()
                        logger.info("Simulator started (market open)")
                        was_running = True
                else:
                    if was_running:
                        await sim.stop()
                        logger.info("Simulator stopped (market closed)")
                        was_running = False
                await asyncio.sleep(30)

        asyncio.create_task(_simulator_market_hours_task())

    # Import strategy implementations to trigger registration
    import src.strategy.implementations.short_straddle  # noqa: F401
    import src.strategy.implementations.short_strangle  # noqa: F401

    try:
        import src.strategy.implementations.iron_condor  # noqa: F401
        import src.strategy.implementations.delta_neutral  # noqa: F401
        import src.strategy.adaptive  # noqa: F401
    except ImportError:
        pass

    # Auto-load strategies from config
    import json
    from src.strategy.registry import create_strategy, list_strategies

    strategy_configs = json.loads(settings.strategies)
    if strategy_configs:
        logger.info(f"Loading {len(strategy_configs)} strategies from config")
        for cfg in strategy_configs:
            try:
                strategy = create_strategy(
                    name=cfg["name"],
                    strategy_id=cfg.get("id", f"{cfg['name']}_1"),
                    params=cfg.get("params", {}),
                )
                await app["strategy_runner"].add_strategy(strategy)
                logger.info(f"Strategy loaded: {strategy.strategy_id} ({cfg['name']})")
            except Exception as e:
                logger.exception(f"Failed to load strategy {cfg}: {e}")
    else:
        logger.info(f"No strategies configured. Available: {list_strategies()}")

    # Start API server
    try:
        from src.api.app import create_api_app
        api_app = create_api_app(app)

        config = uvicorn.Config(
            api_app,
            host=settings.api_host,
            port=settings.api_port,
            log_level=settings.log_level.lower(),
        )
        server = uvicorn.Server(config)
        logger.info(f"API server starting on {settings.api_host}:{settings.api_port}")

        # Run API server (this blocks)
        await server.serve()
    except ImportError:
        logger.warning("API module not available, running without dashboard")
        # Just keep the event loop running
        stop_event = asyncio.Event()

        def _handle_signal():
            stop_event.set()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _handle_signal)

        await stop_event.wait()

    # Cleanup — ordered to prevent data loss
    logger.info("Shutting down...")

    # 1. Stop strategies first (prevents new signals)
    try:
        await app["strategy_runner"].stop_all()
    except Exception:
        logger.exception("Error stopping strategies")

    # 2. Wait for pending orders to settle
    await asyncio.sleep(2)

    # 3. Stop order tracker (stop polling broker)
    try:
        await app["tracker"].stop()
    except Exception:
        logger.exception("Error stopping order tracker")

    # 4. Flush market data before closing connections
    try:
        await app["data_store"].flush()
    except Exception:
        logger.exception("Error flushing data store")

    # 5. Stop event bus
    try:
        await app["event_bus"].stop()
    except Exception:
        logger.exception("Error stopping event bus")

    # 6. Stop data feeds
    if app["ticker"]:
        try:
            await app["ticker"].stop()
        except Exception:
            logger.exception("Error stopping ticker")
    if app["simulator"]:
        try:
            await app["simulator"].stop()
        except Exception:
            logger.exception("Error stopping simulator")

    # 7. Disconnect broker last
    try:
        await app["broker"].disconnect()
    except Exception:
        logger.exception("Error disconnecting broker")
    await app["db_engine"].dispose()
    if app["redis"]:
        await app["redis"].close()

    logger.info("Shutdown complete")


def main():
    """Entry point."""
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nShutting down...")


if __name__ == "__main__":
    main()
