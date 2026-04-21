"""Per-minute dedup of base-strategy entry-skip logs (Apr 21).

Companion to `test_portfolio_skip_log_dedup.py`. The portfolio fix only
covered portfolio_1's PREMIUM/TREND skip logs; ic_1, strangle_1, and
straddle_1 still flooded the audit log via `BaseStrategy._check_expiry_day_block`
and the VIX gate — Apr 21 expiry produced **19,646** identical
"filter blocked entry: expiry day 0DTE" lines in 23 minutes from those
three strategies alone.

Centralising `_log_skip_throttled` on BaseStrategy means every strategy
inherits the throttle for free, including future ones. These tests pin
the centralised contract.
"""

from __future__ import annotations

import logging
from datetime import datetime
from unittest.mock import MagicMock

from src.strategy.base import BaseStrategy
from src.strategy.params import IronCondorParams


class _Stub(BaseStrategy):
    def get_subscriptions(self): pass
    async def on_start(self): pass
    async def on_tick(self, tick): pass
    async def on_stop(self): pass


def _make_stub(now: datetime, *, is_expiry: bool = True, module: str | None = None) -> _Stub:
    """Construct a stub strategy with a fixed clock + expiry flag."""
    stub = _Stub("test-id", IronCondorParams())
    if module is not None:
        # Override the class module so we can verify subclass-aware logger
        # resolution without needing a separate concrete subclass.
        stub.__class__.__module__ = module
    ctx = MagicMock()
    ctx.clock = MagicMock()
    ctx.clock.now = MagicMock(return_value=now)
    ctx.clock.is_expiry_day = MagicMock(return_value=is_expiry)
    stub.set_context(ctx)
    return stub


class TestBaseSkipLogDedup:
    def test_expiry_block_dedups_within_minute(self, caplog):
        stub = _make_stub(datetime(2026, 4, 21, 11, 0, 0))
        # Subclass-aware logger means we capture at the *stub's* module.
        with caplog.at_level(logging.INFO, logger=stub.__class__.__module__):
            for _ in range(50):
                stub._check_expiry_day_block("NIFTY")
        msgs = [r.message for r in caplog.records if "expiry day 0DTE" in r.message]
        assert len(msgs) == 1, (
            f"Expected exactly 1 expiry-block log per minute, got {len(msgs)}. "
            "Apr 21 produced 19,646 such lines across IC/strangle/straddle "
            "before this dedup."
        )

    def test_expiry_block_re_emits_on_minute_rollover(self, caplog):
        stub = _make_stub(datetime(2026, 4, 21, 11, 0, 0))
        with caplog.at_level(logging.INFO, logger=stub.__class__.__module__):
            stub._check_expiry_day_block("NIFTY")
            stub.ctx.clock.now.return_value = datetime(2026, 4, 21, 11, 1, 0)
            stub._check_expiry_day_block("NIFTY")
        msgs = [r.message for r in caplog.records if "expiry day 0DTE" in r.message]
        assert len(msgs) == 2, "Minute rollover must re-emit (1 line/min audit trail)"

    def test_distinct_underlyings_log_independently(self, caplog):
        # NIFTY and BANKNIFTY should not silence each other — they're
        # different symbols with different expiry calendars and an audit
        # reader needs to see both.
        stub = _make_stub(datetime(2026, 4, 21, 11, 0, 0))
        with caplog.at_level(logging.INFO, logger=stub.__class__.__module__):
            stub._check_expiry_day_block("NIFTY")
            stub._check_expiry_day_block("BANKNIFTY")
            # Repeats silenced
            stub._check_expiry_day_block("NIFTY")
            stub._check_expiry_day_block("BANKNIFTY")
        msgs = [r.message for r in caplog.records if "expiry day 0DTE" in r.message]
        assert len(msgs) == 2, (
            "Distinct underlyings must surface independently — symbol is "
            "part of the dedup key."
        )

    def test_subclass_module_logger_is_used(self, caplog):
        # The original base.py call was `logger.info(...)` which resolved
        # to `src.strategy.base`. After centralisation we use
        # `logging.getLogger(type(self).__module__)` so caplog filters
        # keyed on `src.strategy.implementations.iron_condor` (etc.) keep
        # working — and the log line shows the strategy file, not base.py.
        stub = _make_stub(
            datetime(2026, 4, 21, 11, 0, 0),
            module="src.strategy.implementations.iron_condor",
        )
        with caplog.at_level(
            logging.INFO, logger="src.strategy.implementations.iron_condor"
        ):
            stub._check_expiry_day_block("NIFTY")
        msgs = [r.message for r in caplog.records if "expiry day 0DTE" in r.message]
        assert len(msgs) == 1, (
            "Centralised helper must route logs through the subclass's "
            "module logger or the audit/caplog filters all break."
        )

    def test_reset_day_state_clears_dedup_map(self):
        stub = _make_stub(datetime(2026, 4, 21, 11, 0, 0))
        stub._check_expiry_day_block("NIFTY")
        assert stub._last_skip_log_minute, "dedup map should hold one key"
        stub.reset_day_state()
        assert stub._last_skip_log_minute == {}, (
            "Base reset_day_state must clear _last_skip_log_minute or the "
            "first skip on a new session is silently swallowed."
        )

    def test_disabled_filter_does_not_log(self, caplog):
        # Sanity: when skip_entry_on_expiry_day is False, no log fires.
        stub = _make_stub(datetime(2026, 4, 21, 11, 0, 0))
        stub.params.skip_entry_on_expiry_day = False
        with caplog.at_level(logging.INFO, logger=stub.__class__.__module__):
            assert stub._check_expiry_day_block("NIFTY") is None
        assert not [r for r in caplog.records if "expiry day 0DTE" in r.message]
