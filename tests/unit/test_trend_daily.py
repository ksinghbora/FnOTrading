"""Unit tests for TrendDailyStrategy.

Tests the daily-bar accumulation, ATR / Donchian computation, and the
entry/exit logic in isolation. Engine-mode integration test (full
backtest run) is deferred — the existing scripts/smoke_trend_daily.py
covers the signal-edge measurement; these tests cover the production
strategy class's mechanics.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest


def _make_strategy(params_overrides: dict | None = None):
    """Build a TrendDailyStrategy with a stubbed context."""
    from src.strategy.implementations.trend_daily import TrendDailyStrategy
    from src.strategy.params import TrendDailyParams
    from src.market_data.simulator import NIFTY_SPOT_TOKEN

    p_kwargs = {"underlying": "NIFTY", "quantity_lots": 1}
    if params_overrides:
        p_kwargs.update(params_overrides)
    params = TrendDailyParams(**p_kwargs)
    s = TrendDailyStrategy("trend_daily_t", params)

    # Minimal stub context
    ctx = MagicMock()
    ctx.clock = MagicMock()
    ctx.get_spot_token = MagicMock(return_value=NIFTY_SPOT_TOKEN)
    ctx.get_vix = MagicMock(return_value=15.0)
    s.set_context(ctx)
    s._spot_token = NIFTY_SPOT_TOKEN
    return s, ctx


def _seed_bars(strategy, bars: list[dict], vix_means: list[float] | None = None):
    """Push pre-built daily bars into the buffer."""
    for b in bars:
        strategy._daily_bars.append(b)
    for v in vix_means or []:
        strategy._daily_vix.append(v)


def _make_bar(d: date, o: float, h: float, l: float, c: float) -> dict:
    return {"date": d, "open": o, "high": h, "low": l, "close": c}


def _make_tick(token: int, ltp: float, ts: datetime):
    """Build a minimal Tick-like object."""
    t = MagicMock()
    t.instrument_token = token
    t.ltp = Decimal(str(ltp))
    t.timestamp = ts
    return t


# ── Param defaults ──────────────────────────────────────────────────


def test_trend_daily_param_defaults():
    from src.strategy.params import TrendDailyParams
    p = TrendDailyParams()
    assert p.donchian_lookback == 20
    assert p.atr_period == 14
    assert p.atr_floor_pct == 0.5
    assert p.atr_stop_mult == 2.0
    assert p.vix_entry_min == 12.0
    assert p.vix_entry_max == 22.0
    assert p.max_hold_days == 30
    assert p.decision_time == dtime(15, 25)


def test_trend_daily_registered():
    from src.strategy.registry import list_strategies
    from src.backtest.common import import_strategies
    import_strategies()
    assert "trend_daily" in list_strategies()


# ── ATR computation ─────────────────────────────────────────────────


def test_atr_returns_none_with_insufficient_bars():
    """ATR(14) needs at least 15 bars; below that → None."""
    s, _ = _make_strategy()
    today = date(2026, 5, 1)
    for i in range(10):
        s._daily_bars.append(_make_bar(today + timedelta(days=i), 24000, 24050, 23950, 24000))
    assert s._compute_atr(14) is None


def test_atr_returns_value_with_enough_bars():
    """With 20 bars and a synthetic constant range, ATR converges to that range."""
    s, _ = _make_strategy()
    today = date(2026, 5, 1)
    # All bars: range = 100 points, no overnight gap
    for i in range(20):
        c = 24000 + i  # tiny drift
        s._daily_bars.append(_make_bar(today + timedelta(days=i), c, c + 50, c - 50, c))
    atr = s._compute_atr(14)
    assert atr is not None
    # True range each bar = max(100, |c - prev_close|, |c - prev_close|)
    # With drift +1 per day, TR ≈ 100 (range dominates)
    assert 95 < atr < 105


# ── Day rollover + buffer accumulation ─────────────────────────────


def test_day_rollover_appends_yesterday_to_buffer():
    """When a tick arrives with a new date, yesterday's running OHLC
    is pushed to _daily_bars."""
    from src.market_data.simulator import NIFTY_SPOT_TOKEN

    s, ctx = _make_strategy()
    # Day 1: simulate ticks via direct state manipulation (skipping the
    # full on_tick pipeline; we just want the rollover effect)
    s._today_date = date(2026, 5, 5)
    s._today_open = 24000
    s._today_high = 24100
    s._today_low = 23950
    s._today_close = 24050
    s._today_vix_sum = 30.0
    s._today_vix_count = 2  # mean = 15.0

    # Day 2 tick — should trigger rollover
    ctx.clock.now.return_value = datetime(2026, 5, 6, 9, 16, tzinfo=timezone.utc)
    tick = _make_tick(NIFTY_SPOT_TOKEN, 24070, ctx.clock.now.return_value)

    import asyncio
    asyncio.run(s.on_tick(tick))

    assert len(s._daily_bars) == 1
    assert s._daily_bars[0]["date"] == date(2026, 5, 5)
    assert s._daily_bars[0]["close"] == 24050
    assert len(s._daily_vix) == 1
    assert abs(s._daily_vix[0] - 15.0) < 1e-9
    # Today's OHLC reset and started accumulating
    assert s._today_date == date(2026, 5, 6)
    assert s._today_open == 24070
    assert s._today_close == 24070


# ── Entry logic ─────────────────────────────────────────────────────


def test_entry_long_on_donchian_breakout():
    """Close > donchian_high triggers a LONG entry signal."""
    s, ctx = _make_strategy()
    today = date(2026, 5, 6)
    # Seed 21 historical bars with high=24100 each → donchian_high = 24100
    bars = [_make_bar(today - timedelta(days=21 - i), 24000, 24100, 23900, 24000) for i in range(21)]
    _seed_bars(s, bars, vix_means=[15.0] * 21)
    s._today_date = today
    s._today_open = 24000
    s._today_high = 24200
    s._today_low = 23980
    s._today_close = 24200  # > 24100 → long breakout
    s._today_vix_sum = 30.0
    s._today_vix_count = 2

    sig = s._evaluate()
    assert sig is not None
    assert s._position == 1
    assert s._entry_px == 24200
    assert s._entry_atr is not None and s._entry_atr > 0


def test_entry_short_on_donchian_breakdown():
    """Close < donchian_low triggers a SHORT entry signal."""
    s, ctx = _make_strategy()
    today = date(2026, 5, 6)
    bars = [_make_bar(today - timedelta(days=21 - i), 24000, 24100, 23900, 24000) for i in range(21)]
    _seed_bars(s, bars, vix_means=[15.0] * 21)
    s._today_date = today
    s._today_open = 24000
    s._today_high = 24020
    s._today_low = 23800
    s._today_close = 23800  # < 23900 → short breakdown
    s._today_vix_sum = 30.0
    s._today_vix_count = 2

    sig = s._evaluate()
    assert sig is not None
    assert s._position == -1


def test_entry_blocked_when_vix_below_band():
    """VIX < 12 → entry blocked."""
    s, _ = _make_strategy()
    today = date(2026, 5, 6)
    bars = [_make_bar(today - timedelta(days=21 - i), 24000, 24100, 23900, 24000) for i in range(21)]
    _seed_bars(s, bars, vix_means=[10.0] * 21)
    s._today_date = today
    s._today_open = 24200
    s._today_high = 24200
    s._today_low = 23980
    s._today_close = 24200
    s._today_vix_sum = 20.0
    s._today_vix_count = 2  # mean = 10 — out of band

    sig = s._evaluate()
    assert sig is None
    assert s._position == 0


def test_entry_blocked_when_atr_too_low():
    """ATR%/spot < floor → entry blocked."""
    s, _ = _make_strategy({"atr_floor_pct": 5.0})  # 5% floor — unreachable
    today = date(2026, 5, 6)
    bars = [_make_bar(today - timedelta(days=21 - i), 24000, 24010, 23990, 24000) for i in range(21)]
    _seed_bars(s, bars, vix_means=[15.0] * 21)
    s._today_date = today
    s._today_open = 24200
    s._today_high = 24200
    s._today_low = 24180
    s._today_close = 24200
    s._today_vix_sum = 30.0
    s._today_vix_count = 2

    sig = s._evaluate()
    assert sig is None
    assert s._position == 0


def test_entry_blocked_during_warmup():
    """Less than donchian_lookback+1 bars → entry blocked."""
    s, _ = _make_strategy()
    today = date(2026, 5, 6)
    bars = [_make_bar(today - timedelta(days=10 - i), 24000, 24100, 23900, 24000) for i in range(10)]
    _seed_bars(s, bars, vix_means=[15.0] * 10)
    s._today_date = today
    s._today_close = 24200

    sig = s._evaluate()
    assert sig is None


# ── Exit logic ──────────────────────────────────────────────────────


def test_exit_on_max_hold():
    """When days_held >= max_hold_days, exit fires."""
    s, _ = _make_strategy({"max_hold_days": 5})
    today = date(2026, 5, 6)
    bars = [_make_bar(today - timedelta(days=21 - i), 24000, 24100, 23900, 24000) for i in range(21)]
    _seed_bars(s, bars, vix_means=[15.0] * 21)
    s._today_date = today
    s._today_close = 24050
    s._today_vix_sum = 30.0
    s._today_vix_count = 2
    s._position = 1
    s._entry_px = 24000
    s._entry_atr = 50.0
    s._peak_favorable = 24050
    s._days_held = 5  # at the cap

    sig = s._evaluate()
    assert sig is not None
    assert s._position == 0


def test_exit_on_trail_stop_long():
    """LONG: when close drops 2× ATR below peak, trail stop fires."""
    s, _ = _make_strategy()
    today = date(2026, 5, 6)
    bars = [_make_bar(today - timedelta(days=21 - i), 24000, 24100, 23900, 24000) for i in range(21)]
    _seed_bars(s, bars, vix_means=[15.0] * 21)
    s._today_date = today
    s._today_vix_sum = 30.0
    s._today_vix_count = 2
    s._position = 1
    s._entry_px = 24000
    s._entry_atr = 100.0  # so trail = peak - 200
    s._peak_favorable = 24500  # peak well above entry
    s._days_held = 3
    # close drops 250 below peak → triggers trail (24500 - 200 = 24300, close 24290 < 24300)
    s._today_close = 24290

    sig = s._evaluate()
    assert sig is not None
    assert s._position == 0


# ── Multi-day discipline ────────────────────────────────────────────


def test_reset_day_state_preserves_entered():
    """Critical: trend_daily is multi-day. reset_day_state must NOT
    flip _position to 0 — that would close + reopen positions every
    morning. Only the per-day skip-log dedup + decision-fired flag
    reset.
    """
    s, _ = _make_strategy()
    s._position = 1
    s._entry_px = 24000
    s._days_held = 5
    s._decision_fired_today = True
    s._last_skip_log_minute["FOO"] = "12:34"

    s.reset_day_state()

    # Position state preserved
    assert s._position == 1
    assert s._entry_px == 24000
    assert s._days_held == 5
    # Per-day flags reset
    assert s._decision_fired_today is False
    assert "FOO" not in s._last_skip_log_minute


def test_decision_fires_only_once_per_day():
    """Once the decision-fired flag is True, no further evaluations
    happen until the next day. This avoids re-checking on every tick
    after decision_time."""
    from src.market_data.simulator import NIFTY_SPOT_TOKEN

    s, ctx = _make_strategy()
    today = date(2026, 5, 6)
    bars = [_make_bar(today - timedelta(days=21 - i), 24000, 24100, 23900, 24000) for i in range(21)]
    _seed_bars(s, bars, vix_means=[15.0] * 21)
    s._today_date = today
    s._today_open = 24200
    s._today_high = 24200
    s._today_low = 23980
    s._today_close = 24200
    s._today_vix_sum = 30.0
    s._today_vix_count = 2
    s._decision_fired_today = True  # already fired today

    # Tick after decision time — should NOT trigger _evaluate
    ctx.clock.now.return_value = datetime(2026, 5, 6, 15, 28, tzinfo=timezone.utc)
    tick = _make_tick(NIFTY_SPOT_TOKEN, 24205, ctx.clock.now.return_value)

    import asyncio
    sig = asyncio.run(s.on_tick(tick))

    assert sig is None
    assert s._position == 0  # didn't enter despite breakout
