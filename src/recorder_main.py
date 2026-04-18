"""Recorder process — owns the Kite WebSocket and captures all market data.

Why this exists (DATA_RELIABILITY_PLAN §5):
  Trader process restarts (config tweak, strategy hot-reload, deploy) used
  to interrupt the WebSocket and lose ticks for the duration. Worse, a
  strategy crash that took down the whole process also took down recording.
  This process is intentionally boring: subscribe, parse, write, repeat.
  Strategy code never runs here, so it stays up across trader restarts.

Architecture:
  WebSocket --> EventBus.publish() --> {local recorders, Redis pub/sub}
                                                              |
                                                              v
                                              trader process (RedisEventBridge)

Activation:
  Set ``recorder_split_mode=true`` in Settings. When true:
    * Run the recorder via ``python -m src.recorder_main``
    * Run the trader via ``python -m src.main`` (it will skip TickerManager
      creation and use a RedisEventBridge to receive ticks).
  When false (default), src.main runs everything in-process exactly as it
  did before this split was introduced — backward compatible.

Lifecycle:
  start: heartbeat first, then chain/VIX/tick recorders, then ticker
  stop:  ticker first (no new ticks arriving), then recorders flush, then
         heartbeat last (so any recorder-stop error is still visible)
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

from src.broker.zerodha.client import ZerodhaClient
from src.broker.zerodha.instruments import InstrumentManager
from src.broker.zerodha.ticker import TickerManager
from src.config import Settings
from src.core.clock import MarketClock, now_ist
from src.core.events import EventBus, EventType
from src.core.types import OptionType
from src.db.session import create_db_engine, create_session_factory
from src.market_data.chain_recorder import ChainSnapshotRecorder
from src.market_data.feed import TickFeedManager
from src.market_data.option_chain import OptionChainBuilder
from src.market_data.tick_recorder import TickRecorder
from src.market_data.vix_recorder import IndiaVixRecorder
from src.observability.heartbeat import Heartbeat
from src.utils.logging import setup_logging

logger = logging.getLogger(__name__)


HEARTBEAT_PATH = Path("data/heartbeat/recorder.json")


async def _build_recorder(settings: Settings) -> dict:
    """Wire just the recorder-side components.

    Mirrors the recorder section of ``src.main.create_app`` but drops every
    trader-only piece (broker for orders, OMS, risk, strategies, API).
    """

    # ─── Redis is mandatory in split mode ─────────────────────────
    # The trader subscribes to ticks via Redis pub/sub. No Redis = no IPC.
    from redis.asyncio import Redis
    redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis_client.ping()
    except Exception as e:
        logger.error(
            "Recorder requires Redis for tick fan-out to the trader. "
            "Got %r connecting to %s. Bring Redis up before starting the "
            "recorder.", e, settings.redis_url,
        )
        raise SystemExit(1)
    logger.info("Redis connected at %s", settings.redis_url)

    # ─── Database (instrument master only) ────────────────────────
    db_engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(db_engine)

    # ─── Core ─────────────────────────────────────────────────────
    event_bus = EventBus(redis_client=redis_client)
    clock = MarketClock()

    # ─── Broker (needed only for instrument master + option-chain seed) ──
    broker = ZerodhaClient(settings.kite_api_key, settings.kite_access_token)
    await broker.connect()

    instrument_manager = InstrumentManager(broker, session_factory)
    try:
        await instrument_manager.load_from_db()
    except Exception:
        logger.warning("Could not load instruments from DB; trying Kite API fallback")
        try:
            await instrument_manager.load_from_kite_api(
                settings.kite_api_key, settings.kite_access_token,
            )
        except Exception as e:
            logger.error("Kite API instrument fallback failed: %s", e)

    # ─── Market data plumbing ─────────────────────────────────────
    feed = TickFeedManager(event_bus, redis_client)
    chain_builder = OptionChainBuilder(event_bus, clock, redis_client)

    # Spot tokens — same constants the trader uses
    from src.market_data.simulator import BANKNIFTY_SPOT_TOKEN, NIFTY_SPOT_TOKEN
    chain_builder.register_spot(NIFTY_SPOT_TOKEN, "NIFTY")
    chain_builder.register_spot(BANKNIFTY_SPOT_TOKEN, "BANKNIFTY")

    # Register option instruments around ATM so chain builder can compute
    # IV / Greeks. The trader has its own chain builder; we don't share state.
    option_tokens: list[int] = []
    for underlying, step, num_strikes in [("NIFTY", 50, 30), ("BANKNIFTY", 100, 20)]:
        try:
            expiry = clock.next_expiry(underlying)
            options = instrument_manager.get_option_chain_instruments(underlying, expiry)
            if not options:
                logger.warning(
                    "No option instruments for %s expiry=%s — skipping",
                    underlying, expiry,
                )
                continue
            # Seed ATM via Kite LTP API (same logic main.py uses)
            spot_price = 0.0
            try:
                from kiteconnect import KiteConnect as _KC
                _kc = _KC(api_key=settings.kite_api_key)
                _kc.set_access_token(settings.kite_access_token)
                ltp_data = _kc.ltp([f"NSE:{underlying} 50"])
                spot_price = ltp_data.get(f"NSE:{underlying} 50", {}).get("last_price", 0)
            except Exception as e:
                logger.warning("Kite LTP for %s failed: %s", underlying, e)
            if spot_price <= 0:
                strikes = sorted({float(o.strike) for o in options})
                spot_price = strikes[len(strikes) // 3]
            atm = round(spot_price / step) * step
            lo, hi = atm - num_strikes * step, atm + num_strikes * step
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
                    option_tokens.append(inst.instrument_token)
                    registered += 1
            logger.info(
                "Registered %d %s options expiry=%s ATM~%.0f",
                registered, underlying, expiry, atm,
            )
        except Exception as e:
            logger.warning("Failed to register options for %s: %s", underlying, e)

    # ─── Heartbeat (process_name='recorder') ──────────────────────
    heartbeat = Heartbeat(
        process_name="recorder",
        output_path=HEARTBEAT_PATH,
        interval_s=30.0,
    )

    # ─── Recorders ────────────────────────────────────────────────
    chain_recorder = ChainSnapshotRecorder(
        chain_builder, clock, interval_seconds=60, heartbeat=heartbeat,
    )
    vix_recorder = IndiaVixRecorder(event_bus, clock, heartbeat=heartbeat)
    tick_recorder = TickRecorder(event_bus, clock, heartbeat=heartbeat)

    # ─── Ticker (owns the WebSocket) ──────────────────────────────
    ticker = TickerManager(
        settings.kite_api_key, settings.kite_access_token, event_bus,
    )

    # Wire chain builder to receive every tick
    event_bus.subscribe(EventType.TICK, chain_builder.on_tick)

    return {
        "settings": settings,
        "redis": redis_client,
        "db_engine": db_engine,
        "event_bus": event_bus,
        "clock": clock,
        "broker": broker,
        "instrument_manager": instrument_manager,
        "feed": feed,
        "chain_builder": chain_builder,
        "heartbeat": heartbeat,
        "chain_recorder": chain_recorder,
        "vix_recorder": vix_recorder,
        "tick_recorder": tick_recorder,
        "ticker": ticker,
        "option_tokens": option_tokens,
    }


async def run() -> None:
    settings = Settings()
    # JSONL sink — separate file from the trader so a tail of one doesn't
    # interleave with the other (DATA_RELIABILITY_PLAN §8.2).
    setup_logging(
        settings.log_level,
        json_output=settings.is_production,
        process_name="recorder",
        log_file=Path("data/logs/recorder.jsonl"),
    )

    logger.info("=" * 60)
    logger.info("Recorder process starting (data capture only — no strategies)")
    logger.info("Heartbeat file: %s", HEARTBEAT_PATH)
    logger.info("=" * 60)

    if not (settings.kite_api_key and settings.kite_access_token):
        logger.error(
            "Recorder needs KITE_API_KEY and KITE_ACCESS_TOKEN. "
            "Run scripts/auto_auth.py before starting the recorder."
        )
        sys.exit(1)

    app = await _build_recorder(settings)

    # Start order: heartbeat first so a watchdog polling between launch and
    # first recorder start sees a fresh file (not yesterday's stale one).
    await app["event_bus"].start()
    await app["heartbeat"].start()
    await app["chain_recorder"].start()
    await app["vix_recorder"].start()
    await app["tick_recorder"].start()

    # Subscribe to the option tokens (and spot tokens) before opening the WS
    tokens = list(set(app["option_tokens"]) | {
        # Always include the two spot tokens the chain builder cares about
        # (so OI/IV deltas have a reference) and the VIX index.
        # (recorder doesn't subscribe to OHLC tokens — strategies do that.)
    })
    from src.core.constants import INDIA_VIX_TOKEN
    from src.market_data.simulator import BANKNIFTY_SPOT_TOKEN, NIFTY_SPOT_TOKEN
    tokens = list(set(tokens) | {NIFTY_SPOT_TOKEN, BANKNIFTY_SPOT_TOKEN, INDIA_VIX_TOKEN})

    if tokens:
        app["ticker"].subscribe(tokens)
        logger.info("Recorder will subscribe to %d tokens on connect", len(tokens))
    await app["ticker"].start()

    # Graceful shutdown wiring
    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("Recorder received shutdown signal")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # Signal handlers aren't supported on Windows event loops; ignore.
            pass

    logger.info("Recorder running — Ctrl+C to stop")
    await stop_event.wait()

    # ─── Shutdown ─────────────────────────────────────────────────
    logger.info("Recorder shutting down...")

    # 1. Close the WS so no more ticks arrive
    try:
        await app["ticker"].stop()
    except Exception:
        logger.exception("Error stopping ticker")

    # 2. Flush recorders (CSV writer, Parquet writer, VIX minute aggregator)
    for name in ("tick_recorder", "vix_recorder", "chain_recorder"):
        try:
            await app[name].stop()
        except Exception:
            logger.exception("Error stopping %s", name)

    # 3. Stop event bus
    try:
        await app["event_bus"].stop()
    except Exception:
        logger.exception("Error stopping event bus")

    # 4. Heartbeat last — so any error from steps 1-3 is captured in the
    # final heartbeat snapshot under "stopped_at".
    try:
        await app["heartbeat"].stop()
    except Exception:
        logger.exception("Error stopping heartbeat")

    # 5. Connections
    try:
        await app["broker"].disconnect()
    except Exception:
        logger.exception("Error disconnecting broker")
    try:
        await app["db_engine"].dispose()
    except Exception:
        logger.exception("Error disposing DB engine")
    try:
        await app["redis"].close()
    except Exception:
        logger.exception("Error closing Redis")

    logger.info("Recorder shutdown complete")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nRecorder interrupted")


if __name__ == "__main__":
    main()
