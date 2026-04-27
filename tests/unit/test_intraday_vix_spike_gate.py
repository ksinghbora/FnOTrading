"""Tests for Phase 3b Gate B — intraday VIX spike filter.

Pre-registered hypothesis: blocking new entries on days where VIX has
risen >= 15% from morning open after 11:30 IST will modestly improve
iron_condor's wf_coverage by avoiding entries on shock days like
2025-05-08 (VIX 15.6 → 22.8 in last 90 min).

See reports/phase3b_research/regime_gate_proposal.md §4 Gate B.

Tests assert pure gate logic in isolation:
- Default disabled (opt-in only)
- Capture is idempotent within a trading day
- Capture resets on date change
- Activation time gates the check
- Spike threshold compares ratio not absolute
- VIX feed unavailability is non-fatal
"""

from __future__ import annotations

from datetime import datetime, time
from unittest.mock import MagicMock

import pytest

from src.strategy.implementations.iron_condor import IronCondorStrategy
from src.strategy.params import IronCondorParams


def _make_ctx(*, vix: float, now: datetime) -> MagicMock:
    ctx = MagicMock()
    ctx.get_vix = MagicMock(return_value=vix)
    ctx.clock = MagicMock()
    ctx.clock.now = MagicMock(return_value=now)
    return ctx


def _make_strategy(**params_kwargs) -> IronCondorStrategy:
    params = IronCondorParams(**params_kwargs)
    s = IronCondorStrategy("test_ic_gate_b", params)
    return s


class TestGateBDisabledByDefault:
    """Default behavior — gate must NOT fire unless explicitly enabled."""

    def test_default_params_have_gate_disabled(self):
        params = IronCondorParams()
        assert params.intraday_vix_spike_enabled is False

    def test_disabled_gate_returns_none_even_on_huge_spike(self):
        s = _make_strategy()  # default: disabled
        s.set_context(_make_ctx(vix=30.0, now=datetime(2025, 5, 8, 14, 0)))
        s._intraday_vix_morning = 15.0
        s._intraday_vix_capture_date = datetime(2025, 5, 8).date()
        assert s._check_intraday_vix_spike_filter() is None


class TestMorningVixCapture:
    """_capture_morning_vix_if_needed — runs once per day, never before 9:15."""

    def test_pre_915_does_not_capture(self):
        s = _make_strategy(intraday_vix_spike_enabled=True)
        s.set_context(_make_ctx(vix=15.0, now=datetime(2025, 5, 8, 9, 10)))
        s._capture_morning_vix_if_needed()
        assert s._intraday_vix_morning is None

    def test_first_call_after_915_captures(self):
        s = _make_strategy(intraday_vix_spike_enabled=True)
        s.set_context(_make_ctx(vix=15.6, now=datetime(2025, 5, 8, 9, 16)))
        s._capture_morning_vix_if_needed()
        assert s._intraday_vix_morning == 15.6
        assert s._intraday_vix_capture_date == datetime(2025, 5, 8).date()

    def test_second_call_same_day_idempotent(self):
        s = _make_strategy(intraday_vix_spike_enabled=True)
        s.set_context(_make_ctx(vix=15.6, now=datetime(2025, 5, 8, 9, 16)))
        s._capture_morning_vix_if_needed()
        # VIX moves intraday — capture must NOT update
        s.ctx.get_vix = MagicMock(return_value=22.8)
        s.ctx.clock.now = MagicMock(return_value=datetime(2025, 5, 8, 14, 0))
        s._capture_morning_vix_if_needed()
        assert s._intraday_vix_morning == 15.6  # unchanged

    def test_date_change_resets_capture(self):
        s = _make_strategy(intraday_vix_spike_enabled=True)
        s.set_context(_make_ctx(vix=15.6, now=datetime(2025, 5, 8, 9, 16)))
        s._capture_morning_vix_if_needed()
        # Next day, fresh VIX
        s.ctx.get_vix = MagicMock(return_value=20.9)
        s.ctx.clock.now = MagicMock(return_value=datetime(2025, 5, 9, 9, 16))
        s._capture_morning_vix_if_needed()
        assert s._intraday_vix_morning == 20.9
        assert s._intraday_vix_capture_date == datetime(2025, 5, 9).date()

    def test_zero_vix_does_not_capture(self):
        s = _make_strategy(intraday_vix_spike_enabled=True)
        s.set_context(_make_ctx(vix=0.0, now=datetime(2025, 5, 8, 9, 16)))
        s._capture_morning_vix_if_needed()
        assert s._intraday_vix_morning is None  # cold feed


class TestGateBLogic:
    """When gate is enabled and morning VIX is captured, spike check fires."""

    def test_no_morning_capture_yet_gate_inactive(self):
        s = _make_strategy(intraday_vix_spike_enabled=True)
        s.set_context(_make_ctx(vix=22.0, now=datetime(2025, 5, 8, 12, 0)))
        # No morning capture (would normally happen on first tick)
        s._intraday_vix_morning = None
        assert s._check_intraday_vix_spike_filter() is None

    def test_before_activation_time_no_block(self):
        s = _make_strategy(intraday_vix_spike_enabled=True)
        s.set_context(_make_ctx(vix=22.0, now=datetime(2025, 5, 8, 11, 0)))
        s._intraday_vix_morning = 15.0
        s._intraday_vix_capture_date = datetime(2025, 5, 8).date()
        # Big spike but it's only 11:00, gate activates 11:30
        assert s._check_intraday_vix_spike_filter() is None

    def test_at_activation_time_with_spike_blocks(self):
        s = _make_strategy(intraday_vix_spike_enabled=True)
        s.set_context(_make_ctx(vix=22.8, now=datetime(2025, 5, 8, 14, 0)))
        s._intraday_vix_morning = 15.6  # May 8 morning
        s._intraday_vix_capture_date = datetime(2025, 5, 8).date()
        result = s._check_intraday_vix_spike_filter()
        assert result is not None
        assert "intraday VIX spike" in result
        assert "15.60" in result  # morning open
        assert "22.80" in result  # current

    def test_spike_below_threshold_does_not_block(self):
        s = _make_strategy(intraday_vix_spike_enabled=True)
        s.set_context(_make_ctx(vix=17.0, now=datetime(2025, 5, 8, 12, 0)))
        s._intraday_vix_morning = 15.6
        s._intraday_vix_capture_date = datetime(2025, 5, 8).date()
        # 17.0 / 15.6 = 1.090, below 1.15 threshold
        assert s._check_intraday_vix_spike_filter() is None

    def test_spike_exactly_at_threshold_does_not_block(self):
        s = _make_strategy(intraday_vix_spike_enabled=True)
        # Use exact-rational pair: 11.5 / 10.0 == 1.15 exactly (no float drift)
        s.set_context(_make_ctx(vix=11.5, now=datetime(2025, 5, 8, 12, 0)))
        s._intraday_vix_morning = 10.0
        s._intraday_vix_capture_date = datetime(2025, 5, 8).date()
        # 11.5 / 10.0 = 1.15 exactly — strictly > so NOT blocked
        assert s._check_intraday_vix_spike_filter() is None

    def test_spike_just_above_threshold_blocks(self):
        s = _make_strategy(intraday_vix_spike_enabled=True)
        s.set_context(_make_ctx(vix=11.51, now=datetime(2025, 5, 8, 12, 0)))
        s._intraday_vix_morning = 10.0
        s._intraday_vix_capture_date = datetime(2025, 5, 8).date()
        # 11.51 / 10.0 = 1.151 — just above 1.15
        assert s._check_intraday_vix_spike_filter() is not None

    def test_zero_current_vix_does_not_block(self):
        s = _make_strategy(intraday_vix_spike_enabled=True)
        s.set_context(_make_ctx(vix=0.0, now=datetime(2025, 5, 8, 14, 0)))
        s._intraday_vix_morning = 15.6
        s._intraday_vix_capture_date = datetime(2025, 5, 8).date()
        assert s._check_intraday_vix_spike_filter() is None


class TestGateBCustomParams:
    """Custom threshold and activation-time overrides."""

    def test_custom_threshold_25pct(self):
        s = _make_strategy(
            intraday_vix_spike_enabled=True,
            intraday_vix_spike_threshold_pct=25.0,
        )
        s.set_context(_make_ctx(vix=18.0, now=datetime(2025, 5, 8, 14, 0)))
        s._intraday_vix_morning = 15.0
        s._intraday_vix_capture_date = datetime(2025, 5, 8).date()
        # 18.0 / 15.0 = 1.20 — would block at default 15%, but at 25% does not
        assert s._check_intraday_vix_spike_filter() is None

    def test_custom_activate_after_900(self):
        s = _make_strategy(
            intraday_vix_spike_enabled=True,
            intraday_vix_spike_activate_after=time(9, 0),
        )
        s.set_context(_make_ctx(vix=22.0, now=datetime(2025, 5, 8, 9, 30)))
        s._intraday_vix_morning = 15.0
        s._intraday_vix_capture_date = datetime(2025, 5, 8).date()
        # Activate at 9:00 instead of 11:30 — now blocks at 9:30
        assert s._check_intraday_vix_spike_filter() is not None
