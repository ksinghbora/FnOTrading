"""Per-minute dedup of PREMIUM/TREND skip logs (Apr 21).

Apr 21 expiry day exposed a noise issue: with the daemon healthy and
the premium leg correctly blocked by the 0DTE filter, the
"[portfolio_1] PREMIUM skipped: ..." log fired on every tick — 25 lines
in 6 seconds during the morning sample window we observed. Same shape
applies to the VIX-gate skips (`[VIX_GATE] PREMIUM blocked: ...`).

That made the audit log unusable: real signal (the boot banner, the
filter decisions, eventual entry attempts) drowned in a flood of
"still skipped" repetitions of an already-known state.

The dedup key (PREMIUM_EXPIRY / PREMIUM_VIX_LOW / PREMIUM_VIX_HIGH)
matters: changing skip *reason* must surface immediately even within
the same minute, otherwise an operator misses the moment VIX moves
out of the gate window. These tests pin both invariants.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock

from src.strategy.implementations.portfolio_strategy import PortfolioStrategy
from src.strategy.params import PortfolioParams


def _make_strategy_at(now: datetime) -> PortfolioStrategy:
    """Strategy wired with a clock fixed at `now` so cur_min is deterministic."""
    s = PortfolioStrategy("portfolio_test", PortfolioParams())
    ctx = MagicMock()
    ctx.get_spot_price = MagicMock(return_value=Decimal("24380"))
    ctx.get_vix = MagicMock(return_value=18.5)
    ctx.get_ltp = MagicMock(return_value=Decimal("10"))
    ctx.get_positions = MagicMock(return_value=[])
    ctx.clock = MagicMock()
    ctx.clock.now = MagicMock(return_value=now)
    ctx._spot_tokens = ()
    s.set_context(ctx)
    s._expiry = date(2026, 4, 21)
    return s


class TestSkipLogDedup:
    def test_same_key_same_minute_logs_once(self, caplog):
        s = _make_strategy_at(datetime(2026, 4, 21, 10, 30, 0))
        with caplog.at_level(logging.INFO, logger="src.strategy.implementations.portfolio_strategy"):
            for _ in range(50):
                s._log_skip_throttled("PREMIUM_EXPIRY", "PREMIUM skipped: 0DTE")
        msgs = [r.message for r in caplog.records if "0DTE" in r.message]
        assert len(msgs) == 1, (
            f"Expected exactly 1 dedup'd log line in a single minute, got {len(msgs)}. "
            "PREMIUM/TREND skip logs would flood the audit again."
        )

    def test_minute_rollover_re_emits(self, caplog):
        s = _make_strategy_at(datetime(2026, 4, 21, 10, 30, 0))
        with caplog.at_level(logging.INFO, logger="src.strategy.implementations.portfolio_strategy"):
            s._log_skip_throttled("PREMIUM_EXPIRY", "skipped tick A")
            # Roll the clock forward by 1 minute
            s.ctx.clock.now.return_value = datetime(2026, 4, 21, 10, 31, 0)
            s._log_skip_throttled("PREMIUM_EXPIRY", "skipped tick B")
        msgs = [r.message for r in caplog.records if "skipped tick" in r.message]
        assert msgs == ["skipped tick A", "skipped tick B"], (
            "Minute boundary should re-emit so a long-running block still "
            "produces an audit trail (1 line/min)."
        )

    def test_different_keys_log_independently(self, caplog):
        s = _make_strategy_at(datetime(2026, 4, 21, 10, 30, 0))
        with caplog.at_level(logging.INFO, logger="src.strategy.implementations.portfolio_strategy"):
            s._log_skip_throttled("PREMIUM_EXPIRY", "skipped: expiry")
            s._log_skip_throttled("PREMIUM_VIX_LOW", "skipped: VIX low")
            s._log_skip_throttled("PREMIUM_VIX_HIGH", "skipped: VIX high")
            # Repeating any one of them in the same minute is silenced
            s._log_skip_throttled("PREMIUM_EXPIRY", "skipped: expiry AGAIN")
        msgs = [r.message for r in caplog.records if r.message.startswith("skipped:")]
        assert msgs == [
            "skipped: expiry",
            "skipped: VIX low",
            "skipped: VIX high",
        ], (
            "Distinct skip reasons must surface independently — without this "
            "an operator can't tell when a VIX-gate block flips to an "
            "expiry-day block (or vice versa)."
        )

    def test_dedup_state_clears_on_session_reset(self, caplog):
        # New trading day → previous day's dedup memory must clear so the
        # first occurrence of each reason on the new session logs cleanly.
        s = _make_strategy_at(datetime(2026, 4, 21, 10, 30, 0))
        s._log_skip_throttled("PREMIUM_EXPIRY", "day1")
        assert s._last_skip_log_minute.get("PREMIUM_EXPIRY") is not None
        s.reset_day_state()
        assert s._last_skip_log_minute == {}, (
            "reset_day_state() must clear _last_skip_log_minute or the first "
            "skip on a new session is silently swallowed."
        )
