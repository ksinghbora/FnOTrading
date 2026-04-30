"""Tests for the Apr 30 2026 quote-fallback probe.

Pins the ``_bid_ask_total`` / ``_bid_ask_fallback`` counters on
BaseStrategy and the ``get_quote_fallback_stats`` getter.

Why this probe exists
=====================

The Apr 29 audit fix moved every strategy off LTP-midpoint pricing onto
``_bid_ask_for`` (real bid/ask). But ``_bid_ask_for`` itself falls back
to (ltp, ltp) when the tick is missing, has a zero side, or is crossed.
On a sparse / illiquid chain that fallback can fire often enough that
the run's "realistic-fill" claim is, in practice, the same LTP fiction
the audit was supposed to remove.

The probe makes that visible: validation runs now log a ``quote_quality:
N/M (P%) fell back`` line so an operator can see the realistic-fill
fraction at a glance, with a WARNING flag when fallback >= 25%.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from unittest.mock import MagicMock


def _strategy():
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.registry import create_strategy

    s = create_strategy("iron_condor", strategy_id="ic_probe")
    s._context = MagicMock()
    return s, s._context


# ── Counter increments on every call ──────────────────────────────────


def test_counters_initialise_to_zero():
    s, _ = _strategy()
    stats = s.get_quote_fallback_stats()
    assert stats == {"total": 0, "fallback": 0, "fallback_pct": 0.0}


def test_total_increments_on_every_call():
    s, ctx = _strategy()
    ctx.get_tick.return_value = MagicMock(
        bid_price=Decimal("10.0"), ask_price=Decimal("10.5")
    )
    for _ in range(7):
        s._bid_ask_for(123)
    stats = s.get_quote_fallback_stats()
    assert stats["total"] == 7
    assert stats["fallback"] == 0
    assert stats["fallback_pct"] == 0.0


def test_fallback_increments_when_tick_missing():
    s, ctx = _strategy()
    ctx.get_tick.return_value = None
    ctx.get_ltp.return_value = Decimal("9.5")
    s._bid_ask_for(1)
    s._bid_ask_for(2)
    stats = s.get_quote_fallback_stats()
    assert stats == {"total": 2, "fallback": 2, "fallback_pct": 100.0}


def test_fallback_increments_when_quote_crossed():
    """ask < bid means the quote is crossed — invalid. The helper falls
    back to LTP-symmetric and the probe must register that as a
    fallback, otherwise high-fallback CPCV runs would look clean."""
    s, ctx = _strategy()
    ctx.get_tick.return_value = MagicMock(
        bid_price=Decimal("10.0"), ask_price=Decimal("9.5"),  # crossed
    )
    ctx.get_ltp.return_value = Decimal("9.7")
    s._bid_ask_for(1)
    stats = s.get_quote_fallback_stats()
    assert stats["total"] == 1
    assert stats["fallback"] == 1


def test_fallback_increments_when_either_side_zero():
    """One-sided quote (bid=0 or ask=0) is also a fallback path. The
    GDFL gdfl_v2 chain has many strikes with one-sided quotes deep OTM
    — the probe needs to flag those, not silently treat them as real."""
    s, ctx = _strategy()
    ctx.get_tick.return_value = MagicMock(
        bid_price=Decimal("0"), ask_price=Decimal("5.0")  # zero bid
    )
    ctx.get_ltp.return_value = Decimal("5.0")
    s._bid_ask_for(1)
    stats = s.get_quote_fallback_stats()
    assert stats["fallback"] == 1


def test_mixed_real_and_fallback_calls_compute_correct_pct():
    s, ctx = _strategy()
    # Alternating: real, fallback, real, real → 1/4 = 25% fallback
    ticks = [
        MagicMock(bid_price=Decimal("10"), ask_price=Decimal("11")),  # real
        None,                                                          # fallback
        MagicMock(bid_price=Decimal("20"), ask_price=Decimal("21")),  # real
        MagicMock(bid_price=Decimal("30"), ask_price=Decimal("31")),  # real
    ]
    ctx.get_tick.side_effect = ticks
    ctx.get_ltp.return_value = Decimal("15")
    for _ in range(4):
        s._bid_ask_for(1)
    stats = s.get_quote_fallback_stats()
    assert stats["total"] == 4
    assert stats["fallback"] == 1
    assert stats["fallback_pct"] == 25.0


def test_pct_is_rounded_to_two_decimals():
    s, ctx = _strategy()
    # 1/3 fallback → 33.333...% → expect 33.33
    ctx.get_tick.side_effect = [
        None,
        MagicMock(bid_price=Decimal("10"), ask_price=Decimal("11")),
        MagicMock(bid_price=Decimal("12"), ask_price=Decimal("13")),
    ]
    ctx.get_ltp.return_value = Decimal("10")
    for _ in range(3):
        s._bid_ask_for(1)
    stats = s.get_quote_fallback_stats()
    assert stats["fallback_pct"] == 33.33


def test_zero_calls_never_divides():
    """If no calls happen (e.g., entry never fired), the pct must be
    0.0, not raise ZeroDivisionError."""
    s, _ = _strategy()
    stats = s.get_quote_fallback_stats()
    assert stats["fallback_pct"] == 0.0


# ── DEBUG log emission ────────────────────────────────────────────────


def test_debug_log_emitted_on_fallback(caplog):
    """A DEBUG log lets operators trace which legs/timestamps degraded.
    Pin it so a refactor can't silently drop the trace."""
    s, ctx = _strategy()
    ctx.get_tick.return_value = None
    ctx.get_ltp.return_value = Decimal("5.0")
    with caplog.at_level(logging.DEBUG, logger="src.strategy.base"):
        s._bid_ask_for(42)
    assert any("BID_ASK_FALLBACK" in r.message for r in caplog.records)
    assert any("token=42" in r.message for r in caplog.records)


def test_debug_log_not_emitted_on_real_quote(caplog):
    s, ctx = _strategy()
    ctx.get_tick.return_value = MagicMock(
        bid_price=Decimal("10.0"), ask_price=Decimal("11.0")
    )
    with caplog.at_level(logging.DEBUG, logger="src.strategy.base"):
        s._bid_ask_for(42)
    assert not any("BID_ASK_FALLBACK" in r.message for r in caplog.records)


# ── Engine-level wiring ───────────────────────────────────────────────


def test_engine_result_contains_quote_fallback_stats_key():
    """``BacktestEngine.run()`` builds the result dict with the stats
    key so the validation harness has somewhere to read it from. Pin
    the key's presence so a refactor that drops it is caught at unit-
    test time, not at the next 4-hour CPCV run.

    We don't run the engine here (heavy); we just inspect the source
    to confirm the key is in the result-dict literal."""
    import inspect
    from src.backtest import engine
    src = inspect.getsource(engine)
    assert "quote_fallback_stats" in src, (
        "engine result dict should expose quote_fallback_stats so "
        "validate_strategy.py can surface the probe in its run summary"
    )
    assert "get_quote_fallback_stats" in src, (
        "engine should call strategy.get_quote_fallback_stats() to "
        "harvest the per-run counters"
    )
