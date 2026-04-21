"""Application entry point — orchestrates all services."""

import asyncio
import logging
import signal
import sys
from pathlib import Path

import uvicorn

from src.config import Settings
from src.core.clock import MarketClock, now_ist
from src.core.events import EventBus, EventType, RedisEventBridge
from src.broker.zerodha.client import ZerodhaClient
from src.broker.zerodha.instruments import InstrumentManager
from src.broker.zerodha.ticker import TickerManager
from src.broker.paper.client import PaperBrokerClient
from src.db.session import create_db_engine, create_session_factory
from src.market_data.aggregator import OHLCAggregator
from src.market_data.feed import TickFeedManager
from src.market_data.option_chain import OptionChainBuilder
from src.market_data.chain_recorder import ChainSnapshotRecorder
from src.market_data.tick_recorder import TickRecorder
from src.market_data.vix_recorder import IndiaVixRecorder
from src.observability.heartbeat import Heartbeat
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
from src.utils.log_tags import Tag
from src.utils.logging import setup_logging

logger = logging.getLogger(__name__)


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
    if settings.paper_trading:
        broker = PaperBrokerClient(initial_capital=1_000_000)
        logger.info("Using PAPER TRADING broker")

        # Feed tick LTPs into paper broker — use chain builder's symbol map
        # since WebSocket ticks don't include tradingsymbol
        from src.core.constants import INDIA_VIX_TOKEN
        from src.core.events import Event as _Event

        async def _feed_paper_ltp(event: _Event) -> None:
            tick = event.payload.get("tick")
            if not tick or not tick.get("ltp"):
                return
            ltp = float(tick["ltp"])
            if ltp <= 0:
                return
            token = tick.get("instrument_token", 0)
            # VIX feeds the paper broker's slippage regime multiplier
            if token == INDIA_VIX_TOKEN:
                broker.set_vix(ltp)
                return
            # Resolve tradingsymbol from chain builder's symbol map (registered from instrument master)
            symbol = tick.get("tradingsymbol") or ""
            if not symbol and token:
                symbol = chain_builder._symbol_map.get(token, "")
            if symbol:
                broker.set_ltp(symbol, ltp)

        event_bus.subscribe(EventType.TICK, _feed_paper_ltp)
    else:
        broker = ZerodhaClient(settings.kite_api_key, settings.kite_access_token)
        logger.info("Using LIVE Zerodha broker")

    await broker.connect()

    # ─── Instruments ─────────────────────────────────────────────
    instrument_manager = InstrumentManager(broker, session_factory)
    try:
        await instrument_manager.load_from_db()
    except Exception:
        logger.warning("Could not load instruments from DB — trying Kite API fallback")
        try:
            await instrument_manager.load_from_kite_api(settings.kite_api_key, settings.kite_access_token)
        except Exception as e:
            logger.error(f"Kite API instrument fallback also failed: {e}")

    # ─── Market Data Pipeline ────────────────────────────────────
    feed = TickFeedManager(event_bus, redis_client)
    aggregator = OHLCAggregator(event_bus)
    chain_builder = OptionChainBuilder(event_bus, clock, redis_client)
    data_store = MarketDataStore(session_factory, event_bus)

    # Register known spot tokens so strategies can auto-subscribe
    from src.market_data.simulator import NIFTY_SPOT_TOKEN, BANKNIFTY_SPOT_TOKEN
    chain_builder.register_spot(NIFTY_SPOT_TOKEN, "NIFTY")
    chain_builder.register_spot(BANKNIFTY_SPOT_TOKEN, "BANKNIFTY")

    # Register option instruments for live chain building (±30 strikes around ATM)
    # Wider range ensures IC wing strikes are always available
    from src.core.types import OptionType
    option_tokens_to_subscribe: list[int] = []
    for underlying, step, num_strikes in [("NIFTY", 50, 30), ("BANKNIFTY", 100, 20)]:
        try:
            expiry = clock.next_expiry(underlying)
            options = instrument_manager.get_option_chain_instruments(underlying, expiry)
            if not options:
                logger.warning(f"No option instruments found for {underlying} expiry={expiry}")
                continue
            # Get spot price from Kite LTP API for accurate ATM
            # Always use real Kite API — paper broker has no prices at startup
            spot_price = 0
            if settings.kite_api_key and settings.kite_access_token:
                try:
                    from kiteconnect import KiteConnect as _KC
                    _kc = _KC(api_key=settings.kite_api_key)
                    _kc.set_access_token(settings.kite_access_token)
                    ltp_data = _kc.ltp([f"NSE:{underlying} 50"])
                    spot_price = ltp_data.get(f"NSE:{underlying} 50", {}).get("last_price", 0)
                    logger.info(f"Kite LTP for {underlying}: {spot_price}")
                except Exception as e:
                    logger.warning(f"Kite LTP fetch failed for {underlying}: {e}")
            if spot_price <= 0:
                # Fallback: use middle strike weighted toward lower end (markets spend more time below median)
                all_strikes = sorted(set(float(o.strike) for o in options))
                spot_price = all_strikes[len(all_strikes) // 3]
            atm_estimate = round(spot_price / step) * step
            lo = atm_estimate - num_strikes * step
            hi = atm_estimate + num_strikes * step
            registered = 0
            for inst in options:
                if lo <= float(inst.strike) <= hi:
                    chain_builder.register_option(
                        instrument_token=inst.instrument_token,
                        underlying=underlying,
                        expiry=expiry,
                        strike=inst.strike,
                        option_type=OptionType(inst.instrument_type.value),
                        tradingsymbol=inst.tradingsymbol,
                    )
                    option_tokens_to_subscribe.append(inst.instrument_token)
                    registered += 1
            logger.info(
                f"Registered {registered} option instruments for {underlying} "
                f"expiry={expiry} ATM~{atm_estimate:.0f} strikes={lo:.0f}-{hi:.0f}"
            )

            # Seed paper broker with option LTPs from Kite HTTP API
            # This allows fills before WebSocket ticks arrive
            if settings.paper_trading and settings.kite_api_key:
                try:
                    seed_symbols = []
                    token_to_symbol = {}
                    for inst in options:
                        if lo <= float(inst.strike) <= hi:
                            key = f"NFO:{inst.tradingsymbol}"
                            seed_symbols.append(key)
                            token_to_symbol[key] = inst.tradingsymbol
                    # Kite LTP API accepts max 1000 instruments per call
                    for i in range(0, len(seed_symbols), 500):
                        batch = seed_symbols[i:i+500]
                        ltp_batch = _kc.ltp(batch)
                        for key, data in ltp_batch.items():
                            sym = token_to_symbol.get(key, "")
                            if sym and data.get("last_price", 0) > 0:
                                broker.set_ltp(sym, float(data["last_price"]))
                    logger.info(f"Seeded paper broker with {len(seed_symbols)} option LTPs")
                except Exception as e:
                    logger.warning(f"Failed to seed option LTPs: {e}")

        except Exception as e:
            logger.warning(f"Failed to register options for {underlying}: {e}")

    # ─── Seed VIX + NIFTY spot into feed + chain builder ──
    # WebSocket may not send ticks immediately; seed from HTTP API so strategy can evaluate
    if settings.kite_api_key and settings.kite_access_token:
        try:
            from src.core.constants import INDIA_VIX_TOKEN
            from src.core.models import Tick
            from decimal import Decimal as D
            from datetime import datetime
            from kiteconnect import KiteConnect as _KC
            _kc = _KC(api_key=settings.kite_api_key)
            _kc.set_access_token(settings.kite_access_token)
            seed_ltps = _kc.ltp(["NSE:INDIA VIX", "NSE:NIFTY 50", "NSE:NIFTY BANK"])
            for key, data in seed_ltps.items():
                ltp = data.get("last_price", 0)
                token = data.get("instrument_token", 0)
                if ltp > 0 and token > 0:
                    seed_tick = Tick(
                        instrument_token=token, tradingsymbol=key.split(":")[-1],
                        timestamp=now_ist(), ltp=D(str(ltp)),
                        volume=0, oi=0, bid_price=D("0"), ask_price=D("0"),
                        bid_qty=0, ask_qty=0, high=D("0"), low=D("0"),
                        open=D("0"), close=D("0"),
                    )
                    feed._latest_ticks[token] = seed_tick
                    # Also seed into chain builder for spot price
                    if token in chain_builder._spot_tokens:
                        underlying = chain_builder._spot_tokens[token]
                        chain_builder._spot_prices[underlying] = D(str(ltp))
                    logger.info(f"Seeded feed: {key} = {ltp}")
        except Exception as e:
            logger.warning(f"Failed to seed feed LTPs: {e}")

    # ─── Recorders + heartbeat ───────────────────────────────────
    # In split mode (DATA_RELIABILITY_PLAN §5) the recorder process owns
    # the WebSocket and the recorders. The trader gets ticks via Redis
    # pub/sub (RedisEventBridge below). Heartbeat name + path differ so
    # the watchdog can distinguish the two processes.
    if settings.recorder_split_mode:
        # Trader-only heartbeat. Recorder writes its own data/heartbeat/recorder.json.
        recorder_heartbeat = Heartbeat(
            process_name="trader",
            output_path="data/heartbeat/trader.json",
            interval_s=30.0,
        )
        chain_recorder = None
        vix_recorder = None
        tick_recorder = None
        logger.info(
            "recorder_split_mode=true — recorders run in separate process; "
            "trader will subscribe to ticks via Redis pub/sub"
        )
    else:
        recorder_heartbeat = Heartbeat(
            process_name="trader",  # legacy single-process value, kept for back-compat
            output_path="data/heartbeat/recorder.json",
            interval_s=30.0,
        )
        chain_recorder = ChainSnapshotRecorder(
            chain_builder, clock, interval_seconds=60, heartbeat=recorder_heartbeat,
        )
        vix_recorder = IndiaVixRecorder(event_bus, clock, heartbeat=recorder_heartbeat)
        # Tick recorder: enabled by default, gated to subscribed instruments only
        # so we don't bloat disk with unrelated WebSocket noise.
        tick_recorder = TickRecorder(event_bus, clock, heartbeat=recorder_heartbeat)

    # ─── Ticker / Simulator / Redis bridge ────────────────────────
    # Three mutually exclusive sources of TICK events:
    #   1. split mode  -> RedisEventBridge consumes from recorder process
    #   2. Kite creds  -> TickerManager owns the WebSocket directly
    #   3. no creds    -> SimulationEngine generates synthetic ticks
    ticker = None
    simulator = None
    redis_bridge = None
    if settings.recorder_split_mode:
        if not redis_client:
            raise RuntimeError(
                "recorder_split_mode=true requires Redis. Bring Redis up "
                "before starting the trader, or set recorder_split_mode=false."
            )
        # Forward TICK + connection events from recorder. Strategies subscribe
        # to TICK on the local bus exactly as before; they don't know the
        # event came from another process.
        redis_bridge = RedisEventBridge(
            redis_client=redis_client,
            bus=event_bus,
            event_types=[
                EventType.TICK,
                EventType.CONNECTION_LOST,
                EventType.CONNECTION_RESTORED,
            ],
        )
        logger.info("Trader will receive market data from recorder via Redis bridge")
    elif settings.kite_api_key and settings.kite_access_token:
        ticker = TickerManager(settings.kite_api_key, settings.kite_access_token, event_bus)
        logger.info("Live market data via Kite WebSocket" + (" (paper trading)" if settings.paper_trading else ""))
    else:
        from src.market_data.simulator import SimulationEngine
        simulator = SimulationEngine(event_bus, chain_builder, tick_interval=1.0)
        logger.info("Simulator mode — no Kite credentials available")

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

    from src.strategy.state_store import StrategyStateStore
    state_store = StrategyStateStore(session_factory)
    strategy_runner = StrategyRunner(
        event_bus, feed, chain_builder, aggregator, clock,
        order_callback, portfolio_getter,
        state_store=state_store,
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
        "clock": clock,
        "event_bus": event_bus,
        "broker": broker,
        "feed": feed,
        "ticker": ticker,
        "simulator": simulator,
        "redis_bridge": redis_bridge,
        "aggregator": aggregator,
        "chain_builder": chain_builder,
        "chain_recorder": chain_recorder,
        "vix_recorder": vix_recorder,
        "tick_recorder": tick_recorder,
        "recorder_heartbeat": recorder_heartbeat,
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
        "option_tokens": option_tokens_to_subscribe,
    }


async def run():
    """Main application run loop."""
    settings = Settings()
    # JSONL sink lives next to the heartbeat dir. One file per process so
    # recorder/trader logs don't interleave on disk (DATA_RELIABILITY_PLAN §8.2).
    setup_logging(
        settings.log_level,
        json_output=settings.is_production,
        process_name="trader",
        log_file=Path("data/logs/trader.jsonl"),
    )

    logger.info("=" * 60)
    logger.info("F&O Trading System Starting")
    logger.info(f"Environment: {settings.environment}")
    logger.info(f"Paper Trading: {settings.paper_trading}")
    logger.info("=" * 60)

    # Boot-time secret audit (Apr 21 — see commit 32dd91c follow-up).
    # Settings is read ONCE at daemon startup and reused all day, so a missing
    # secret silently breaks features hours later (e.g. advisor at 9 AM, audit
    # at 4 PM). Surface gaps now, while the operator is still watching the
    # console / Telegram boot ping.
    _critical_secrets = {
        "anthropic_api_key": "morning advisor + nightly audit will be DISABLED",
        "telegram_bot_token": "no Telegram alerts will be sent",
        "telegram_chat_id": "Telegram configured but no chat_id set",
    }
    _missing_at_boot = [
        (attr, why) for attr, why in _critical_secrets.items()
        if not getattr(settings, attr, "")
    ]
    if _missing_at_boot:
        logger.warning("=" * 60)
        logger.warning("BOOT SECRET AUDIT — gaps found:")
        for attr, why in _missing_at_boot:
            logger.warning(f"  [MISSING] {attr.upper()} → {why}")
        logger.warning(
            "Resolution order: keychain > os.environ > %s/.env",
            Path(__file__).resolve().parents[1],
        )
        logger.warning("=" * 60)
    else:
        logger.info("Boot secret audit: all critical secrets resolved.")

    # Auto-authenticate helper — used at startup and scheduled daily
    async def do_auto_auth(reason: str = "startup") -> bool:
        """Authenticate with Kite and update token. Returns True on success."""
        import os
        try:
            from scripts.auto_auth import get_request_token, exchange_token, save_token, load_env
            from src.core.secrets import get_secret
            load_env()  # Ensure .env vars are in os.environ
            user_id = os.environ.get("KITE_USER_ID", "")
            password = get_secret("KITE_PASSWORD")
            totp_secret = get_secret("KITE_TOTP_SECRET")
            if not (user_id and password and totp_secret):
                logger.warning(
                    "Auto-auth skipped — set KITE_USER_ID in .env and KITE_PASSWORD/KITE_TOTP_SECRET "
                    "in the OS keychain (run: python -m src.core.secrets migrate)"
                )
                return False
            request_token = get_request_token(settings.kite_api_key, user_id, password, totp_secret)
            access_token = exchange_token(settings.kite_api_key, settings.kite_api_secret, request_token)
            save_token(access_token)
            settings.kite_access_token = access_token
            logger.info(f"Auto-auth successful ({reason}) — new token saved")

            # Send Telegram notification
            if settings.telegram_bot_token:
                try:
                    from src.notifications.telegram import TelegramNotifier
                    tg = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
                    await tg.send_message(
                        f"Kite auto-login successful ({reason})\n"
                        f"User: {user_id}\n"
                        f"Token: {access_token[:8]}...",
                        parse_mode="",
                    )
                except Exception:
                    pass
            return True
        except Exception as e:
            logger.error(f"Auto-auth failed ({reason}): {e}")
            if settings.telegram_bot_token:
                try:
                    from src.notifications.telegram import TelegramNotifier
                    tg = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
                    await tg.send_message(f"Kite auto-login FAILED ({reason}): {e}", parse_mode="")
                except Exception:
                    pass
            return False

    def is_token_valid() -> bool:
        """Check if current Kite access token is valid."""
        if not settings.kite_access_token:
            return False
        try:
            from kiteconnect import KiteConnect as _KC
            _kc = _KC(api_key=settings.kite_api_key)
            _kc.set_access_token(settings.kite_access_token)
            _kc.profile()
            return True
        except Exception:
            return False

    # Authenticate at startup
    if settings.kite_api_key:
        if is_token_valid():
            logger.info("Kite token valid")
        else:
            logger.warning("Kite token expired — attempting auto-auth...")
            await do_auto_auth("startup")

    app = await create_app(settings)

    # Start services
    await app["event_bus"].start()
    await app["tracker"].start()

    # Subscribe option chain builder to tick events
    app["event_bus"].subscribe(EventType.TICK, app["chain_builder"].on_tick)

    # Start the recorder heartbeat first so the watchdog sees a fresh file
    # immediately, even if individual recorders haven't produced data yet.
    await app["recorder_heartbeat"].start()
    # Recorders run here only when NOT in split mode. In split mode they
    # live in src.recorder_main and we just bridge ticks from Redis.
    if app["chain_recorder"]:
        await app["chain_recorder"].start()
    if app["vix_recorder"]:
        await app["vix_recorder"].start()
    if app["tick_recorder"]:
        await app["tick_recorder"].start()
    # Start the Redis tick bridge (split mode only). Must come BEFORE strategy
    # registration so any TICK fired during startup is delivered.
    if app["redis_bridge"]:
        await app["redis_bridge"].start()

    # Schedule daily P&L reset at market open
    async def daily_reset_task():
        """Reset daily P&L and circuit breaker at 9:10 AM each trading day."""
        from datetime import datetime, time as _time
        last_reset_date = None
        while True:
            now = now_ist()
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

    # Scheduled daily re-auth at 8:55 AM + token health monitor
    async def auth_refresh_task():
        """Re-authenticate with Kite every morning and monitor token health."""
        from datetime import datetime, time as _time
        from src.core.clock import MarketClock

        _clock = MarketClock()
        last_auth_date = None
        ticker_restarted_today = False

        while True:
            try:
                now = now_ist()
                today = now.date()

                # Skip holidays and weekends
                if _clock.is_trading_holiday(today) or today.weekday() >= 5:
                    await asyncio.sleep(300)
                    continue

                # Daily re-auth at 8:55 AM (before market open)
                if (
                    now.time() >= _time(8, 55)
                    and now.time() < _time(9, 0)
                    and last_auth_date != today
                ):
                    last_auth_date = today
                    ticker_restarted_today = False
                    logger.info("Scheduled daily re-auth starting...")
                    success = await do_auto_auth("scheduled_daily")
                    if success and app.get("ticker"):
                        # Update token on existing ticker — it will use it on next reconnect
                        app["ticker"].update_token(settings.kite_access_token)
                        logger.info("Ticker token updated, will reconnect at market open")

                # At 9:16 AM — force ticker restart with fresh token to ensure ticks flow
                if (
                    now.time() >= _time(9, 16)
                    and now.time() < _time(9, 18)
                    and last_auth_date == today
                    and not ticker_restarted_today
                ):
                    ticker_restarted_today = True
                    ticker = app.get("ticker")
                    if ticker and not ticker._last_tick_time:
                        # No ticks received yet — full restart
                        logger.info("Market open but no ticks — full ticker restart")
                        old_ticker = ticker
                        await old_ticker.stop()
                        new_ticker = TickerManager(
                            settings.kite_api_key, settings.kite_access_token, app["event_bus"]
                        )
                        tokens = list(old_ticker._subscribed_tokens)
                        if tokens:
                            new_ticker.subscribe(tokens)
                        await new_ticker.start()
                        app["ticker"] = new_ticker
                        logger.info("Ticker fully restarted at market open")
                    elif ticker and ticker._last_tick_time:
                        logger.info("Ticks already flowing — no restart needed")

                # Token health check every 5 minutes during market hours
                if _clock.is_market_open() and now.minute % 5 == 0:
                    if not is_token_valid():
                        logger.warning("Token expired mid-session — re-authenticating...")
                        success = await do_auto_auth("token_expired")
                        if success and app.get("ticker"):
                            # Update token and force reconnect
                            app["ticker"].update_token(settings.kite_access_token)
                            await app["ticker"]._force_reconnect()
                            logger.info("Ticker reconnected after mid-session re-auth")

            except Exception:
                logger.exception("Error in auth refresh task")

            await asyncio.sleep(30)

    asyncio.create_task(auth_refresh_task())

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
                    "operational summary",
                    extra={
                        "tag": Tag.SUMMARY,
                        "positions": len(open_positions),
                        "realized": pnl.realized,
                        "unrealized": pnl.unrealized,
                        "day_pnl": pnl.net,
                        "charges": pnl.charges,
                        "ticks_60s": ticks_since_last,
                        "strategies": strategy_states,
                        "circuit_breaker": cb_state,
                        "pending_orders": pending_orders,
                        "queue_depth": queue_depth,
                    },
                )

                # Take P&L snapshot for intraday curve
                portfolio.snapshot_pnl()

                # Snapshot OI at end of day for next-day change tracking
                from datetime import datetime as _dt
                now_t = _dt.now()
                if now_t.hour == 15 and 25 <= now_t.minute <= 29:
                    app["chain_builder"].snapshot_oi_for_next_day()

            except Exception:
                logger.exception("Error in operational summary")

    asyncio.create_task(operational_summary_task())

    # AI Advisor — morning advisory + nightly audit
    async def advisor_task():
        """Run morning advisor at 9:00 AM and nightly audit at 4:00 PM on trading days."""
        from datetime import date as _date, datetime, time as _time
        from pathlib import Path
        from src.core.clock import MarketClock

        _clock = MarketClock()
        last_advisory_date = None
        last_audit_date = None

        while True:
            now = now_ist()
            today = now.date()

            if _clock.is_trading_holiday(today):
                await asyncio.sleep(300)
                continue

            # Morning advisor: 9:00-9:05 AM
            if (
                now.time() >= _time(9, 0)
                and now.time() < _time(9, 5)
                and last_advisory_date != today
            ):
                last_advisory_date = today
                try:
                    from src.advisor.analyzer import analyze_with_claude
                    from src.advisor.collector import collect_today_data
                    from src.advisor.context import fetch_external_context
                    from src.advisor.store import save_advisory_json, save_day_bias_json

                    yesterday = today
                    log_dir = Path("logs")
                    today_data = await collect_today_data(
                        target_date=yesterday,
                        log_dir=log_dir if log_dir.exists() else None,
                        settings=settings,
                    )
                    # Get NIFTY prev close for GIFT Nifty change calculation
                    nifty_close = today_data.closing_spot
                    if nifty_close <= 0:
                        ltp = app["feed"].get_ltp(256265)  # NIFTY spot token
                        nifty_close = float(ltp) if ltp else 0

                    context = await fetch_external_context(
                        target_date=today,
                        calendar_path=Path("data/economic_calendar.json"),
                        nifty_prev_close=nifty_close,
                    )
                    advisory = await analyze_with_claude(today_data, context, settings)
                    save_day_bias_json(advisory)
                    save_advisory_json(advisory)
                    logger.info(
                        "morning advisory generated",
                        extra={
                            "tag": Tag.ADVISOR,
                            "phase": "morning",
                            "risk_level": advisory.day_bias.risk_level,
                            "confidence": round(advisory.day_bias.confidence, 2),
                        },
                    )

                    # Send to Telegram
                    if settings.telegram_bot_token:
                        from src.advisor.formatter import format_advisory_telegram
                        from src.notifications.telegram import TelegramNotifier
                        tg = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
                        await tg.send_message(format_advisory_telegram(advisory), parse_mode="")

                except Exception:
                    logger.exception(
                        "morning advisory failed",
                        extra={"tag": Tag.ADVISOR, "phase": "morning"},
                    )

            # Nightly audit: 4:00-4:05 PM
            if (
                now.time() >= _time(16, 0)
                and now.time() < _time(16, 5)
                and last_audit_date != today
            ):
                last_audit_date = today
                try:
                    from src.advisor.collector import collect_today_data
                    from src.advisor.formatter import format_audit_telegram
                    from src.advisor.shadow import build_audit
                    from src.advisor.store import save_audit_json

                    log_dir = Path("logs")
                    today_data = await collect_today_data(
                        target_date=today,
                        log_dir=log_dir if log_dir.exists() else None,
                        settings=settings,
                    )
                    audit = build_audit(
                        target_date=today,
                        log_dir=log_dir if log_dir.exists() else None,
                        actual_pnl=today_data.total_pnl,
                    )
                    save_audit_json(audit)
                    logger.info(
                        "nightly audit complete",
                        extra={
                            "tag": Tag.ADVISOR,
                            "phase": "nightly_audit",
                            "decisions": len(audit.decisions),
                            "agree_count": audit.agree_count,
                            "disagree_count": audit.disagree_count,
                            "ai_alpha": round(audit.ai_alpha, 2),
                        },
                    )

                    # Send to Telegram if configured
                    if settings.telegram_bot_token:
                        from src.notifications.telegram import TelegramNotifier
                        tg = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
                        await tg.send_message(format_audit_telegram(audit), parse_mode="")

                except Exception:
                    logger.exception(
                        "nightly audit failed",
                        extra={"tag": Tag.ADVISOR, "phase": "nightly_audit"},
                    )

            await asyncio.sleep(30)

    asyncio.create_task(advisor_task())

    # Import strategy implementations to trigger registration
    import src.strategy.implementations.short_straddle  # noqa: F401
    import src.strategy.implementations.short_strangle  # noqa: F401

    try:
        import src.strategy.implementations.iron_condor  # noqa: F401
        import src.strategy.implementations.delta_neutral  # noqa: F401
        import src.strategy.implementations.portfolio_strategy  # noqa: F401
        import src.strategy.implementations.trend_debit_spread  # noqa: F401
        import src.strategy.adaptive  # noqa: F401
    except ImportError:
        pass

    # ─── Holiday Guard ──────────────────────────────────────────
    clock = app["clock"] if "clock" in app else MarketClock()
    if clock.is_trading_holiday(clock.today()) or clock.today().weekday() >= 5:
        logger.warning(
            f"TODAY IS NOT A TRADING DAY ({clock.today()}). "
            f"Strategies will NOT be loaded. Only chain recorder and API will run."
        )
        # Still start ticker for chain recording, but skip strategies
        if app["ticker"]:
            all_tokens = list(set(app["feed"].get_subscribed_tokens()) | set(app.get("option_tokens", [])))
            if all_tokens:
                app["ticker"].subscribe(all_tokens)
            await app["ticker"].start()

        # Start API server (for monitoring)
        try:
            from src.api.app import create_api_app
            api_app = create_api_app(app)
            config = uvicorn.Config(api_app, host="0.0.0.0", port=settings.api_port, log_level="info")
            server = uvicorn.Server(config)
            await server.serve()
        except Exception:
            logger.exception("API server error on holiday")
        return

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

    # Forward all feed subscriptions + option tokens to the WebSocket ticker, then start it
    if app["ticker"]:
        all_tokens = app["feed"].get_subscribed_tokens()
        # Add option tokens for live chain building
        all_tokens = list(set(all_tokens) | set(app.get("option_tokens", [])))
        if all_tokens:
            app["ticker"].subscribe(all_tokens)
            logger.info(f"Ticker will subscribe to {len(all_tokens)} tokens on connect")
        await app["ticker"].start()
    elif app["simulator"]:
        await app["simulator"].start()

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

    # 4. Stop Redis bridge first (split mode) so no more ticks arrive
    if app.get("redis_bridge"):
        try:
            await app["redis_bridge"].stop()
        except Exception:
            logger.exception("Error stopping Redis event bridge")

    # 4a. Stop chain recorder (in-process mode only)
    if app.get("chain_recorder"):
        try:
            await app["chain_recorder"].stop()
        except Exception:
            logger.exception("Error stopping chain recorder")

    # 4b. Stop India VIX recorder (flush in-flight minute)
    if app.get("vix_recorder"):
        try:
            await app["vix_recorder"].stop()
        except Exception:
            logger.exception("Error stopping VIX recorder")

    # 4c. Stop tick recorder (drain pending tick buffers to Parquet)
    if app.get("tick_recorder"):
        try:
            await app["tick_recorder"].stop()
        except Exception:
            logger.exception("Error stopping tick recorder")

    # 4d. Stop heartbeat last so a crash in one of the recorder stop()s
    # is still visible in the final heartbeat snapshot.
    try:
        await app["recorder_heartbeat"].stop()
    except Exception:
        logger.exception("Error stopping recorder heartbeat")

    # 5. Flush market data before closing connections
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
