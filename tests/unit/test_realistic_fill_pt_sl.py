"""Tests for the Apr 30 2026 PT/SL realistic-fill threshold fix.

All three reviewers (opus, sonnet, haiku) of the multi-model audit
independently flagged that the strategies' profit-target / stop-loss /
trail-stop thresholds were comparing against an LTP-midpoint sum of the
legs, while the broker actually crosses the bid/ask spread on close.

The fallout: PT fires too early (LTP-mid is optimistic for shorts
relative to the BUY-at-ask close cost), SL fires too late, and the
adjustment trigger evaluates on a different valuation than the rolling
adjustment will actually pay. The harness's per-trade PnL was already
fixed in the Apr 29 audit (``_exit_fill_debit`` / ``_exit_fill_credit``
on each strategy); the threshold check was the missing leg.

These tests pin the contract: PT/SL/trail/adjustment branches all read
from the realistic-fill helper, NOT from a sum of ``ctx.get_ltp`` calls.

For each of the four active strategies, we set up bid/ask quotes so the
LTP-midpoint sum and the realistic-fill close cost diverge enough to
flip the decision, then verify the strategy follows the bid/ask number.
"""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from unittest.mock import MagicMock


def _mock_clock(now: datetime) -> MagicMock:
    clock = MagicMock()
    clock.now.return_value = now
    return clock


def _tick_with(bid: float, ask: float) -> MagicMock:
    return MagicMock(bid_price=Decimal(str(bid)), ask_price=Decimal(str(ask)))


# ── short_strangle ─────────────────────────────────────────────────────


def _build_strangle(**param_overrides):
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.registry import create_strategy

    # Disable trail + adjustment so the test isolates PT/SL.
    params = {
        "trail_stop_pct": 0.0,
        "adjustment_threshold_pct": 9999,
        "vol_scaled_exits": False,
        **param_overrides,
    }
    s = create_strategy("short_strangle", strategy_id="ss_test", params=params)
    s._context = MagicMock()
    return s, s._context


def test_short_strangle_pt_does_not_fire_when_only_ltp_mid_would():
    """Realistic close cost (sum of asks) exceeds the PT threshold the
    LTP-mid sum would fire. After the fix, PT must NOT fire."""
    s, ctx = _build_strangle(profit_target_pct=40.0, stop_loss_pct=200.0)
    s._entered = True
    s._entry_premium = Decimal("100")
    s._peak_premium = Decimal("100")
    s._ce_token, s._pe_token = 11, 22
    s._expiry = date(2026, 5, 1)
    ctx.clock = _mock_clock(datetime(2026, 4, 28, 11, 30))

    # LTP-mid sum = 25 + 25 = 50 → "decay" = 50% (would fire 40% PT, BUG).
    # But ASK sum = 35 + 35 = 70 → realistic close cost
    #   → decay = (100 − 70)/100 = 30% < 40% PT → NO fire (correct).
    ctx.get_ltp.side_effect = lambda tok: Decimal("25")
    ctx.get_tick.side_effect = lambda tok: _tick_with(15.0, 35.0)

    sig = s._check_adjustments()
    assert sig is None, "PT must not fire when realistic close cost is below the threshold"


def test_short_strangle_sl_fires_on_realistic_fill_when_ltp_mid_would_not():
    """Mirror case: ask-sum is well above LTP-mid sum, so SL fires on
    realistic close cost while LTP-mid would have stayed silent."""
    s, ctx = _build_strangle(profit_target_pct=200.0, stop_loss_pct=80.0)
    s._entered = True
    s._entry_premium = Decimal("100")
    s._peak_premium = Decimal("100")
    s._ce_token, s._pe_token = 11, 22
    s._expiry = date(2026, 5, 1)
    ctx.clock = _mock_clock(datetime(2026, 4, 28, 11, 30))

    # LTP-mid sum = 90 + 90 = 180 → +80% (right at threshold, may not fire)
    # ASK sum = 100 + 100 = 200 → +100% > 80% SL → fires (correct)
    ctx.get_ltp.side_effect = lambda tok: Decimal("90")
    ctx.get_tick.side_effect = lambda tok: _tick_with(80.0, 100.0)

    sig = s._check_adjustments()
    assert sig is not None
    # _create_exit_signal flips signal_type to EXIT and reason mentions stop loss
    assert "Stop loss" in (sig.reason or "")


# ── short_straddle ────────────────────────────────────────────────────


def _build_straddle(**param_overrides):
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.registry import create_strategy

    params = {
        "trail_stop_pct": 0.0,
        "adjustment_threshold_pct": 9999,
        "vol_scaled_exits": False,
        **param_overrides,
    }
    s = create_strategy("short_straddle", strategy_id="sst_test", params=params)
    s._context = MagicMock()
    return s, s._context


def test_short_straddle_pt_does_not_fire_when_only_ltp_mid_would():
    s, ctx = _build_straddle(profit_target_pct=40.0, stop_loss_pct=200.0)
    s._entered = True
    s._entry_premium = Decimal("200")
    s._peak_premium = Decimal("200")
    s._ce_token, s._pe_token = 11, 22
    s._atm_strike = 22500.0
    s._expiry = date(2026, 5, 1)
    ctx.clock = _mock_clock(datetime(2026, 4, 28, 11, 30))

    # LTP-mid sum = 50 + 50 = 100 → decay 50% (would fire 40% PT)
    # ASK sum = 70 + 70 = 140 → decay 30% < 40% → NO fire (correct)
    ctx.get_ltp.side_effect = lambda tok: Decimal("50")
    ctx.get_tick.side_effect = lambda tok: _tick_with(30.0, 70.0)

    sig = s._check_adjustments(MagicMock())
    assert sig is None


def test_short_straddle_sl_fires_on_realistic_fill_when_ltp_mid_would_not():
    s, ctx = _build_straddle(profit_target_pct=200.0, stop_loss_pct=50.0)
    s._entered = True
    s._entry_premium = Decimal("200")
    s._peak_premium = Decimal("200")
    s._ce_token, s._pe_token = 11, 22
    s._atm_strike = 22500.0
    s._expiry = date(2026, 5, 1)
    ctx.clock = _mock_clock(datetime(2026, 4, 28, 11, 30))

    # LTP-mid sum = 140 + 140 = 280 → +40% (under SL=50%)
    # ASK sum = 160 + 160 = 320 → +60% > 50% SL → fires (correct)
    ctx.get_ltp.side_effect = lambda tok: Decimal("140")
    ctx.get_tick.side_effect = lambda tok: _tick_with(120.0, 160.0)

    sig = s._check_adjustments(MagicMock())
    assert sig is not None
    assert "Stop loss" in (sig.reason or "")


# ── iron_condor ───────────────────────────────────────────────────────


def _build_ic(**param_overrides):
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.registry import create_strategy

    params = {
        "vol_scaled_exits": False,
        **param_overrides,
    }
    s = create_strategy("iron_condor", strategy_id="ic_test", params=params)
    s._context = MagicMock()
    return s, s._context


def test_iron_condor_pt_does_not_fire_when_only_ltp_mid_would():
    """4-leg analogue. PT requires the spread debit to fall enough below
    the entry credit; ask-on-shorts and bid-on-longs make that harder
    than the LTP-mid path suggested."""
    s, ctx = _build_ic(profit_target_pct=40.0, stop_loss_pct=300.0)
    s._entered = True
    s._entry_credit = Decimal("100")
    s._short_ce_token, s._short_pe_token = 1, 2
    s._long_ce_token, s._long_pe_token = 3, 4
    s._expiry = date(2026, 5, 1)
    ctx.clock = _mock_clock(datetime(2026, 4, 28, 11, 30))

    # LTP-mid: short_ce=20, short_pe=20, long_ce=10, long_pe=10
    #   → debit = (20+20) − (10+10) = 20 → decay = (100−20)/100 = 80% (would fire)
    # Realistic: shorts bid=20 ask=40; longs bid=1 ask=10 (bid>0 to avoid the
    #   LTP fallback in _bid_ask_for, which requires bid>0 AND ask>0).
    #   debit = (short_ce_ask + short_pe_ask) − (long_ce_bid + long_pe_bid)
    #         = (40+40) − (1+1) = 78 → decay = (100−78)/100 = 22% < 40% PT (no fire).
    ltp_map = {1: Decimal("20"), 2: Decimal("20"), 3: Decimal("10"), 4: Decimal("10")}
    bid_ask_map = {
        1: (20.0, 40.0),  # short_ce
        2: (20.0, 40.0),  # short_pe
        3: (1.0, 10.0),   # long_ce wing — bid>0 to avoid LTP-fallback
        4: (1.0, 10.0),   # long_pe wing — bid>0 to avoid LTP-fallback
    }
    ctx.get_ltp.side_effect = lambda tok: ltp_map[tok]
    ctx.get_tick.side_effect = lambda tok: _tick_with(*bid_ask_map[tok])

    sig = s._check_adjustments()
    assert sig is None


def test_iron_condor_adjustment_trigger_uses_bid_ask_not_ltp_mid():
    """Adjustment trigger is the OTHER spot the audit flagged. With LTP-
    mid the call-side close cost would breach the threshold; with bid/
    ask it doesn't. The fix means the trigger now matches what the
    rolling adjustment will actually pay."""
    s, ctx = _build_ic(
        profit_target_pct=99.0,        # disable PT
        stop_loss_pct=999.0,           # disable SL
        adjustment_threshold_pct=50.0,
    )
    s._entered = True
    s._entry_credit = Decimal("100")
    s._short_ce_token, s._short_pe_token = 1, 2
    s._long_ce_token, s._long_pe_token = 3, 4
    s._expiry = date(2026, 5, 1)
    s.MAX_ADJUSTMENTS_PER_DAY = 99
    s._adjustments_today = 0
    s._last_adjustment_time = 0.0
    ctx.clock = _mock_clock(datetime(2026, 4, 28, 11, 30))

    # LTP-mid: short_ce=70, short_pe=10, long_ce=10, long_pe=5
    #   call_close_cost (LTP) = 70 − 10 = 60 → 60% > 50% threshold (would fire BUG)
    #   put_close_cost  (LTP) = 10 −  5 =  5 → safe
    # Realistic: short_ce ask=70 (same), long_ce bid=30 (much higher than LTP)
    #   call_close_cost = 70 − 30 = 40 → 40% < 50% (no fire — correct)
    ltp_map = {1: Decimal("70"), 2: Decimal("10"), 3: Decimal("10"), 4: Decimal("5")}
    bid_ask_map = {
        1: (50.0, 70.0),    # short_ce — ask=70 same as LTP
        2: (5.0, 15.0),     # short_pe — ask=15
        3: (30.0, 50.0),    # long_ce  — bid=30, well above LTP=10
        4: (3.0, 7.0),      # long_pe
    }
    ctx.get_ltp.side_effect = lambda tok: ltp_map[tok]
    ctx.get_tick.side_effect = lambda tok: _tick_with(*bid_ask_map[tok])

    sig = s._check_adjustments()
    # No PT, no SL, no adjustment (call cost on bid/ask = 40% < 50%).
    assert sig is None


# ── long_calendar ─────────────────────────────────────────────────────


def _build_calendar(**param_overrides):
    from src.backtest.common import import_strategies
    import_strategies()
    from src.strategy.registry import create_strategy

    params = {
        "max_underlying_move_pct": 999.0,  # disable underlying-move stop
        **param_overrides,
    }
    s = create_strategy("long_calendar", strategy_id="lc_test", params=params)
    s._context = MagicMock()
    return s, s._context


def test_long_calendar_pt_does_not_fire_when_only_ltp_mid_would():
    """Calendar is opposite direction from the shorts: it's a debit
    spread, so PROFIT happens when ``current_value`` (back_bid - front_ask
    on close) exceeds entry. LTP-mid (back_ltp - front_ltp) overstates
    the close credit; the fix means PT only fires on the realistic
    sell-back-at-bid / buy-front-at-ask spread."""
    s, ctx = _build_calendar(profit_target_pct=30.0, stop_loss_pct=999.0)
    s._entered = True
    s._entry_debit = Decimal("50")
    s._peak_value = Decimal("0")
    s._front_token, s._back_token = 11, 22
    s._strike = 22500.0
    ctx.clock = _mock_clock(datetime(2026, 4, 28, 11, 30))

    # LTP-mid: back_ltp − front_ltp = 80 − 12 = 68 → +36% (would fire PT 30%, BUG)
    # Realistic: back_bid − front_ask = 70 − 15 = 55 → +10% < 30% PT (correct, no fire)
    ltp_map = {11: Decimal("12"), 22: Decimal("80")}  # front, back
    bid_ask_map = {
        11: (10.0, 15.0),  # front: bid 10, ask 15
        22: (70.0, 90.0),  # back:  bid 70, ask 90
    }
    ctx.get_ltp.side_effect = lambda tok: ltp_map[tok]
    ctx.get_tick.side_effect = lambda tok: _tick_with(*bid_ask_map[tok])
    ctx.get_spot_price.return_value = Decimal("22500")

    sig = s._check_exit_conditions()
    assert sig is None


def test_long_calendar_pt_fires_when_realistic_credit_clears_target():
    """Companion to the above: when the realistic close credit DOES
    clear the PT target, the signal fires. Sanity-check that the test
    setup isn't inadvertently disabling the path."""
    s, ctx = _build_calendar(profit_target_pct=30.0, stop_loss_pct=999.0)
    s._entered = True
    s._entry_debit = Decimal("50")
    s._peak_value = Decimal("0")
    s._front_token, s._back_token = 11, 22
    s._strike = 22500.0
    ctx.clock = _mock_clock(datetime(2026, 4, 28, 11, 30))

    # back_bid − front_ask = 80 − 10 = 70 → +40% > 30% PT (fires)
    ltp_map = {11: Decimal("12"), 22: Decimal("85")}
    bid_ask_map = {11: (8.0, 10.0), 22: (80.0, 95.0)}
    ctx.get_ltp.side_effect = lambda tok: ltp_map[tok]
    ctx.get_tick.side_effect = lambda tok: _tick_with(*bid_ask_map[tok])
    ctx.get_spot_price.return_value = Decimal("22500")

    sig = s._check_exit_conditions()
    assert sig is not None
    assert "Profit target" in (sig.reason or "")
