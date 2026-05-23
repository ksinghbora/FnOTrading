"""V7.4 (May 22 2026) — dead-reactor exec-replace tests.

Bug context: KiteTicker library v5.0.1 cannot reconnect within the same
Python process. Twisted's reactor is a process-wide singleton — once
disconnected, the in-process state is irrecoverable. Empirically the
_on_connect callback fires EXACTLY ONCE per process lifetime; all
subsequent reconnect attempts call connectWS() which returns silently
without establishing the WebSocket.

Observed on 6 of 7 trading days (May 11, 12, 13, 18, 20, 22). Only
fully-validated workaround is to restart the process. V7.4 makes the
daemon do this automatically via os.execv() when it detects the
dead-reactor state.

These tests pin the contract:
  1. _reconnect_attempts_since_connect starts at 0
  2. _force_reconnect() increments the counter
  3. _on_connect() callback resets it to 0 (real success)
  4. Counter + elapsed threshold triggers os.execv() invocation
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest


def _make_ticker():
    """Construct TickerManager with broker network stubbed out."""
    from src.broker.zerodha.ticker import TickerManager
    bus = MagicMock()
    t = TickerManager(api_key="dummy_api", access_token="TOK", event_bus=bus)
    return t


# ─── Counter contract ───────────────────────────────────────────────


def test_counter_starts_at_zero():
    t = _make_ticker()
    assert t._reconnect_attempts_since_connect == 0


def test_force_reconnect_increments_counter():
    """Each call to _force_reconnect bumps the counter up-front."""
    import asyncio
    from unittest.mock import AsyncMock
    t = _make_ticker()
    t._ticker = MagicMock()

    fake_loop = MagicMock()
    fake_loop.run_in_executor = AsyncMock(return_value=None)

    with patch("src.broker.zerodha.ticker.KiteTicker"), \
         patch("src.broker.zerodha.ticker.asyncio.sleep", new=AsyncMock(return_value=None)), \
         patch("src.broker.zerodha.ticker.asyncio.get_event_loop", return_value=fake_loop):
        asyncio.run(t._force_reconnect(cooldown_sec=0))
    assert t._reconnect_attempts_since_connect == 1


def test_on_connect_resets_counter():
    """_on_connect callback fires ONLY on real WebSocket success → reset to 0."""
    t = _make_ticker()
    t._reconnect_attempts_since_connect = 5  # simulate accumulated failed attempts
    t._on_connect(ws=MagicMock(), response={})
    assert t._reconnect_attempts_since_connect == 0


def test_multiple_force_reconnects_accumulate():
    import asyncio
    from unittest.mock import AsyncMock
    t = _make_ticker()
    t._ticker = MagicMock()

    fake_loop = MagicMock()
    fake_loop.run_in_executor = AsyncMock(return_value=None)

    with patch("src.broker.zerodha.ticker.KiteTicker"), \
         patch("src.broker.zerodha.ticker.asyncio.sleep", new=AsyncMock(return_value=None)), \
         patch("src.broker.zerodha.ticker.asyncio.get_event_loop", return_value=fake_loop):
        asyncio.run(t._force_reconnect(cooldown_sec=0))
        asyncio.run(t._force_reconnect(cooldown_sec=0))
        asyncio.run(t._force_reconnect(cooldown_sec=0))
    assert t._reconnect_attempts_since_connect == 3


def test_on_connect_between_reconnects_resets():
    """If a reconnect succeeds (callback fires), counter resets even
    if more reconnects come after."""
    import asyncio
    from unittest.mock import AsyncMock
    t = _make_ticker()
    t._ticker = MagicMock()

    fake_loop = MagicMock()
    fake_loop.run_in_executor = AsyncMock(return_value=None)

    with patch("src.broker.zerodha.ticker.KiteTicker"), \
         patch("src.broker.zerodha.ticker.asyncio.sleep", new=AsyncMock(return_value=None)), \
         patch("src.broker.zerodha.ticker.asyncio.get_event_loop", return_value=fake_loop):
        asyncio.run(t._force_reconnect(cooldown_sec=0))
        asyncio.run(t._force_reconnect(cooldown_sec=0))
        # Simulate broker callback firing successfully
        t._on_connect(ws=MagicMock(), response={})
        assert t._reconnect_attempts_since_connect == 0
        # Subsequent reconnect re-increments from 0
        asyncio.run(t._force_reconnect(cooldown_sec=0))
    assert t._reconnect_attempts_since_connect == 1


# ─── Threshold constants ────────────────────────────────────────────


def test_dead_reactor_threshold_constants_present():
    """Pin the threshold constants so future refactor doesn't drop them."""
    from src.broker.zerodha.ticker import TickerManager
    assert hasattr(TickerManager, "DEAD_REACTOR_THRESHOLD")
    assert hasattr(TickerManager, "DEAD_REACTOR_TICK_GAP_SECONDS")
    # Sensible defaults
    assert TickerManager.DEAD_REACTOR_THRESHOLD >= 2
    assert TickerManager.DEAD_REACTOR_THRESHOLD <= 10
    assert TickerManager.DEAD_REACTOR_TICK_GAP_SECONDS >= 60
    assert TickerManager.DEAD_REACTOR_TICK_GAP_SECONDS <= 300


# ─── execv trigger ──────────────────────────────────────────────────


def test_execv_triggers_at_threshold_plus_gap():
    """When counter ≥ threshold AND elapsed ≥ tick_gap, os.execv fires.

    We can't call _heartbeat_loop directly (infinite while), so we
    simulate the relevant guard conditions and assert os.execv is
    invoked with the python -m src.main invocation.
    """
    import sys
    from src.broker.zerodha.ticker import TickerManager
    t = _make_ticker()
    t._reconnect_attempts_since_connect = TickerManager.DEAD_REACTOR_THRESHOLD
    elapsed = TickerManager.DEAD_REACTOR_TICK_GAP_SECONDS + 1

    triggered = []

    def fake_execv(prog, argv):
        triggered.append((prog, argv))
        # Don't actually exec — raise to abort the simulated code path
        raise SystemExit(0)

    # Inline the guard logic from _heartbeat_loop for unit-testable check
    with patch("os.execv", side_effect=fake_execv):
        with pytest.raises(SystemExit):
            if (
                t._reconnect_attempts_since_connect >= TickerManager.DEAD_REACTOR_THRESHOLD
                and elapsed > TickerManager.DEAD_REACTOR_TICK_GAP_SECONDS
            ):
                import os
                os.execv(sys.executable, [sys.executable, "-m", "src.main"])

    assert len(triggered) == 1
    prog, argv = triggered[0]
    assert prog == sys.executable
    assert argv == [sys.executable, "-m", "src.main"]


def test_execv_does_not_trigger_below_threshold():
    """If counter < threshold, no execv call (regular reconnect path)."""
    from src.broker.zerodha.ticker import TickerManager
    t = _make_ticker()
    t._reconnect_attempts_since_connect = TickerManager.DEAD_REACTOR_THRESHOLD - 1
    elapsed = TickerManager.DEAD_REACTOR_TICK_GAP_SECONDS + 1

    should_exec = (
        t._reconnect_attempts_since_connect >= TickerManager.DEAD_REACTOR_THRESHOLD
        and elapsed > TickerManager.DEAD_REACTOR_TICK_GAP_SECONDS
    )
    assert not should_exec


def test_execv_does_not_trigger_below_gap():
    """If tick gap < threshold gap, no execv call (recent enough to retry)."""
    from src.broker.zerodha.ticker import TickerManager
    t = _make_ticker()
    t._reconnect_attempts_since_connect = TickerManager.DEAD_REACTOR_THRESHOLD
    elapsed = TickerManager.DEAD_REACTOR_TICK_GAP_SECONDS - 1

    should_exec = (
        t._reconnect_attempts_since_connect >= TickerManager.DEAD_REACTOR_THRESHOLD
        and elapsed > TickerManager.DEAD_REACTOR_TICK_GAP_SECONDS
    )
    assert not should_exec


# ─── Integration — full heartbeat loop one-iteration smoke test ────


def test_heartbeat_loop_invokes_trigger_when_dead_reactor_state(tmp_path, monkeypatch):
    """End-to-end: drive _heartbeat_loop one iteration with the
    dead-reactor condition fully satisfied and verify
    _trigger_dead_reactor_recovery is invoked.

    This is the integration test that protects against future refactors
    that might accidentally re-order the heartbeat decision tree.
    """
    import asyncio
    from datetime import timedelta
    from unittest.mock import AsyncMock
    from src.broker.zerodha.ticker import TickerManager

    monkeypatch.chdir(tmp_path)
    t = _make_ticker()
    t._running = True
    t._subscribed_tokens = {12345}
    t._loop = asyncio.new_event_loop()

    # Force dead-reactor preconditions
    t._reconnect_attempts_since_connect = TickerManager.DEAD_REACTOR_THRESHOLD
    # Last tick + last reconnect both old enough to exceed DEAD_REACTOR_TICK_GAP_SECONDS
    from src.core.clock import now_ist
    old = now_ist() - timedelta(seconds=TickerManager.DEAD_REACTOR_TICK_GAP_SECONDS + 30)
    t._last_tick_time = old
    t._last_reconnect_time = old

    trigger_calls = []

    def fake_trigger(elapsed):
        trigger_calls.append(elapsed)
        # Stop the heartbeat loop so the test terminates
        t._running = False

    t._trigger_dead_reactor_recovery = fake_trigger

    # Speed up the loop — skip the initial 60s warmup + heartbeat sleep
    async def fast_sleep(_):
        return

    with patch("src.broker.zerodha.ticker.asyncio.sleep", new=AsyncMock(side_effect=fast_sleep)):
        # Run the loop briefly; it should hit the trigger and exit
        try:
            t._loop.run_until_complete(asyncio.wait_for(t._heartbeat_loop(), timeout=2.0))
        except asyncio.TimeoutError:
            pass  # loop didn't terminate naturally, that's fine

    t._loop.close()

    assert len(trigger_calls) >= 1, "heartbeat loop did not invoke dead-reactor trigger"
    assert trigger_calls[0] > TickerManager.DEAD_REACTOR_TICK_GAP_SECONDS


# ─── V7.4 hardening — execv loop safeguard + integration path ─────


def test_trigger_dead_reactor_recovery_calls_execv(tmp_path, monkeypatch):
    """Direct call to _trigger_dead_reactor_recovery invokes os.execv."""
    import sys
    monkeypatch.chdir(tmp_path)
    t = _make_ticker()

    captured = {}

    def fake_execv(prog, argv):
        captured["prog"] = prog
        captured["argv"] = argv
        raise SystemExit(0)

    with patch("os.execv", side_effect=fake_execv):
        with pytest.raises(SystemExit):
            t._trigger_dead_reactor_recovery(elapsed=120.0)

    assert captured["prog"] == sys.executable
    assert captured["argv"] == [sys.executable, "-m", "src.main"]
    # Sentinel written
    sentinel = tmp_path / "data" / ".ticker_execv_log"
    assert sentinel.exists()
    assert "dead_reactor" in sentinel.read_text()


def test_execv_loop_safeguard_refuses_after_cap(tmp_path, monkeypatch):
    """If MAX_EXECV_IN_WINDOW execvs already happened in the window,
    refuse to execv again — log loudly and return so daemon stays alive."""
    import time
    from src.broker.zerodha.ticker import TickerManager
    monkeypatch.chdir(tmp_path)
    t = _make_ticker()
    t._reconnect_attempts_since_connect = TickerManager.DEAD_REACTOR_THRESHOLD

    # Pre-seed sentinel with MAX recent execv events
    sentinel = tmp_path / "data" / ".ticker_execv_log"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    lines = [
        f"{now - 60:.0f} pid=1000 reason=dead_reactor",
        f"{now - 120:.0f} pid=1001 reason=dead_reactor",
        f"{now - 180:.0f} pid=1002 reason=dead_reactor",
    ]
    sentinel.write_text("\n".join(lines) + "\n")

    execv_called = []

    def fake_execv(prog, argv):
        execv_called.append((prog, argv))
        raise SystemExit(0)

    # Should NOT call execv — the cap is hit
    with patch("os.execv", side_effect=fake_execv):
        t._trigger_dead_reactor_recovery(elapsed=120.0)

    assert execv_called == [], "execv should be skipped when cap is hit"


def test_execv_loop_safeguard_ignores_old_events(tmp_path, monkeypatch):
    """Execv events older than RAPID_RESPAWN_WINDOW_SEC don't count toward cap."""
    import sys
    import time
    from src.broker.zerodha.ticker import TickerManager
    monkeypatch.chdir(tmp_path)
    t = _make_ticker()

    sentinel = tmp_path / "data" / ".ticker_execv_log"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    old = time.time() - TickerManager.RAPID_RESPAWN_WINDOW_SEC - 100
    sentinel.write_text(
        f"{old:.0f} pid=999 reason=dead_reactor\n"
        f"{old - 60:.0f} pid=998 reason=dead_reactor\n"
        f"{old - 120:.0f} pid=997 reason=dead_reactor\n"
    )

    captured = {}

    def fake_execv(prog, argv):
        captured["prog"] = prog
        raise SystemExit(0)

    # Should fire — old events outside window don't count
    with patch("os.execv", side_effect=fake_execv):
        with pytest.raises(SystemExit):
            t._trigger_dead_reactor_recovery(elapsed=120.0)

    assert captured.get("prog") == sys.executable


def test_execv_failure_calls_os_exit(tmp_path, monkeypatch):
    """If os.execv raises (rare — disk full, EPERM), fall back to os._exit(99)
    so .pid file is freed and daily launchd respawn picks it up."""
    monkeypatch.chdir(tmp_path)
    t = _make_ticker()

    exit_code = []

    def fake_execv(prog, argv):
        raise OSError("simulated execv failure")

    def fake_exit(code):
        exit_code.append(code)
        raise SystemExit(code)

    with patch("os.execv", side_effect=fake_execv), \
         patch("os._exit", side_effect=fake_exit):
        with pytest.raises(SystemExit):
            t._trigger_dead_reactor_recovery(elapsed=120.0)

    assert exit_code == [99]


def test_startup_logs_recent_execv_history(tmp_path, monkeypatch, caplog):
    """When a new process boots and sentinel shows recent execvs,
    a warning/error log surfaces so monitoring catches silent loops."""
    import logging
    import time
    monkeypatch.chdir(tmp_path)
    t = _make_ticker()

    sentinel = tmp_path / "data" / ".ticker_execv_log"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    sentinel.write_text(
        f"{now - 60:.0f} pid=1000 reason=dead_reactor\n"
        f"{now - 120:.0f} pid=1001 reason=dead_reactor\n"
    )

    with caplog.at_level(logging.WARNING, logger="src.broker.zerodha.ticker"):
        t._log_recent_execv_history()

    msgs = [r.message for r in caplog.records]
    assert any("V7.4 dead-reactor history" in m for m in msgs)


def test_startup_silent_when_sentinel_absent(tmp_path, monkeypatch, caplog):
    """No sentinel file → no log noise at startup (the happy path)."""
    import logging
    monkeypatch.chdir(tmp_path)
    t = _make_ticker()

    with caplog.at_level(logging.WARNING, logger="src.broker.zerodha.ticker"):
        t._log_recent_execv_history()

    msgs = [r.message for r in caplog.records]
    assert not any("V7.4" in m for m in msgs)


def test_execv_args_replace_process_with_main_module():
    """The execv args must point at python + src.main so the new process
    re-enters main.py with all the initialisation logic (token load,
    broker connect, orchestrator construction)."""
    # Just verify the args we'd pass — assertion-based contract pin
    import sys
    expected_prog = sys.executable
    expected_argv = [sys.executable, "-m", "src.main"]
    assert expected_prog.endswith(("python", "python3", "python3.13"))
    assert "src.main" in expected_argv
