"""Tests for the May 1 2026 TrendITM pivot strategy.

Pins the contract for the post-premium-selling pivot:
- 1-min OHLC ring buffer behaviour (warm-up, bar rollover, ATR seeding)
- Donchian breakout entry condition (long / short / no-breakout)
- VIX + ATR + time-window entry gates
- Deep-ITM strike selection (CE for long, PE for short, skip if no quote)
- Trailing-stop arithmetic (long + short)
- Premium PT / SL / time exit precedence

Why this matters: the entire pivot decision (commit c17ad01) depends on
this strategy validating cleanly on the post-SEBI corpus. Tests here pin
the mechanics so a "Sharpe<0" verdict means the SIGNAL has no edge,
not that the implementation has a bug.
"""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from unittest.mock import MagicMock

import pytest


# ── Helpers ───────────────────────────────────────────────────────────


def _strategy(**param_overrides):
    """Build a TrendITMStrategy with a mocked context."""
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.registry import create_strategy

    s = create_strategy("trend_itm", strategy_id="trend_test", params=param_overrides)
    s._context = MagicMock()
    return s, s._context


def _seed_bars(s, bars: list[dict]):
    """Inject a list of completed bars into the ring buffer for testing."""
    for b in bars:
        s._bars.append(b)


def _bar(minute: str, o: float, h: float, low: float, c: float) -> dict:
    return {"minute": minute, "open": o, "high": h, "low": low, "close": c}


def _opt(bid: float, ask: float, *, ltp: float | None = None,
         token: int = 1, symbol: str = "NIFTY25APR22000CE") -> MagicMock:
    o = MagicMock()
    o.bid_price = Decimal(str(bid))
    o.ask_price = Decimal(str(ask))
    o.ltp = Decimal(str(ltp if ltp is not None else (bid + ask) / 2))
    o.tradingsymbol = symbol
    o.instrument_token = token
    return o


def _chain(strikes_with_legs):
    """Build a mock OptionChain. ``strikes_with_legs`` = list of
    (strike, ce_opt, pe_opt) tuples."""
    chain = MagicMock()
    chain.strikes = []
    for strike, ce, pe in strikes_with_legs:
        entry = MagicMock()
        entry.strike = Decimal(str(strike))
        entry.ce = ce
        entry.pe = pe
        chain.strikes.append(entry)
    return chain


# ── Init / registration ──────────────────────────────────────────────


def test_strategy_registers_under_trend_itm():
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.registry import _REGISTRY
    assert "trend_itm" in _REGISTRY


def test_default_params_match_design_doc():
    s, _ = _strategy()
    p = s.params
    assert p.donchian_lookback == 20
    assert p.atr_period == 14
    assert p.atr_stop_mult == 2.0
    assert p.itm_offset_pts == 500
    assert p.vix_entry_min == 12.0
    assert p.vix_entry_max == 22.0
    assert p.entry_time == time(9, 30)
    assert p.exit_time == time(14, 45)


def test_buffer_size_accommodates_donchian_and_atr():
    s, _ = _strategy()
    # maxlen = max(lookback+1, period+1) = max(21, 15) = 21
    assert s._bars.maxlen == 21


# ── Bar buffer + ATR ─────────────────────────────────────────────────


def test_bar_rolls_over_on_minute_change():
    """Two ticks in different minutes → first bar finalised + pushed,
    second bar started."""
    s, ctx = _strategy()
    ctx.get_spot_price.return_value = Decimal("22500")

    t1 = datetime(2025, 5, 1, 10, 0, 12)
    s._update_bars(t1)
    assert s._current_bar is not None
    assert s._current_bar["minute"] == "2025-05-01T10:00:00"
    assert len(s._bars) == 0  # First bar still open

    t2 = datetime(2025, 5, 1, 10, 1, 5)
    s._update_bars(t2)
    assert len(s._bars) == 1  # First bar finalised
    assert s._current_bar["minute"] == "2025-05-01T10:01:00"


def test_bar_updates_high_low_close_within_minute():
    s, ctx = _strategy()
    spot_seq = [Decimal("22500"), Decimal("22510"), Decimal("22480"), Decimal("22495")]
    ctx.get_spot_price.side_effect = spot_seq

    base = datetime(2025, 5, 1, 10, 0)
    for i, _ in enumerate(spot_seq):
        s._update_bars(base.replace(second=i))

    bar = s._current_bar
    assert bar["open"] == 22500.0
    assert bar["high"] == 22510.0
    assert bar["low"] == 22480.0
    assert bar["close"] == 22495.0


def test_atr_seeds_after_period_plus_one_bars():
    """ATR(Wilder) needs N TRs (so N+1 bars). Before that it stays 0."""
    s, _ = _strategy(atr_period=3)
    # Inject 3 bars — only 2 TRs available, ATR not yet seeded
    bars = [
        _bar("00:01", 100, 105, 99, 102),
        _bar("00:02", 102, 108, 101, 106),
        _bar("00:03", 106, 110, 103, 109),
    ]
    _seed_bars(s, bars)
    s._update_atr_after_bar()
    assert s._atr == 0.0  # Not seeded yet (need period+1 = 4 bars)

    # Add a 4th bar — now we have 3 TRs over the 3-period window
    s._bars.append(_bar("00:04", 109, 112, 107, 111))
    s._update_atr_after_bar()
    assert s._atr > 0
    # TRs: bar2 vs bar1 = max(7, 3, 0) = 7 ... etc; check it's positive
    # and within plausible range (max true range was ~7-8 across bars)
    assert 1.0 < s._atr < 20.0


def test_atr_recurrence_after_seeding():
    """After seed, Wilder smoothing: ATR_new = ((N-1)*ATR_old + TR) / N"""
    s, _ = _strategy(atr_period=3)
    s._atr = 5.0  # Pre-seed
    # Push two bars so we have prev_close to compute TR
    s._bars.append(_bar("00:01", 100, 105, 99, 102))
    s._bars.append(_bar("00:02", 102, 108, 101, 106))
    s._update_atr_after_bar()
    # TR = max(108-101, |108-102|, |101-102|) = max(7, 6, 1) = 7
    # New ATR = (2*5 + 7) / 3 = 5.667
    assert abs(s._atr - 5.667) < 0.01


# ── Donchian breakout entry ──────────────────────────────────────────


def _populate_buffer_for_entry(s, latest_close: float, ch_high: float, ch_low: float):
    """Fill the buffer with enough bars to make Donchian computable.
    Last bar has close=`latest_close`. Prior `lookback` bars have
    high<=ch_high, low>=ch_low.
    """
    needed = s._bars.maxlen
    for i in range(needed - 1):
        s._bars.append(_bar(
            f"00:{i:02d}",
            (ch_high + ch_low) / 2,
            ch_high,    # all prior bars cap at ch_high
            ch_low,     # and floor at ch_low
            (ch_high + ch_low) / 2,
        ))
    # Final bar — its close is what we compare to ch_high/ch_low
    s._bars.append(_bar(
        "00:99",
        latest_close,
        max(latest_close, ch_high),
        min(latest_close, ch_low),
        latest_close,
    ))
    s._atr = max(10.0, abs(latest_close - ch_high) * 2)  # Plenty of room


@pytest.mark.asyncio
async def test_no_entry_when_buffer_warming_up():
    s, ctx = _strategy()
    ctx.clock.now.return_value = datetime(2025, 5, 1, 10, 0)
    ctx.get_spot_price.return_value = Decimal("22500")
    sig = await s._try_entry(ctx.clock.now())
    assert sig is None


@pytest.mark.asyncio
async def test_no_entry_when_no_breakout():
    s, ctx = _strategy(breakout_confirmation_pts=5.0)
    _populate_buffer_for_entry(s, latest_close=22550, ch_high=22600, ch_low=22500)
    ctx.clock.now.return_value = datetime(2025, 5, 1, 10, 0)
    ctx.get_spot_price.return_value = Decimal("22550")
    ctx.get_vix.return_value = 15.0
    # latest_close 22550 is INSIDE [22500, 22600] — no breakout
    sig = await s._try_entry(ctx.clock.now())
    assert sig is None


@pytest.mark.asyncio
async def test_long_entry_fires_on_upside_breakout():
    s, ctx = _strategy(
        breakout_confirmation_pts=5.0, itm_offset_pts=200, itm_max_strike_search_pts=100,
        # Lower ATR floor — the helper sets _atr to abs(latest-ch_high)*2
        # which on this 10-pt overshoot would be 20, well below 0.4% of
        # spot 22610 (~90). The signal is the breakout, not the ATR
        # tested here, so loosen the floor.
        atr_floor_pct_of_spot=0.05,
    )
    _populate_buffer_for_entry(s, latest_close=22610, ch_high=22600, ch_low=22500)
    # latest_close 22610 > ch_high 22600 + 5 = 22605 → LONG breakout
    ctx.clock.now.return_value = datetime(2025, 5, 1, 10, 0)
    ctx.get_spot_price.return_value = Decimal("22610")
    ctx.get_vix.return_value = 15.0
    s._expiry = date(2025, 5, 8)

    # Chain has a CE at strike spot - 200 = 22410 (the deep-ITM target)
    ce_22410 = _opt(bid=210, ask=212, token=42, symbol="NIFTY25MAY22410CE")
    pe_22410 = _opt(bid=2, ask=3, token=43, symbol="NIFTY25MAY22410PE")
    ctx.get_option_chain.return_value = _chain([(22410, ce_22410, pe_22410)])
    # Tick lookup for the entry-fill snapshot
    ctx.get_tick.return_value = MagicMock(
        bid_price=Decimal("210"), ask_price=Decimal("212")
    )

    sig = await s._try_entry(ctx.clock.now())
    assert sig is not None
    assert s._entered is True
    assert s._side == "long"
    assert s._entry_option_type == "CE"
    assert s._entry_strike == 22410
    assert s._entry_token == 42
    # Entry premium snapshots ASK (we BUY)
    assert s._entry_premium == Decimal("212.00")


@pytest.mark.asyncio
async def test_short_entry_fires_on_downside_breakout():
    s, ctx = _strategy(
        breakout_confirmation_pts=5.0, itm_offset_pts=200, itm_max_strike_search_pts=100,
    )
    _populate_buffer_for_entry(s, latest_close=22390, ch_high=22500, ch_low=22400)
    # latest_close 22390 < ch_low 22400 - 5 = 22395 → SHORT breakout
    ctx.clock.now.return_value = datetime(2025, 5, 1, 10, 0)
    ctx.get_spot_price.return_value = Decimal("22390")
    ctx.get_vix.return_value = 15.0
    s._expiry = date(2025, 5, 8)

    # Chain has a PE at strike spot + 200 = 22590 (the deep-ITM target)
    ce_22590 = _opt(bid=2, ask=3, token=99, symbol="NIFTY25MAY22590CE")
    pe_22590 = _opt(bid=205, ask=208, token=100, symbol="NIFTY25MAY22590PE")
    ctx.get_option_chain.return_value = _chain([(22590, ce_22590, pe_22590)])
    ctx.get_tick.return_value = MagicMock(
        bid_price=Decimal("205"), ask_price=Decimal("208")
    )

    sig = await s._try_entry(ctx.clock.now())
    assert sig is not None
    assert s._side == "short"
    assert s._entry_option_type == "PE"
    assert s._entry_strike == 22590


@pytest.mark.asyncio
async def test_entry_blocked_when_vix_below_min():
    s, ctx = _strategy(vix_entry_min=12.0)
    _populate_buffer_for_entry(s, latest_close=22610, ch_high=22600, ch_low=22500)
    ctx.clock.now.return_value = datetime(2025, 5, 1, 10, 0)
    ctx.get_spot_price.return_value = Decimal("22610")
    ctx.get_vix.return_value = 10.0  # Below min
    s._expiry = date(2025, 5, 8)
    sig = await s._try_entry(ctx.clock.now())
    assert sig is None
    assert s._entered is False


@pytest.mark.asyncio
async def test_entry_blocked_when_atr_below_floor():
    s, ctx = _strategy(atr_floor_pct_of_spot=1.0)  # 1% floor at spot 22500 = 225
    _populate_buffer_for_entry(s, latest_close=22610, ch_high=22600, ch_low=22500)
    s._atr = 50.0  # Below the 225 floor
    ctx.clock.now.return_value = datetime(2025, 5, 1, 10, 0)
    ctx.get_spot_price.return_value = Decimal("22610")
    ctx.get_vix.return_value = 15.0
    s._expiry = date(2025, 5, 8)
    sig = await s._try_entry(ctx.clock.now())
    assert sig is None


@pytest.mark.asyncio
async def test_entry_blocked_when_no_itm_strike_in_chain():
    s, ctx = _strategy(itm_offset_pts=200, itm_max_strike_search_pts=10)
    _populate_buffer_for_entry(s, latest_close=22610, ch_high=22600, ch_low=22500)
    ctx.clock.now.return_value = datetime(2025, 5, 1, 10, 0)
    ctx.get_spot_price.return_value = Decimal("22610")
    ctx.get_vix.return_value = 15.0
    s._expiry = date(2025, 5, 8)
    # Chain only has a strike 50 pts away from the 22410 ideal — outside 10pt tolerance
    ce_22500 = _opt(bid=120, ask=122, token=1)
    ctx.get_option_chain.return_value = _chain([(22500, ce_22500, None)])
    sig = await s._try_entry(ctx.clock.now())
    assert sig is None


@pytest.mark.asyncio
async def test_entry_blocked_when_chain_strike_has_no_quote():
    """A strike with bid=0 or ask=0 is rejected — we don't enter into
    the LTP-fallback fiction the audit warned against."""
    s, ctx = _strategy(itm_offset_pts=200, itm_max_strike_search_pts=100)
    _populate_buffer_for_entry(s, latest_close=22610, ch_high=22600, ch_low=22500)
    ctx.clock.now.return_value = datetime(2025, 5, 1, 10, 0)
    ctx.get_spot_price.return_value = Decimal("22610")
    ctx.get_vix.return_value = 15.0
    s._expiry = date(2025, 5, 8)
    ce_22410_no_quote = _opt(bid=0, ask=212)  # bid missing
    ctx.get_option_chain.return_value = _chain([(22410, ce_22410_no_quote, None)])
    sig = await s._try_entry(ctx.clock.now())
    assert sig is None


# ── Trailing stop ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_long_trail_stop_does_not_fire_within_atr():
    s, ctx = _strategy(atr_stop_mult=2.0)
    s._entered = True
    s._side = "long"
    s._entry_token = 42
    s._entry_symbol = "NIFTY_test"
    s._entry_premium = Decimal("100")
    s._atr = 20.0
    s._entry_spot = 22600
    s._peak_favorable_spot = 22640  # peak so far
    ctx.get_spot_price.return_value = Decimal("22610")  # 30 below peak, < 2*20=40
    ctx.get_tick.return_value = MagicMock(
        bid_price=Decimal("110"), ask_price=Decimal("112")
    )
    sig = s._check_exit_conditions(datetime(2025, 5, 1, 12, 0))
    assert sig is None


@pytest.mark.asyncio
async def test_long_trail_stop_fires_below_two_atr():
    s, ctx = _strategy(atr_stop_mult=2.0)
    s._entered = True
    s._side = "long"
    s._entry_token = 42
    s._entry_symbol = "NIFTY_test"
    s._entry_premium = Decimal("100")
    s._atr = 20.0
    s._entry_spot = 22600
    s._peak_favorable_spot = 22640
    ctx.get_spot_price.return_value = Decimal("22595")  # 45 below peak, > 2*20=40
    ctx.get_tick.return_value = MagicMock(
        bid_price=Decimal("90"), ask_price=Decimal("92")
    )
    sig = s._check_exit_conditions(datetime(2025, 5, 1, 12, 0))
    assert sig is not None
    assert "ATR trail (long)" in (sig.reason or "")


@pytest.mark.asyncio
async def test_short_trail_stop_fires_above_two_atr_from_trough():
    s, ctx = _strategy(atr_stop_mult=2.0)
    s._entered = True
    s._side = "short"
    s._entry_token = 42
    s._entry_symbol = "NIFTY_test"
    s._entry_premium = Decimal("100")
    s._atr = 20.0
    s._entry_spot = 22500
    s._peak_favorable_spot = 22460  # trough so far (best for short)
    ctx.get_spot_price.return_value = Decimal("22510")  # 50 above trough, > 2*20=40
    ctx.get_tick.return_value = MagicMock(
        bid_price=Decimal("90"), ask_price=Decimal("92")
    )
    sig = s._check_exit_conditions(datetime(2025, 5, 1, 12, 0))
    assert sig is not None
    assert "ATR trail (short)" in (sig.reason or "")


# ── Premium PT/SL ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_premium_profit_target_fires():
    s, ctx = _strategy(profit_target_pct=80.0, stop_loss_pct=999.0)
    s._entered = True
    s._side = "long"
    s._entry_token = 42
    s._entry_symbol = "NIFTY_test"
    s._entry_premium = Decimal("100")
    s._atr = 20.0
    s._peak_favorable_spot = 22500
    ctx.get_spot_price.return_value = Decimal("22500")
    # bid 200 → close-cost-on-sell 200 vs entry 100 = +100% > 80% → PT fires
    ctx.get_tick.return_value = MagicMock(
        bid_price=Decimal("200"), ask_price=Decimal("202")
    )
    sig = s._check_exit_conditions(datetime(2025, 5, 1, 12, 0))
    assert sig is not None
    assert "Profit target" in (sig.reason or "")


@pytest.mark.asyncio
async def test_premium_stop_loss_fires():
    s, ctx = _strategy(profit_target_pct=999.0, stop_loss_pct=40.0)
    s._entered = True
    s._side = "long"
    s._entry_token = 42
    s._entry_symbol = "NIFTY_test"
    s._entry_premium = Decimal("100")
    s._atr = 20.0
    s._peak_favorable_spot = 22500
    ctx.get_spot_price.return_value = Decimal("22500")
    # bid 55 → -45% < -40% SL → fires
    ctx.get_tick.return_value = MagicMock(
        bid_price=Decimal("55"), ask_price=Decimal("57")
    )
    sig = s._check_exit_conditions(datetime(2025, 5, 1, 12, 0))
    assert sig is not None
    assert "Stop loss" in (sig.reason or "")


# ── Time stop ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_time_stop_takes_precedence_over_pt_sl():
    """If time stop has fired, exit on time-stop reason regardless of
    whether the position is in profit or loss. Time stop is unconditional."""
    s, ctx = _strategy(exit_time=time(14, 45))
    s._entered = True
    s._side = "long"
    s._entry_token = 42
    s._entry_symbol = "NIFTY_test"
    s._entry_premium = Decimal("100")
    s._atr = 20.0
    s._peak_favorable_spot = 22500
    ctx.get_spot_price.return_value = Decimal("22500")
    # In profit, but time has elapsed
    ctx.get_tick.return_value = MagicMock(
        bid_price=Decimal("200"), ask_price=Decimal("202")
    )
    sig = s._check_exit_conditions(datetime(2025, 5, 1, 14, 45))
    assert sig is not None
    assert "Exit time reached" in (sig.reason or "")


# ── State reset on exit ──────────────────────────────────────────────


def test_exit_resets_position_state_and_marks_stopped():
    s, _ = _strategy()
    s._entered = True
    s._side = "long"
    s._entry_token = 42
    s._entry_symbol = "NIFTY_test"
    s._entry_premium = Decimal("100")
    s._entry_strike = 22000.0
    s._entry_option_type = "CE"
    # _exit needs to call _build_option_leg → _bid_ask_for. Stub get_tick.
    s.ctx.get_tick.return_value = MagicMock(
        bid_price=Decimal("90"), ask_price=Decimal("92")
    )

    sig = s._exit("Test exit")
    assert sig.signal_type.value == "EXIT"
    assert s._entered is False
    assert s._side == ""
    assert s._entry_token == 0
    assert s._entry_premium == Decimal("0")
    assert s._stopped_for_day is True


# ── ITM strike selection ─────────────────────────────────────────────


def test_select_itm_long_picks_ce_below_spot_at_offset():
    s, _ = _strategy(itm_offset_pts=500, itm_max_strike_search_pts=100)
    spot = 22500.0
    # Strikes available; ideal CE for long = spot - 500 = 22000
    ce_21950 = _opt(bid=560, ask=565, token=1)
    ce_22000 = _opt(bid=510, ask=515, token=2)
    ce_22050 = _opt(bid=460, ask=465, token=3)
    chain = _chain([
        (21950, ce_21950, _opt(bid=2, ask=3)),
        (22000, ce_22000, _opt(bid=4, ask=5)),
        (22050, ce_22050, _opt(bid=6, ask=7)),
    ])
    opt, strike, opt_type = s._select_itm_option(chain, spot, "long")
    assert opt_type == "CE"
    assert strike == 22000.0  # Closest to ideal 22000
    assert opt.instrument_token == 2


def test_select_itm_short_picks_pe_above_spot_at_offset():
    s, _ = _strategy(itm_offset_pts=500, itm_max_strike_search_pts=100)
    spot = 22500.0
    # Ideal PE for short = spot + 500 = 23000
    pe_22950 = _opt(bid=460, ask=465, token=10)
    pe_23000 = _opt(bid=510, ask=515, token=11)
    pe_23050 = _opt(bid=560, ask=565, token=12)
    chain = _chain([
        (22950, _opt(bid=4, ask=5), pe_22950),
        (23000, _opt(bid=6, ask=7), pe_23000),
        (23050, _opt(bid=8, ask=9), pe_23050),
    ])
    opt, strike, opt_type = s._select_itm_option(chain, spot, "short")
    assert opt_type == "PE"
    assert strike == 23000.0
    assert opt.instrument_token == 11
