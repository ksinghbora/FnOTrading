"""Per-minute dedup of [CONFLUENCE] IGNORED/shadow log lines (Apr 21).

A low-confidence morning bias (today: conf=0.00) makes Gate 1
(CONFIDENCE_THRESHOLD) fire on every tick of every active strategy.
Apr 21 produced **~2,600** identical lines like:

    [CONFLUENCE] leg=PREMIUM rule=70 ai_adj=+0 conf=0.00 IGNORED(low_confidence)

in 23 minutes — drowning the genuinely interesting lines (the rare
"applied" path with a non-trivial final score change).

Audit safety: `src/advisor/shadow.py:build_audit` filters
`if d.ai_adj == 0 or d.ai_confidence < 0.7` *before* counting agreements,
so deduping low-confidence rows changes no scorecard math. For the
not_significant and shadow paths, deduping per-tick → per-minute
*improves* accuracy — counting the same decision 50 times per minute
inflates agree/disagree counts.

The "applied" path (Gate 3, enabled=True) is intentionally never
throttled: those decisions are rare and the final score must surface
every time.
"""

from __future__ import annotations

import logging

import pytest

from src.advisor import confluence
from src.advisor.confluence import (
    apply_confluence,
    reset_confluence_log_dedup,
)
from src.advisor.models import DayBias


@pytest.fixture(autouse=True)
def _clear_dedup():
    """Module-level dedup state must not leak between tests."""
    reset_confluence_log_dedup()
    yield
    reset_confluence_log_dedup()


def _bias(*, prem_adj=0, prem_conf=0.0, trend_adj=0, trend_conf=0.0,
          confidence=0.5, sizing_multiplier=1.0) -> DayBias:
    """Build a minimal DayBias with the only fields apply_confluence reads."""
    from datetime import date
    return DayBias(
        date=date(2026, 4, 21),
        confidence=confidence,
        premium_score_adj=prem_adj,
        premium_confidence=prem_conf,
        trend_score_adj=trend_adj,
        trend_confidence=trend_conf,
        sizing_multiplier=sizing_multiplier,
    )


class TestLowConfidenceDedup:
    def test_low_confidence_gate_dedups_within_minute(self, caplog):
        bias = _bias(prem_adj=0, prem_conf=0.0)
        with caplog.at_level(logging.INFO, logger="src.advisor.confluence"):
            for _ in range(50):
                apply_confluence(70, bias, "premium", dedup_minute=630)
        ignored = [r for r in caplog.records if "IGNORED(low_confidence)" in r.message]
        assert len(ignored) == 1, (
            f"Expected 1 dedup'd low-confidence line, got {len(ignored)}. "
            "Apr 21 produced ~2,600 of these in 23 minutes pre-fix."
        )

    def test_minute_rollover_re_emits(self, caplog):
        bias = _bias(prem_adj=0, prem_conf=0.0)
        with caplog.at_level(logging.INFO, logger="src.advisor.confluence"):
            apply_confluence(70, bias, "premium", dedup_minute=630)
            apply_confluence(70, bias, "premium", dedup_minute=631)
        ignored = [r for r in caplog.records if "IGNORED(low_confidence)" in r.message]
        assert len(ignored) == 2

    def test_premium_and_trend_log_independently(self, caplog):
        # Distinct legs must not silence each other within the same minute.
        bias = _bias(prem_adj=0, prem_conf=0.0, trend_adj=0, trend_conf=0.0)
        with caplog.at_level(logging.INFO, logger="src.advisor.confluence"):
            apply_confluence(70, bias, "premium", dedup_minute=630)
            apply_confluence(50, bias, "trend", dedup_minute=630)
            # Repeats silenced
            apply_confluence(70, bias, "premium", dedup_minute=630)
            apply_confluence(50, bias, "trend", dedup_minute=630)
        legs = sorted(
            r.message.split("leg=")[1].split(" ")[0]
            for r in caplog.records if "IGNORED(low_confidence)" in r.message
        )
        assert legs == ["PREMIUM", "TREND"], legs


class TestNotSignificantDedup:
    def test_dedups_within_minute(self, caplog):
        # conf >= 0.7 (passes Gate 1), but |adj| < 5 (Gate 2 blocks)
        bias = _bias(prem_adj=2, prem_conf=0.9)
        with caplog.at_level(logging.INFO, logger="src.advisor.confluence"):
            for _ in range(20):
                apply_confluence(70, bias, "premium", dedup_minute=630)
        ignored = [r for r in caplog.records if "IGNORED(not_significant)" in r.message]
        assert len(ignored) == 1

    def test_low_conf_and_not_sig_are_distinct_keys(self, caplog):
        # Two different IGNORED reasons within the same minute must each
        # surface once — the keys partition by gate_reason.
        low_conf = _bias(prem_adj=0, prem_conf=0.0)
        not_sig = _bias(prem_adj=2, prem_conf=0.9)
        with caplog.at_level(logging.INFO, logger="src.advisor.confluence"):
            apply_confluence(70, low_conf, "premium", dedup_minute=630)
            apply_confluence(70, not_sig, "premium", dedup_minute=630)
        reasons = [
            r.message for r in caplog.records if "IGNORED" in r.message
        ]
        assert any("low_confidence" in m for m in reasons)
        assert any("not_significant" in m for m in reasons)


class TestShadowPathDedup:
    def test_shadow_dedups_within_minute(self, caplog):
        # conf >= 0.7, |adj| >= 5, enabled=False → shadow path
        bias = _bias(prem_adj=10, prem_conf=0.9)
        with caplog.at_level(logging.INFO, logger="src.advisor.confluence"):
            for _ in range(20):
                apply_confluence(70, bias, "premium", enabled=False, dedup_minute=630)
        shadow = [r for r in caplog.records if "(shadow)" in r.message]
        assert len(shadow) == 1


class TestAppliedPathNeverThrottled:
    def test_applied_path_logs_every_call(self, caplog):
        # Gate 3, enabled=True — must surface every decision. These rows
        # are rare (need conf >= 0.7 AND |adj| >= 5 AND advisor active),
        # and the final score can vary tick-to-tick as rule_score moves.
        bias = _bias(prem_adj=10, prem_conf=0.9)
        with caplog.at_level(logging.INFO, logger="src.advisor.confluence"):
            for _ in range(5):
                apply_confluence(70, bias, "premium", enabled=True, dedup_minute=630)
        applied = [r for r in caplog.records if "final=" in r.message]
        assert len(applied) == 5, (
            "Applied confluence decisions must NEVER be throttled — they "
            "drive entries and need a row per occurrence in the audit log."
        )


class TestNoDedupWhenMinuteIsNone:
    def test_unthrottled_when_minute_omitted(self, caplog):
        # Backward compat: callers that don't pass dedup_minute (tests,
        # ad-hoc scripts) get the original always-log behaviour.
        bias = _bias(prem_adj=0, prem_conf=0.0)
        with caplog.at_level(logging.INFO, logger="src.advisor.confluence"):
            for _ in range(5):
                apply_confluence(70, bias, "premium")  # no dedup_minute
        ignored = [r for r in caplog.records if "IGNORED(low_confidence)" in r.message]
        assert len(ignored) == 5, (
            "dedup_minute=None must preserve historical unthrottled "
            "behaviour for backward compatibility."
        )


class TestResetConfluenceLogDedup:
    def test_reset_clears_state(self, caplog):
        bias = _bias(prem_adj=0, prem_conf=0.0)
        with caplog.at_level(logging.INFO, logger="src.advisor.confluence"):
            apply_confluence(70, bias, "premium", dedup_minute=630)
            # Same minute — would be silenced
            apply_confluence(70, bias, "premium", dedup_minute=630)
            reset_confluence_log_dedup()
            # After reset, same minute should emit again
            apply_confluence(70, bias, "premium", dedup_minute=630)
        ignored = [r for r in caplog.records if "IGNORED(low_confidence)" in r.message]
        assert len(ignored) == 2, (
            "reset_confluence_log_dedup() must clear module state — "
            "called from portfolio.reset_day_state() so a new trading "
            "day's first decision logs cleanly."
        )

    def test_module_state_is_isolated_with_fixture(self):
        # Fixture clears between tests; verify it works.
        assert confluence._LAST_LOG_MINUTE == {}
