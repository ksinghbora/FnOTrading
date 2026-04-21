"""Per-minute dedup of TREND hard-block skip logs (Apr 21).

Apr 21 expiry day: `logs/main_20260421_104804.log` contained 240
identical `[trend_1] TREND hard-blocked: DTE=0 (expiry day)` lines
between 11:00:00 and 11:00:59 — then silent until 12:00, then 240
more. The old guard `if now.minute == 0` was true for the entire
60-second window, so every tick during that minute logged.

This is the same noise class as the PREMIUM skip flood fixed in
portfolio_strategy (commit 88ae455). The helper pattern is mirrored
here: `_log_skip_throttled(key, message)` keyed on the wall-clock
minute via `self.ctx.clock.now()`. Distinct keys (TREND_EXPIRY_DAY
vs TREND_EXPIRED) surface independently so a state change still
emits on the next tick instead of being swallowed.

These tests pin the four invariants:
  1. Same key + same minute → exactly 1 log line
  2. Minute rollover → re-emits (audit still has hourly granularity)
  3. Distinct keys → independent (state-change visibility)
  4. Session reset → clears dedup memory
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from unittest.mock import MagicMock

from src.strategy.implementations.trend_debit_spread import TrendDebitSpreadStrategy
from src.strategy.params import TrendDebitSpreadParams


def _make_strategy_at(now: datetime) -> TrendDebitSpreadStrategy:
    """Strategy wired with a clock fixed at `now` so cur_min is deterministic."""
    s = TrendDebitSpreadStrategy("trend_test", TrendDebitSpreadParams())
    ctx = MagicMock()
    ctx.clock = MagicMock()
    ctx.clock.now = MagicMock(return_value=now)
    s.set_context(ctx)
    s._expiry = date(2026, 4, 21)
    return s


class TestTrendSkipLogDedup:
    def test_same_key_same_minute_logs_once(self, caplog):
        s = _make_strategy_at(datetime(2026, 4, 21, 11, 0, 0))
        with caplog.at_level(logging.INFO, logger="src.strategy.implementations.trend_debit_spread"):
            for _ in range(240):
                s._log_skip_throttled(
                    "TREND_EXPIRY_DAY",
                    "[trend_1] TREND hard-blocked: DTE=0 (expiry day)",
                )
        msgs = [r.message for r in caplog.records if "TREND hard-blocked" in r.message]
        assert len(msgs) == 1, (
            f"Expected exactly 1 dedup'd log line in a single minute, got {len(msgs)}. "
            "Apr 21 produced 240 identical lines in the 11:00:00-11:00:59 window — "
            "the same flood would return."
        )

    def test_minute_rollover_re_emits(self, caplog):
        s = _make_strategy_at(datetime(2026, 4, 21, 11, 0, 0))
        with caplog.at_level(logging.INFO, logger="src.strategy.implementations.trend_debit_spread"):
            s._log_skip_throttled("TREND_EXPIRY_DAY", "skipped tick A")
            # Roll the clock forward by 1 minute
            s.ctx.clock.now.return_value = datetime(2026, 4, 21, 11, 1, 0)
            s._log_skip_throttled("TREND_EXPIRY_DAY", "skipped tick B")
        msgs = [r.message for r in caplog.records if "skipped tick" in r.message]
        assert msgs == ["skipped tick A", "skipped tick B"], (
            "Minute boundary should re-emit so a long-running block still "
            "produces an audit trail (1 line/min)."
        )

    def test_different_keys_log_independently(self, caplog):
        s = _make_strategy_at(datetime(2026, 4, 21, 11, 0, 0))
        with caplog.at_level(logging.INFO, logger="src.strategy.implementations.trend_debit_spread"):
            s._log_skip_throttled("TREND_EXPIRY_DAY", "skipped: expiry day")
            s._log_skip_throttled("TREND_EXPIRED", "skipped: already expired")
            # Repeating any one of them in the same minute is silenced
            s._log_skip_throttled("TREND_EXPIRY_DAY", "skipped: expiry day AGAIN")
        msgs = [r.message for r in caplog.records if r.message.startswith("skipped:")]
        assert msgs == [
            "skipped: expiry day",
            "skipped: already expired",
        ], (
            "Distinct skip reasons must surface independently — without this "
            "an operator can't tell when expiry-day flips to past-expiry "
            "(a data/clock bug signal)."
        )

    def test_dedup_state_clears_on_session_reset(self):
        # New trading day → previous day's dedup memory must clear so the
        # first occurrence of each reason on the new session logs cleanly.
        s = _make_strategy_at(datetime(2026, 4, 21, 11, 0, 0))
        s._log_skip_throttled("TREND_EXPIRY_DAY", "day1")
        assert s._last_skip_log_minute.get("TREND_EXPIRY_DAY") is not None
        s.reset_day_state()
        assert s._last_skip_log_minute == {}, (
            "reset_day_state() must clear _last_skip_log_minute or the first "
            "skip on a new session is silently swallowed."
        )
