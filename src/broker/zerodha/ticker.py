"""KiteTicker WebSocket wrapper for real-time market data."""

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable

from kiteconnect import KiteTicker

from src.core.clock import now_ist
from src.core.events import Event, EventBus, EventType
from src.core.models import Tick
from src.utils.log_tags import Tag

logger = logging.getLogger(__name__)


class TickerManager:
    """Wraps KiteTicker WebSocket for async integration with the EventBus."""

    HEARTBEAT_TIMEOUT = 30  # seconds without ticks to consider connection dead

    def __init__(
        self,
        api_key: str,
        access_token: str,
        event_bus: EventBus,
    ):
        self._api_key = api_key
        self._access_token = access_token
        self._event_bus = event_bus
        self._ticker: KiteTicker | None = None
        self._subscribed_tokens: set[int] = set()
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_tick_time: datetime | None = None
        # Apr 30 2026: track the last successful WebSocket connect/reconnect
        # separately from the last tick. Used by the heartbeat loop to give
        # a fresh subscription a grace period (HEARTBEAT_TIMEOUT seconds)
        # to start delivering ticks before complaining. Without this, a
        # market-open reconnect at 09:10 + the first post-open tick at
        # 09:15 would have ``_last_tick_time = None`` (or yesterday's
        # last tick), so the heartbeat at 09:15:39 fired "tick gap
        # exceeded" → forced reconnect → repeat every 30s. The flap
        # blocked entries for the entire morning. _last_tick_time stays
        # semantic — "when was the last REAL tick" — because main.py:624
        # uses it to decide whether the market-open ticker needs a full
        # restart, and that check must still see None when no ticks have
        # arrived since startup.
        self._last_reconnect_time: datetime | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._tick_log_count: int = 0
        # V7.2 (May 18 2026): flag set by update_token() to signal the
        # heartbeat loop that an aggressive (longer-cooldown) reconnect
        # is required. Cleared after the aggressive reconnect completes.
        self._token_refreshed_pending_reconnect: bool = False
        # V7.4 (May 22 2026): consecutive failed reconnect counter.
        #
        # Root cause: KiteTicker library v5.0.1 cannot reconnect within
        # the same Python process — Twisted's reactor is a singleton
        # that can't be restarted after disconnect. Empirically the
        # _on_connect callback fires exactly ONCE per process lifetime.
        # All in-process reconnect attempts after a disconnect call
        # connectWS() on a broken reactor and return silently without
        # actually establishing the WebSocket.
        #
        # The ONLY working "reconnect" is to restart the process. This
        # counter tracks how many force_reconnect attempts have been
        # made since the last successful _on_connect callback. When it
        # exceeds DEAD_REACTOR_THRESHOLD AND tick gap exceeds the
        # dead-reactor exit gap, the heartbeat loop calls os._exit(42)
        # to trigger launchd respawn (KeepAlive=true with
        # SuccessfulExit=false on the plist).
        self._reconnect_attempts_since_connect: int = 0

    # V7.4 thresholds for dead-reactor exec-replace logic.
    # After N failed reconnects without an _on_connect callback firing,
    # AND no real tick for X seconds, we declare the reactor dead and
    # call os.execv() to replace the process image with a fresh Python
    # interpreter. The new process has a fresh Twisted reactor and
    # picks up the latest .env (incl. fresh access token).
    #
    # Why os.execv() instead of os._exit() + launchd KeepAlive:
    # The daemon is a GRANDCHILD of launchd (started via
    # restart_daemon.sh double-fork pattern). launchd's KeepAlive only
    # tracks direct children, so it cannot respawn the grandchild
    # daemon on exit. os.execv() replaces the process image in-place,
    # keeping the same PID (so the .pid file stays valid) and PPID
    # (still PID 1), but loading fresh Python code with a fresh
    # Twisted reactor. No external respawn mechanism needed.
    DEAD_REACTOR_THRESHOLD: int = 3            # consecutive failed reconnects
    DEAD_REACTOR_TICK_GAP_SECONDS: int = 90    # no tick for this long

    async def start(self) -> None:
        """Start the WebSocket ticker in a background thread."""
        # V7.4: on startup, surface any recent execv events so an
        # operator sees the daemon has been auto-recovering. This is
        # how a silent dead-reactor loop becomes visible.
        self._log_recent_execv_history()
        self._loop = asyncio.get_event_loop()
        self._ticker = KiteTicker(self._api_key, self._access_token)

        # Register callbacks
        self._ticker.on_ticks = self._on_ticks
        self._ticker.on_connect = self._on_connect
        self._ticker.on_close = self._on_close
        self._ticker.on_error = self._on_error
        self._ticker.on_reconnect = self._on_reconnect

        self._running = True
        # KiteTicker.connect() is blocking, run in thread
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._ticker.connect(threaded=True)
        )
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("KiteTicker started")

    async def stop(self) -> None:
        """Stop the WebSocket ticker."""
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        if self._ticker:
            self._ticker.close()
            self._ticker = None
        logger.info("KiteTicker stopped")

    def subscribe(self, tokens: list[int], mode: str = "full") -> None:
        """Subscribe to instrument tokens.

        Modes: 'full' (all data), 'quote' (no depth), 'ltp' (only LTP)
        """
        if not self._ticker:
            # Store for subscription on connect
            self._subscribed_tokens.update(tokens)
            return

        new_tokens = [t for t in tokens if t not in self._subscribed_tokens]
        if new_tokens:
            self._ticker.subscribe(new_tokens)
            self._ticker.set_mode(mode, new_tokens)
            self._subscribed_tokens.update(new_tokens)
            logger.info(f"Subscribed to {len(new_tokens)} tokens (total: {len(self._subscribed_tokens)})")

    def unsubscribe(self, tokens: list[int]) -> None:
        """Unsubscribe from instrument tokens."""
        if self._ticker:
            self._ticker.unsubscribe(tokens)
        self._subscribed_tokens -= set(tokens)

    def _on_ticks(self, ws: Any, ticks: list[dict]) -> None:
        """Callback: received tick data from WebSocket."""
        if not self._loop or not self._running:
            logger.warning(f"Dropping {len(ticks)} ticks: loop={self._loop is not None} running={self._running}")
            return

        self._last_tick_time = now_ist()
        if self._tick_log_count < 3:
            self._tick_log_count += 1
            # Typed tag — JSONL queries filter on tag="TICKER" instead of grepping
            # for "[TICKER]" in free text (DATA_RELIABILITY_PLAN §8.2).
            logger.info(
                "tick batch received",
                extra={
                    "tag": Tag.TICKER,
                    "batch_size": len(ticks),
                    "batch_number": self._tick_log_count,
                },
            )

        for tick_data in ticks:
            try:
                tick = self._parse_tick(tick_data)
                event = Event.create(
                    EventType.TICK,
                    source="ticker",
                    tick=tick.model_dump(),
                )
                asyncio.run_coroutine_threadsafe(
                    self._event_bus.publish(event), self._loop
                )
            except Exception:
                logger.exception("Error parsing tick data")

    def _on_connect(self, ws: Any, response: Any) -> None:
        """Callback: WebSocket connected.

        V7.4 (May 22 2026): resets the dead-reactor reconnect counter.
        This callback ONLY fires on a TRULY successful WebSocket
        handshake — so seeing it means the connection actually opened
        (not just the connect() call returned). Resetting the counter
        here is what distinguishes a real reconnect from the silent
        failures observed in May 11-22 2026.
        """
        # Mark the connect time so the heartbeat grants the new subscription
        # a HEARTBEAT_TIMEOUT-wide grace window before declaring tick gap.
        # Apr 30 2026 fix — see _last_reconnect_time docstring.
        self._last_reconnect_time = now_ist()
        # V7.4: real connect succeeded, reset the dead-reactor counter
        self._reconnect_attempts_since_connect = 0
        logger.info(
            "ticker connected",
            extra={"tag": Tag.TICKER, "subscribed_tokens": len(self._subscribed_tokens)},
        )
        # Re-subscribe to all tokens
        if self._subscribed_tokens:
            token_list = list(self._subscribed_tokens)
            try:
                ws.subscribe(token_list)
                ws.set_mode(ws.MODE_FULL, token_list)
                logger.info(f"Re-subscribed to {len(token_list)} tokens")
            except Exception:
                logger.exception(
                    f"Failed to re-subscribe {len(token_list)} tokens on reconnect"
                )

        if self._loop:
            event = Event.create(EventType.CONNECTION_RESTORED, source="ticker")
            asyncio.run_coroutine_threadsafe(
                self._event_bus.publish(event), self._loop
            )

    def _on_close(self, ws: Any, code: int, reason: str) -> None:
        """Callback: WebSocket disconnected.

        Does NOT publish CONNECTION_LOST — the heartbeat loop handles reconnect
        detection and notification to avoid duplicate/spammy Telegram alerts.
        """
        logger.warning(
            "ticker disconnected",
            extra={"tag": Tag.TICKER, "code": code, "reason": reason},
        )

    def _on_error(self, ws: Any, code: int, reason: str) -> None:
        """Callback: WebSocket error."""
        logger.error(
            "ticker error",
            extra={"tag": Tag.TICKER, "code": code, "reason": reason},
        )

    def _on_reconnect(self, ws: Any, attempts: int) -> None:
        """Callback: WebSocket reconnecting."""
        logger.info(
            "ticker reconnecting",
            extra={"tag": Tag.WS_RECONNECT, "attempt": attempts, "trigger": "auto"},
        )

    async def _heartbeat_loop(self) -> None:
        """Monitor tick flow — force reconnect if ticks stop arriving.

        Outside market hours: sleeps in 10s increments, reconnecting at 9:10 AM
        (5 min before open) to ensure a fresh connection before market open.
        During market hours: checks every HEARTBEAT_TIMEOUT seconds and reconnects
        immediately if no ticks received.
        """
        await asyncio.sleep(60)
        _market_open_reconnect_done = False

        while self._running:
            try:
                # V7.2: aggressive reconnect on token-refresh signal.
                # Check FIRST every iteration so this fires regardless of
                # market hours or other timing checks.
                if self._token_refreshed_pending_reconnect:
                    logger.warning(
                        "Token-refresh flag set — running aggressive reconnect "
                        "(close + 5s cooldown + fresh KiteTicker)"
                    )
                    self._token_refreshed_pending_reconnect = False
                    await self._force_reconnect(cooldown_sec=5)
                    # After aggressive reconnect, give 15s grace to receive
                    # ticks. If still no ticks, the heartbeat will catch it
                    # naturally in the next iteration.
                    await asyncio.sleep(15)
                    continue

                now = now_ist()
                hour, minute = now.hour, now.minute
                is_weekday = now.weekday() < 5
                is_market_hours = (
                    is_weekday
                    and ((hour == 9 and minute >= 15) or (9 < hour < 15) or (hour == 15 and minute <= 30))
                )
                # Reset the market-open flag after close
                if hour >= 15 and minute > 30:
                    _market_open_reconnect_done = False

                if not is_market_hours:
                    # Reconnect at 9:10 AM — 5 min before open, gives WebSocket time to stabilise.
                    # Window is 9:10-9:14 so the 10s sleep loop reliably catches it.
                    if is_weekday and hour == 9 and 10 <= minute < 15 and not _market_open_reconnect_done:
                        logger.info(f"9:{minute:02d} AM — forcing pre-market reconnect (5 min before open)")
                        await self._force_reconnect()
                        _market_open_reconnect_done = True
                    await asyncio.sleep(10)
                    continue

                # During market hours: check every HEARTBEAT_TIMEOUT seconds.
                # Apr 30 2026 fix — heartbeat baseline is the LATEST of the
                # last real tick and the last reconnect. A fresh subscription
                # needs ~5-15 seconds to deliver its first tick after the
                # broker accepts the subscribe call; without the reconnect
                # baseline the heartbeat would fire one HEARTBEAT_TIMEOUT
                # window after every reconnect and force another reconnect,
                # producing the 30s-flap loop observed Apr 27-30 mornings.
                # See _last_reconnect_time docstring on this class.
                await asyncio.sleep(self.HEARTBEAT_TIMEOUT)
                if not self._running or not self._subscribed_tokens:
                    continue

                # Heartbeat baseline: latest of (last real tick, last
                # reconnect). If neither is set, treat elapsed as
                # "infinite" so the heartbeat triggers (covers the
                # never-connected edge case).
                baselines = [
                    t for t in (self._last_tick_time, self._last_reconnect_time)
                    if t is not None
                ]
                if baselines:
                    elapsed = (now_ist() - max(baselines)).total_seconds()
                else:
                    elapsed = 999

                if elapsed > self.HEARTBEAT_TIMEOUT:
                    # V7.4 dead-reactor check: if we've reconnected N
                    # times without the on_connect callback firing AND
                    # ticks have been silent for the dead-reactor
                    # threshold, the Twisted reactor is irrecoverable.
                    # Replace the process image with a fresh Python
                    # interpreter via os.execv() — this guarantees a
                    # fresh Twisted reactor.
                    if (
                        self._reconnect_attempts_since_connect >= self.DEAD_REACTOR_THRESHOLD
                        and elapsed > self.DEAD_REACTOR_TICK_GAP_SECONDS
                    ):
                        self._trigger_dead_reactor_recovery(elapsed)

                    logger.warning(
                        "tick gap exceeded heartbeat — forcing reconnect",
                        extra={
                            "tag": Tag.WS_RECONNECT,
                            "elapsed_seconds": round(elapsed, 1),
                            "timeout_seconds": self.HEARTBEAT_TIMEOUT,
                            "trigger": "heartbeat",
                            "reconnect_attempts_since_connect": self._reconnect_attempts_since_connect,
                        },
                    )
                    event = Event.create(
                        EventType.CONNECTION_LOST,
                        source="ticker_heartbeat",
                        reason=f"No ticks for {elapsed:.0f}s",
                    )
                    await self._event_bus.publish(event)
                    await self._force_reconnect()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in heartbeat loop")

    def update_token(self, access_token: str) -> None:
        """Update access token AND schedule an immediate ticker reconnect.

        V7.2 fix (May 18 2026): the prior behaviour ("update field, reconnect
        at market open") was unreliable. Observed bug pattern:
          - Auto-auth refreshes token at 08:55 IST
          - `update_token` sets `self._access_token` but the running
            KiteTicker keeps its OWN internal access_token from construction
          - At 09:10 the heartbeat does a "pre-market reconnect"
          - At 09:15 market opens, ticks should flow
          - Instead, ticker enters a reconnect loop receiving zero ticks
            (heartbeat timeout every 30s, force_reconnect, repeat)
          - Loop continued for 1h22m on May 18; same issue May 13

        Root cause: even after `_force_reconnect` recreates the KiteTicker
        with fresh token, the new connection inherits some broken state.
        Empirically, a MANUAL daemon restart (which spawns a fresh process)
        resolves the issue immediately.

        Fix:
        1. Update the token field (as before).
        2. Set a flag that signals the heartbeat loop to do a TWO-STEP
           reconnect with a longer cool-down between close and reopen.
        3. The heartbeat loop reads this flag on next iteration and runs
           the full reconnect path (rather than deferring to market open).

        This still doesn't guarantee fix in all cases — the KiteTicker
        library's internal state may still be sticky — but it shortens
        the recovery window from ~1.5h to ~30s and gives operators a
        clearer signal that reconnect IS being attempted.
        """
        self._access_token = access_token
        # V7.2: flag for heartbeat loop to do an aggressive reconnect
        self._token_refreshed_pending_reconnect = True
        logger.info("Ticker access token updated — flagged for aggressive reconnect on next heartbeat tick")

    # V7.4 dead-reactor recovery — file-based execv-loop safeguard.
    #
    # Sentinel file records timestamps of every execv we trigger. On
    # startup, the next process reads the file and counts execs in the
    # last RAPID_RESPAWN_WINDOW_SEC seconds. If the count exceeds
    # MAX_EXECV_IN_WINDOW, _trigger_dead_reactor_recovery refuses to
    # execv again — it logs loudly and lets the daemon keep running in
    # a degraded state so an operator notices. Without this, a true
    # infrastructure-level outage (broker side, network) could put us
    # into a tight execv loop burning CPU and log volume.
    EXECV_SENTINEL_PATH: str = "data/.ticker_execv_log"
    RAPID_RESPAWN_WINDOW_SEC: int = 1800          # 30 minutes
    MAX_EXECV_IN_WINDOW: int = 3                  # 3 execs / 30 min cap

    def _log_recent_execv_history(self) -> None:
        """Read sentinel and log any execv events in the recent window.

        Called once at TickerManager.start(). Lets monitoring catch the
        case where the daemon is silently auto-recovering several times
        per hour due to dead-reactor — a sign the bug has worsened or
        an upstream broker issue is in play.
        """
        import time
        from pathlib import Path

        sentinel = Path(self.EXECV_SENTINEL_PATH)
        if not sentinel.exists():
            return

        now = time.time()
        recent: list[tuple[float, str]] = []
        try:
            for line in sentinel.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split(maxsplit=1)
                try:
                    ts = float(parts[0])
                except (ValueError, IndexError):
                    continue
                if now - ts < self.RAPID_RESPAWN_WINDOW_SEC:
                    recent.append((ts, parts[1] if len(parts) > 1 else ""))
        except Exception:
            logger.exception("Could not read execv sentinel at startup")
            return

        if not recent:
            return

        level = logger.error if len(recent) >= self.MAX_EXECV_IN_WINDOW else logger.warning
        level(
            "V7.4 dead-reactor history: %d execv event(s) in last %dmin",
            len(recent),
            self.RAPID_RESPAWN_WINDOW_SEC // 60,
            extra={"tag": Tag.TICKER, "execv_count_recent": len(recent)},
        )

    def _trigger_dead_reactor_recovery(self, elapsed: float) -> None:
        """Replace process image after sanity-checking against execv loops.

        Logs the decision, persists the timestamp, then either calls
        os.execv() or — if the recent-execv count exceeds the cap —
        refuses and reverts to the normal force-reconnect path so an
        operator can investigate.
        """
        import os
        import sys
        import time
        from pathlib import Path

        sentinel = Path(self.EXECV_SENTINEL_PATH)
        now = time.time()
        recent_execs: list[float] = []
        if sentinel.exists():
            try:
                for line in sentinel.read_text().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ts = float(line.split()[0])
                    except (ValueError, IndexError):
                        continue
                    if now - ts < self.RAPID_RESPAWN_WINDOW_SEC:
                        recent_execs.append(ts)
            except Exception:
                logger.exception("Could not read execv sentinel — proceeding anyway")

        if len(recent_execs) >= self.MAX_EXECV_IN_WINDOW:
            logger.error(
                "DEAD REACTOR detected but execv-loop cap hit — refusing to "
                "replace process. Operator intervention required.",
                extra={
                    "tag": Tag.TICKER,
                    "recent_execv_count": len(recent_execs),
                    "window_seconds": self.RAPID_RESPAWN_WINDOW_SEC,
                    "reconnect_attempts": self._reconnect_attempts_since_connect,
                    "elapsed_seconds": round(elapsed, 1),
                },
            )
            sys.stdout.flush()
            sys.stderr.flush()
            # Fall through — let the regular force_reconnect path run.
            # It won't actually recover (reactor is dead), but at least
            # the daemon stays alive so the operator can debug.
            return

        logger.error(
            "REACTOR DEAD — replacing process via os.execv()",
            extra={
                "tag": Tag.TICKER,
                "reconnect_attempts": self._reconnect_attempts_since_connect,
                "elapsed_seconds": round(elapsed, 1),
                "recent_execv_count": len(recent_execs),
            },
        )

        try:
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            with sentinel.open("a") as fh:
                fh.write(f"{now:.0f} pid={os.getpid()} reason=dead_reactor\n")
        except Exception:
            logger.exception("Could not persist execv sentinel — proceeding with execv anyway")

        sys.stdout.flush()
        sys.stderr.flush()

        try:
            os.execv(sys.executable, [sys.executable, "-m", "src.main"])
        except Exception as e:
            # execv failure is rare (e.g., disk full, EPERM). If it
            # happens we cannot recover in-process. Best we can do is
            # log + os._exit() so the .pid file is freed; an operator
            # or the daily launchd respawn at 09:02 IST picks it up.
            logger.exception("os.execv FAILED — process is unrecoverable, exiting")
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(99)  # noqa: SLF001 — intentional, see above
        # NOTREACHED on successful execv

    async def _force_reconnect(self, cooldown_sec: int = 2) -> None:
        """Close and reopen the WebSocket connection.

        Args:
          cooldown_sec: seconds to wait between close() and new ticker
                       creation. Default 2 (historical behaviour). V7.2
                       aggressive reconnect uses 5 to give the OS a wider
                       window to release the old socket / DNS / TLS state.

        V7.4 (May 22 2026): increments
        ``_reconnect_attempts_since_connect`` BEFORE the reconnect.
        On a real success, ``_on_connect`` callback fires and resets
        this counter to 0. If the counter grows past
        DEAD_REACTOR_THRESHOLD without resetting, the heartbeat loop
        will declare the reactor dead and trigger process exit.
        """
        # V7.4: count this attempt up-front. _on_connect will reset on success.
        self._reconnect_attempts_since_connect += 1
        try:
            # Read latest token from file (auto-auth may have refreshed it)
            from pathlib import Path
            token_file = Path(".kite_access_token")
            if token_file.exists():
                fresh_token = token_file.read_text().strip()
                if fresh_token and fresh_token != self._access_token:
                    self._access_token = fresh_token
                    logger.info("Ticker picked up fresh token from .kite_access_token")

            if self._ticker:
                logger.info(f"Force-closing ticker for reconnect (cooldown={cooldown_sec}s)...")
                try:
                    self._ticker.close()
                except Exception:
                    logger.exception("Error during ticker.close() — proceeding with new ticker anyway")
                # Drop the old reference explicitly so GC can reclaim sockets
                self._ticker = None
                await asyncio.sleep(cooldown_sec)

            self._ticker = KiteTicker(self._api_key, self._access_token)
            self._ticker.on_ticks = self._on_ticks
            self._ticker.on_connect = self._on_connect
            self._ticker.on_close = self._on_close
            self._ticker.on_error = self._on_error
            self._ticker.on_reconnect = self._on_reconnect

            await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._ticker.connect(threaded=True)
            )
            logger.info("Ticker reconnected — waiting for ticks")
        except Exception:
            logger.exception("Failed to force reconnect ticker")

    def _parse_tick(self, tick_data: dict) -> Tick:
        """Parse raw Kite tick data into our Tick model."""
        depth = tick_data.get("depth", {})
        buy_depth = depth.get("buy", [{}])
        sell_depth = depth.get("sell", [{}])

        return Tick(
            instrument_token=tick_data["instrument_token"],
            tradingsymbol=tick_data.get("tradingsymbol", ""),
            timestamp=tick_data.get("exchange_timestamp", now_ist()),
            ltp=Decimal(str(tick_data.get("last_price", 0))),
            volume=tick_data.get("volume_traded", 0),
            oi=tick_data.get("oi", 0),
            bid_price=Decimal(str(buy_depth[0].get("price", 0))) if buy_depth else Decimal("0"),
            ask_price=Decimal(str(sell_depth[0].get("price", 0))) if sell_depth else Decimal("0"),
            bid_qty=buy_depth[0].get("quantity", 0) if buy_depth else 0,
            ask_qty=sell_depth[0].get("quantity", 0) if sell_depth else 0,
            high=Decimal(str(tick_data.get("ohlc", {}).get("high", 0))),
            low=Decimal(str(tick_data.get("ohlc", {}).get("low", 0))),
            open=Decimal(str(tick_data.get("ohlc", {}).get("open", 0))),
            close=Decimal(str(tick_data.get("ohlc", {}).get("close", 0))),
        )
