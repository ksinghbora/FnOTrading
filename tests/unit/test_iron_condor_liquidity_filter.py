"""Tests for IC's Apr 29 2026 fix: realistic-fill PnL + liquidity filter.

The Apr 28 chain-gap diagnostic
(``reports/diagnose_ic_chain_gap/comparison.md``) revealed two issues:

1. ``IronCondorStrategy._create_exit_signal`` was computing
   ``outcome_pnl`` from LTP, not bid/ask. On gdfl_v2's wider chain this
   under-reported broker-realised loss by ~₹321K over a 227-day window.
2. The strategy entered positions on illiquid deep-OTM strikes whose
   bid-ask spreads alone would erase the IC's edge.

Both fixes live on ``IronCondorStrategy``:
  - ``_bid_ask_for(token)`` — return (bid, ask), fall back to (ltp, ltp)
  - ``_entry_fill_credit() / _exit_fill_debit()`` — model spread
    crossing on every leg
  - ``_check_strike_liquidity(opt, leg_label)`` — block entries when
    spread/mid > ``params.max_spread_pct``

These tests pin the helpers' contract so future refactors can't quietly
revert to the LTP fiction.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest


def _strategy(**overrides):
    """Construct an IronCondorStrategy with a mocked context."""
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.registry import create_strategy

    s = create_strategy("iron_condor", strategy_id="ic_test", params=overrides)
    ctx = MagicMock()
    # BaseStrategy stores the context in _context (read via the .ctx
    # property). set_context() validates so we bypass it for unit tests.
    s._context = ctx
    return s, ctx


def _opt(bid: float, ask: float, *, ltp: float | None = None) -> MagicMock:
    """A stand-in for OptionData with bid/ask/ltp fields."""
    o = MagicMock()
    o.bid_price = Decimal(str(bid))
    o.ask_price = Decimal(str(ask))
    o.ltp = Decimal(str(ltp if ltp is not None else (bid + ask) / 2))
    o.tradingsymbol = f"NIFTY_test_{bid:.0f}_{ask:.0f}"
    return o


# ── _bid_ask_for ──────────────────────────────────────────────────────


def test_bid_ask_for_returns_bid_ask_when_valid():
    s, ctx = _strategy()
    tick = MagicMock(bid_price=Decimal("10.0"), ask_price=Decimal("10.5"))
    ctx.get_tick.return_value = tick
    bid, ask = s._bid_ask_for(123)
    assert bid == 10.0
    assert ask == 10.5


def test_bid_ask_for_falls_back_to_ltp_when_tick_missing():
    s, ctx = _strategy()
    ctx.get_tick.return_value = None
    ctx.get_ltp.return_value = Decimal("9.7")
    bid, ask = s._bid_ask_for(123)
    assert bid == 9.7
    assert ask == 9.7


def test_bid_ask_for_falls_back_to_ltp_when_quote_invalid():
    """ask < bid, or zero, → fall back to LTP-symmetric to avoid
    poisoning the pricing path with a crossed market."""
    s, ctx = _strategy()
    ctx.get_tick.return_value = MagicMock(
        bid_price=Decimal("10.0"), ask_price=Decimal("9.5"),  # crossed
    )
    ctx.get_ltp.return_value = Decimal("9.8")
    bid, ask = s._bid_ask_for(123)
    assert (bid, ask) == (9.8, 9.8)


# ── _entry_fill_credit / _exit_fill_debit ────────────────────────────


def test_entry_fill_credit_models_spread_crossing():
    """Sell shorts at bid, buy longs at ask → entry credit is strictly
    less than (or equal to) the LTP-based mid value."""
    s, ctx = _strategy()
    s._short_ce_token, s._short_pe_token = 1, 2
    s._long_ce_token, s._long_pe_token = 3, 4

    # Realistic quotes: shorts have decent liquidity, longs (wings) are wider.
    ctx.get_tick.side_effect = lambda tok: {
        1: MagicMock(bid_price=Decimal("30.0"), ask_price=Decimal("31.0")),  # short_ce
        2: MagicMock(bid_price=Decimal("28.0"), ask_price=Decimal("29.0")),  # short_pe
        3: MagicMock(bid_price=Decimal("10.0"), ask_price=Decimal("12.0")),  # long_ce wing
        4: MagicMock(bid_price=Decimal("9.0"),  ask_price=Decimal("11.0")),  # long_pe wing
    }[tok]

    credit = s._entry_fill_credit()
    # Sell shorts at bid, buy longs at ask:
    # (30 + 28) − (12 + 11) = 35.0
    assert credit == 35.0


def test_exit_fill_debit_is_strictly_worse_than_entry_credit():
    """If quotes haven't moved, exiting immediately costs the full
    bid-ask spread × 4 legs. Entry credit and exit debit should
    bracket the LTP-mid value, not equal it."""
    s, ctx = _strategy()
    s._short_ce_token, s._short_pe_token = 1, 2
    s._long_ce_token, s._long_pe_token = 3, 4

    ctx.get_tick.side_effect = lambda tok: {
        1: MagicMock(bid_price=Decimal("30.0"), ask_price=Decimal("31.0")),
        2: MagicMock(bid_price=Decimal("28.0"), ask_price=Decimal("29.0")),
        3: MagicMock(bid_price=Decimal("10.0"), ask_price=Decimal("12.0")),
        4: MagicMock(bid_price=Decimal("9.0"),  ask_price=Decimal("11.0")),
    }[tok]

    credit = s._entry_fill_credit()  # 35.0
    debit = s._exit_fill_debit()
    # Exit: buy shorts at ask, sell longs at bid: (31 + 29) − (10 + 9) = 41.0
    assert debit == 41.0
    # Round-trip cost = debit − credit = 6.0 = sum of 4 leg spreads
    assert debit - credit == 6.0


# ── _check_strike_liquidity ───────────────────────────────────────────


def test_liquidity_filter_passes_tight_spread():
    s, _ = _strategy(max_spread_pct=5.0)
    # 50/50.5 → spread 0.5 / mid 50.25 = 0.99% → passes
    assert s._check_strike_liquidity(_opt(50.0, 50.5), "short_ce") is None


def test_liquidity_filter_blocks_wide_spread():
    s, _ = _strategy(max_spread_pct=5.0)
    # 1.0 / 1.4 → spread 0.4 / mid 1.2 = 33.3% → blocked
    reason = s._check_strike_liquidity(_opt(1.0, 1.4), "long_ce")
    assert reason is not None
    assert "long_ce" in reason
    assert "33.3%" in reason or "33." in reason


def test_liquidity_filter_blocks_invalid_quote():
    s, _ = _strategy(max_spread_pct=5.0)
    # ask < bid (crossed) — invalid
    reason = s._check_strike_liquidity(_opt(10.0, 9.5), "short_pe")
    assert reason is not None
    assert "invalid" in reason


def test_liquidity_filter_blocks_zero_quote():
    s, _ = _strategy(max_spread_pct=5.0)
    reason = s._check_strike_liquidity(_opt(0.0, 0.0), "long_pe")
    assert reason is not None


def test_liquidity_filter_disabled_when_threshold_zero():
    """``max_spread_pct=0`` disables the filter — useful for bisecting
    against pre-fix behaviour or running on data with known wide quotes."""
    s, _ = _strategy(max_spread_pct=0.0)
    # Even an absurdly wide quote should pass:
    assert s._check_strike_liquidity(_opt(1.0, 100.0), "short_ce") is None


def test_liquidity_filter_blocks_when_opt_missing():
    s, _ = _strategy(max_spread_pct=5.0)
    reason = s._check_strike_liquidity(None, "long_ce")
    assert reason is not None
    assert "missing" in reason


def test_max_spread_pct_default_is_5pct():
    """The Apr 29 fix calibrated max_spread_pct=5%. If a future change
    moves the default, this test surfaces the drift."""
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.registry import create_strategy
    s = create_strategy("iron_condor", strategy_id="ic_default")
    assert s.params.max_spread_pct == 5.0


def test_iron_butterfly_inherits_liquidity_filter():
    """IronButterflyParams subclasses IronCondorParams, so it gets the
    same liquidity filter for free. Verifying explicitly because IB
    ships ATM strikes which have tighter spreads but should still be
    gated."""
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.params import IronButterflyParams
    p = IronButterflyParams()
    assert p.max_spread_pct == 5.0
