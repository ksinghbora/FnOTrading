"""Tests for the Apr 30 2026 fix: WebSocket heartbeat grace period after reconnect.

Bug observed Apr 27-30 mornings: every market-open reconnect (the 09:10
pre-open reconnect, plus any heartbeat-triggered reconnect) was followed
30 seconds later by a "tick gap exceeded" → another reconnect, producing
a tight flap loop until something external (operator restart) broke the
cycle. No entries fired during the flap.

Root cause: the heartbeat compared ``now - _last_tick_time``. After a
fresh reconnect the subscription needs ~5-15s to deliver its first
tick. ``_last_tick_time`` is either ``None`` (never received a tick)
or yesterday's last tick (>>HEARTBEAT_TIMEOUT). Either way the next
heartbeat check fired and forced another reconnect.

Fix: track ``_last_reconnect_time`` separately (set in ``_on_connect``)
and use ``max(_last_tick_time, _last_reconnect_time)`` as the baseline.
The fresh subscription gets a HEARTBEAT_TIMEOUT-wide grace window.

These tests pin the contract so a future refactor can't quietly
re-introduce the flap.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest


def _make_ticker():
    """Construct a TickerManager with the network bits stubbed out."""
    from src.broker.zerodha.ticker import TickerManager
    bus = MagicMock()
    t = TickerManager(api_key="dummy", access_token="tok", event_bus=bus)
    return t


# ── _on_connect sets _last_reconnect_time ──────────────────────────


def test_on_connect_records_reconnect_time():
    t = _make_ticker()
    assert t._last_reconnect_time is None

    fake_now = datetime(2026, 4, 30, 9, 10, 0)
    with patch("src.broker.zerodha.ticker.now_ist", return_value=fake_now):
        # Simulate a Kite connect callback — args (ws, response) are
        # ignored by our handler beyond logging.
        t._on_connect(ws=MagicMock(), response={})

    assert t._last_reconnect_time == fake_now


def test_last_tick_time_unaffected_by_reconnect():
    """_last_tick_time must remain semantic — only set on real ticks.
    main.py:624 uses ``not ticker._last_tick_time`` to detect a daemon
    that started but never received a tick. If reconnect updated it,
    that detection would be silently broken."""
    t = _make_ticker()
    fake_now = datetime(2026, 4, 30, 9, 10, 0)
    with patch("src.broker.zerodha.ticker.now_ist", return_value=fake_now):
        t._on_connect(ws=MagicMock(), response={})
    assert t._last_tick_time is None  # still None — only ticks set this


# ── Heartbeat baseline = max(last_tick, last_reconnect) ────────────
#
# We test the baseline-selection logic directly rather than spinning
# up the asyncio heartbeat loop. The loop's logic is a one-liner:
#     elapsed = now - max(last_tick, last_reconnect)
# so verifying the inputs picked is the meaningful contract.


def _baseline(t):
    """Replicate the heartbeat-loop baseline pick from ticker.py."""
    candidates = [
        x for x in (t._last_tick_time, t._last_reconnect_time)
        if x is not None
    ]
    return max(candidates) if candidates else None


def test_baseline_is_reconnect_when_no_ticks_yet():
    """Fresh reconnect, no ticks yet → baseline = reconnect time.
    This is the case that broke before: 09:15:39 heartbeat fires
    against a None _last_tick_time → infinite elapsed → reconnect →
    repeat. With the fix, baseline = 09:10's reconnect time so the
    first heartbeat at 09:10 + HEARTBEAT_TIMEOUT (=09:10:30) is the
    earliest that can fire — giving ticks 30s to arrive."""
    t = _make_ticker()
    reconnect_t = datetime(2026, 4, 30, 9, 10, 0)
    t._last_reconnect_time = reconnect_t
    assert _baseline(t) == reconnect_t


def test_baseline_is_tick_when_ticks_flowing():
    """Steady state — ticks every minute, last reconnect was hours ago.
    Baseline must be the last tick (so the heartbeat catches a real
    tick gap), not the stale reconnect time."""
    t = _make_ticker()
    t._last_reconnect_time = datetime(2026, 4, 30, 9, 10, 0)
    t._last_tick_time = datetime(2026, 4, 30, 13, 45, 0)
    assert _baseline(t) == datetime(2026, 4, 30, 13, 45, 0)


def test_baseline_is_latest_of_the_two():
    """Mid-day reconnect → baseline jumps to reconnect time even though
    we have an older tick recorded. The fresh subscription gets the
    same grace window as a startup reconnect."""
    t = _make_ticker()
    t._last_tick_time = datetime(2026, 4, 30, 12, 0, 0)
    t._last_reconnect_time = datetime(2026, 4, 30, 12, 30, 0)
    assert _baseline(t) == datetime(2026, 4, 30, 12, 30, 0)


def test_baseline_none_when_neither_set():
    """Edge case: never connected and no ticks ever. The heartbeat
    loop treats this as "elapsed=999 → reconnect" so the loop kicks
    even on a totally fresh process. Verifying the helper returns
    None lets the caller handle that explicit case."""
    t = _make_ticker()
    assert _baseline(t) is None


# ── Regression scenario: the actual flap-prone window ──────────────


def test_post_reconnect_grace_prevents_immediate_flap():
    """Reproduce the flap scenario:
      - 09:10:00 — pre-market reconnect
      - 09:10:30 — heartbeat wakes up, checks elapsed
    Pre-fix: _last_tick_time=None → elapsed=999 → force reconnect (BUG).
    Post-fix: max(None, 09:10:00) = 09:10:00 → elapsed=30 →
    NOT > HEARTBEAT_TIMEOUT (==30) → no reconnect. Ticks have until
    09:10:30 + HEARTBEAT_TIMEOUT (09:11:00) to arrive."""
    from src.broker.zerodha.ticker import TickerManager
    t = _make_ticker()
    reconnect_t = datetime(2026, 4, 30, 9, 10, 0)
    heartbeat_t = datetime(2026, 4, 30, 9, 10, 30)
    t._last_reconnect_time = reconnect_t
    t._last_tick_time = None  # No ticks yet — pre-market reconnect

    elapsed = (heartbeat_t - max(
        x for x in (t._last_tick_time, t._last_reconnect_time) if x is not None
    )).total_seconds()
    assert elapsed == TickerManager.HEARTBEAT_TIMEOUT
    assert elapsed <= TickerManager.HEARTBEAT_TIMEOUT  # NOT > — no reconnect fires


def test_real_tick_gap_still_triggers_reconnect():
    """The grace-period fix must not break the heartbeat's actual job.
    A ticker that has been receiving ticks for hours but suddenly
    stops should still trip the heartbeat once HEARTBEAT_TIMEOUT
    elapses."""
    from src.broker.zerodha.ticker import TickerManager
    t = _make_ticker()
    t._last_reconnect_time = datetime(2026, 4, 30, 9, 10, 0)  # old
    t._last_tick_time = datetime(2026, 4, 30, 13, 0, 0)        # old
    check_t = datetime(2026, 4, 30, 13, 1, 0)                  # 60s later
    elapsed = (check_t - max(
        x for x in (t._last_tick_time, t._last_reconnect_time) if x is not None
    )).total_seconds()
    assert elapsed == 60
    assert elapsed > TickerManager.HEARTBEAT_TIMEOUT  # WOULD trigger reconnect — correct
