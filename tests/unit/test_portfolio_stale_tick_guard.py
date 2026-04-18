"""Tests for the stale-tick / impossible-move guard added Apr 18 2026.

Concrete trigger: the 13-day chain replay (scripts/replay_23days.py) on
Apr 17 2026 entered a TREND debit_spread at spot=22505 followed 28 minutes
later by an IC entry at spot=24268 — a phantom 7.8% move caused by
corrupted chain-recorder snapshots. The strategy had no defense and acted
on the bad data. In live trading the same gap could fire from a Kite
WebSocket reconnect after a heartbeat timeout.

The guard sits at the very top of `PortfolioStrategy.on_tick` and refuses
to advance any state when a tick implies >2% spot move per minute (pro-
rated by elapsed seconds since the last accepted tick). Real NIFTY has
never moved 2% in a minute without circuit-breaker halts, so the guard
favours false-positives on chaos days over silent execution on bad data.

Tests stay surgical — they call `await strategy.on_tick(tick)` with a
mocked context that controls spot and clock, then assert the guard's
side effects (return value, internal counter, baseline preservation).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.core.models import Tick
from src.strategy.implementations.portfolio_strategy import PortfolioStrategy
from src.strategy.params import PortfolioParams


# ── Fixtures ────────────────────────────────────────────────────────


def _make_tick(ts: datetime, ltp: float = 24000.0) -> Tick:
    """Build a tick whose contents the guard ignores — only ts is read.

    The guard reads spot via `ctx.get_spot_price(...)`, NOT `tick.ltp`.
    This is the key invariant: tick is a wake-up signal; spot is
    authoritative via the chain builder. Ltp is set realistically just so
    the rest of `on_tick` (if it runs) doesn't trip on zero-price math.
    """
    return Tick(
        instrument_token=256265,  # NIFTY 50
        tradingsymbol="NIFTY 50",
        timestamp=ts,
        ltp=Decimal(str(ltp)),
    )


def _make_ctx(*, spot: float, clock_now: datetime) -> MagicMock:
    """Minimal context: spot lookup + clock + VIX + builder shims.

    The guard runs before any of the heavy on_tick logic, so VIX/regime/
    chain mocks just need to NOT raise — the early `return None` from a
    guard hit means the rest is never reached. For the happy-path tests
    we still set `get_vix`/`get_ltp` to safe values because on_tick falls
    through to evaluate-premium etc. once the guard passes.
    """
    ctx = MagicMock()
    ctx.get_spot_price = MagicMock(return_value=Decimal(str(spot)))
    ctx.get_vix = MagicMock(return_value=18.0)
    ctx.get_ltp = MagicMock(return_value=Decimal("0"))
    ctx.clock = MagicMock()
    ctx.clock.now = MagicMock(return_value=clock_now)
    # _build_banknifty_morning_range scans this tuple — empty is fine.
    ctx._spot_tokens = ()
    # Suppress regime detector and chain builder from being invoked
    # during the (post-guard) happy-path; tests assert on guard state
    # before strategy logic gets that far.
    ctx._chain_builder = MagicMock()
    ctx._chain_builder.get_spot_price = MagicMock(return_value=Decimal(str(spot)))
    ctx._aggregator = MagicMock()
    ctx._aggregator.get_completed_candles = MagicMock(return_value=[])
    ctx._feed = MagicMock()
    ctx._feed.get_ltp = MagicMock(return_value=Decimal("0"))
    return ctx


def _make_strategy() -> PortfolioStrategy:
    """Construct a strategy with default params; context patched per-test."""
    return PortfolioStrategy("test_stale_guard", PortfolioParams())


# ── Test class ──────────────────────────────────────────────────────


class TestStaleTickGuard:

    @pytest.mark.asyncio
    async def test_first_tick_always_accepted(self):
        """No baseline ⇒ no comparison possible. First tick must pass.

        Documented behavior: `_last_sane_spot=0.0` is the sentinel for
        "not yet seen a good tick this session" and the guard short-
        circuits without rejecting. Without this rule the strategy would
        be paralyzed at session start.
        """
        s = _make_strategy()
        ts = datetime(2026, 4, 18, 9, 20)
        s.set_context(_make_ctx(spot=24000.0, clock_now=ts))

        await s.on_tick(_make_tick(ts))

        assert s._last_sane_spot == 24000.0
        assert s._last_sane_spot_ts == ts
        assert s._stale_ticks_today == 0

    @pytest.mark.asyncio
    async def test_small_move_within_budget_accepted(self):
        """0.5% move in 60s ⇒ allowed budget is 2.0%, well under. Pass.

        This is the typical case — NIFTY moves a few bps per minute most
        of the day. The guard must not interfere with normal operation.
        """
        s = _make_strategy()
        # Seed baseline at 9:30 with spot=24000
        ts0 = datetime(2026, 4, 18, 9, 20)
        s.set_context(_make_ctx(spot=24000.0, clock_now=ts0))
        await s.on_tick(_make_tick(ts0))

        # 60s later, spot is 24120 (+0.5%) — well within 2%/min budget.
        ts1 = datetime(2026, 4, 18, 9, 21)
        s.ctx.get_spot_price = MagicMock(return_value=Decimal("24120"))
        s.ctx.clock.now = MagicMock(return_value=ts1)
        await s.on_tick(_make_tick(ts1))

        assert s._last_sane_spot == 24120.0
        assert s._stale_ticks_today == 0

    @pytest.mark.asyncio
    async def test_large_move_in_one_minute_rejected(self):
        """7% move in 28 min — the actual Apr 17 2026 chain bug pattern.

        Allowed budget at dt=28min is 2%/min × 28min = 56%. But the
        per-second scaling makes the comparison tighter than that — at
        dt=60s the budget is 2%, at dt=120s it's 4%, etc. We test the
        immediate "next minute" case where the bad data shows up and the
        budget should be ~2%.
        """
        s = _make_strategy()
        ts0 = datetime(2026, 4, 18, 11, 2)
        s.set_context(_make_ctx(spot=22500.0, clock_now=ts0))
        await s.on_tick(_make_tick(ts0))

        # 60s later: spot jumps to 24100 (+7.1%) — far above 2% budget.
        ts1 = datetime(2026, 4, 18, 11, 3)
        s.ctx.get_spot_price = MagicMock(return_value=Decimal("24100"))
        s.ctx.clock.now = MagicMock(return_value=ts1)
        result = await s.on_tick(_make_tick(ts1))

        assert result is None
        # Baseline preserved — we did NOT update to the bad value, so the
        # next tick is compared against the LAST GOOD spot, not the
        # corrupted one. This is the core invariant.
        assert s._last_sane_spot == 22500.0
        assert s._last_sane_spot_ts == ts0
        assert s._stale_ticks_today == 1

    @pytest.mark.asyncio
    async def test_baseline_preserved_across_repeated_bad_ticks(self):
        """Sustained bad-data window — every tick is rejected, baseline holds.

        If we accidentally updated the baseline on rejection, two
        consecutive bad ticks at the same corrupt level would produce a
        zero delta on the second one and pass. The guard must keep the
        last KNOWN GOOD baseline, no matter how long the bad window lasts.
        """
        s = _make_strategy()
        ts0 = datetime(2026, 4, 18, 11, 2)
        s.set_context(_make_ctx(spot=22500.0, clock_now=ts0))
        await s.on_tick(_make_tick(ts0))

        for offset_sec, bad_spot in [(60, 24100), (90, 24150), (120, 24080)]:
            ts = datetime(2026, 4, 18, 11, 2, offset_sec % 60) if offset_sec < 60 else \
                 datetime(2026, 4, 18, 11, 3 + (offset_sec - 60) // 60, (offset_sec - 60) % 60)
            s.ctx.get_spot_price = MagicMock(return_value=Decimal(str(bad_spot)))
            s.ctx.clock.now = MagicMock(return_value=ts)
            assert await s.on_tick(_make_tick(ts)) is None

        assert s._last_sane_spot == 22500.0
        assert s._stale_ticks_today == 3

    @pytest.mark.asyncio
    async def test_long_gap_not_policed(self):
        """dt > 120s ⇒ guard does not police (could be lunch / halt / etc).

        Indian equity market doesn't have lunch breaks but commodity
        and currency segments do, and exchange-wide halts (rare) would
        also produce long gaps. Treating 5min+ silence as "definitely
        corruption" would lock the strategy out of post-halt resumption.
        """
        s = _make_strategy()
        ts0 = datetime(2026, 4, 18, 11, 0)
        s.set_context(_make_ctx(spot=22500.0, clock_now=ts0))
        await s.on_tick(_make_tick(ts0))

        # 5 minutes later, spot jumps 5% — large move, but the long gap
        # means we accept it (treat as "real news during a halt").
        ts1 = datetime(2026, 4, 18, 11, 5)
        s.ctx.get_spot_price = MagicMock(return_value=Decimal("23625"))  # +5%
        s.ctx.clock.now = MagicMock(return_value=ts1)
        await s.on_tick(_make_tick(ts1))

        assert s._last_sane_spot == 23625.0
        assert s._stale_ticks_today == 0

    @pytest.mark.asyncio
    async def test_zero_or_missing_spot_silently_skipped(self):
        """Spot=0 (chain not built yet) ⇒ guard takes no action.

        At session start the ChainBuilder hasn't received its first
        chain snapshot, so get_spot_price returns 0 / None. The guard
        is a defense against bad NUMBERS; missing data is a separate
        concern handled by the rest of on_tick (which gates entries on
        spot > 0 implicitly via _evaluate_premium / _evaluate_trend).
        Critically: missing spot must not poison the baseline.
        """
        s = _make_strategy()
        ts0 = datetime(2026, 4, 18, 9, 20)
        ctx = _make_ctx(spot=24000.0, clock_now=ts0)
        s.set_context(ctx)
        await s.on_tick(_make_tick(ts0))
        assert s._last_sane_spot == 24000.0

        # Next tick: chain builder returns 0 (cleared / not refreshed)
        ts1 = datetime(2026, 4, 18, 9, 21)
        ctx.get_spot_price = MagicMock(return_value=Decimal("0"))
        ctx.clock.now = MagicMock(return_value=ts1)
        await s.on_tick(_make_tick(ts1))

        # Baseline UNCHANGED — we didn't overwrite a good value with junk.
        assert s._last_sane_spot == 24000.0
        assert s._last_sane_spot_ts == ts0
        assert s._stale_ticks_today == 0

    @pytest.mark.asyncio
    async def test_reset_session_clears_baseline(self):
        """End-of-day session reset must clear the baseline.

        Tomorrow's open could legitimately gap several percent from
        today's close (overnight news, US session moves). Without
        clearing, the first tick tomorrow would be compared to today's
        last spot and incorrectly rejected. reset_day_state() is the
        contract called by the runtime at the day boundary.
        """
        s = _make_strategy()
        ts0 = datetime(2026, 4, 18, 9, 20)
        s.set_context(_make_ctx(spot=24000.0, clock_now=ts0))
        await s.on_tick(_make_tick(ts0))
        assert s._last_sane_spot == 24000.0

        s.reset_day_state()

        assert s._last_sane_spot == 0.0
        assert s._last_sane_spot_ts is None
        assert s._stale_ticks_today == 0
        assert s._last_stale_log_minute == -1
