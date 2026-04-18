"""Unit tests for scripts/trend_shadow_metrics.py.

These lock the metrics math that the 30-day shadow window decision tree
relies on. The script is imported from scripts/ via sys.path manipulation
because scripts/ isn't on the package path — same approach test_archive_to_parquet.py
and friends use for script-level utilities.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "trend_shadow_metrics.py"


def _load_script_module():
    """Import the script as a module so we can call its functions in tests."""
    spec = importlib.util.spec_from_file_location("trend_shadow_metrics", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["trend_shadow_metrics"] = module
    spec.loader.exec_module(module)
    return module


tsm = _load_script_module()


def _phase_a_row(
    date_str: str = "2026-04-21",
    rule_score: int = 72,
    final_score: int = 72,
    threshold: int = 50,
    f1: int = 30, f2: int = 25, f3: int = 12,
    f4: int = 5, f5: int = 0, f6: int = 0,
    strategy_id: str = "portfolio_hist",
) -> dict:
    """Build a parsed Phase A row matching what collect_rows() returns."""
    return {
        "_date": date_str,
        "_rule_score": rule_score,
        "_final_score": final_score,
        "_threshold": threshold,
        "_phase_a": True,
        "_score_f1_breakout": f1,
        "_score_f2_oi": f2,
        "_score_f3_duration": f3,
        "_score_f4_vix_level": f4,
        "_score_f5_vix_dir": f5,
        "_score_f6_banknifty": f6,
        "strategy_id": strategy_id,
    }


def _pre_a_row(date_str: str, rule_score: int = 72, threshold: int = 50) -> dict:
    return {
        "_date": date_str,
        "_rule_score": rule_score,
        "_final_score": rule_score,
        "_threshold": threshold,
        "_phase_a": False,
    }


class TestEntryRateBand:
    """The PASS/WARN/FAIL classification — the gate of the whole plan."""

    def test_healthy_median_passes(self):
        # 10 entries/day for 5 weekdays — median 10, above PASS_LOWER_BOUND=5
        rows = [_pre_a_row("2026-04-20")] * 10  # Mon
        rows += [_pre_a_row("2026-04-21")] * 10  # Tue
        rows += [_pre_a_row("2026-04-22")] * 10  # Wed
        rows += [_pre_a_row("2026-04-23")] * 10  # Thu
        rows += [_pre_a_row("2026-04-24")] * 10  # Fri
        _, median, _, band, max_zeros = tsm.compute_entry_rate(
            rows, date(2026, 4, 20), date(2026, 4, 24),
        )
        assert median == 10
        assert band == "PASS"
        assert max_zeros == 0

    def test_low_median_warns(self):
        # 4 entries/day median — below PASS=5 but ≥ WARN_LOWER_BOUND=4
        # Build a window with median 4: [4, 4, 4, 5, 4] → median 4
        rows = []
        rows += [_pre_a_row("2026-04-20")] * 4
        rows += [_pre_a_row("2026-04-21")] * 4
        rows += [_pre_a_row("2026-04-22")] * 4
        rows += [_pre_a_row("2026-04-23")] * 5
        rows += [_pre_a_row("2026-04-24")] * 4
        _, median, _, band, _ = tsm.compute_entry_rate(
            rows, date(2026, 4, 20), date(2026, 4, 24),
        )
        assert median == 4
        assert band == "WARN"

    def test_very_low_median_fails(self):
        # 2 entries/day median — well below WARN_LOWER_BOUND=4
        rows = [_pre_a_row(d) for d in [
            "2026-04-20", "2026-04-20",
            "2026-04-21", "2026-04-21",
            "2026-04-22", "2026-04-22",
            "2026-04-23", "2026-04-23",
            "2026-04-24", "2026-04-24",
        ]]
        _, median, _, band, _ = tsm.compute_entry_rate(
            rows, date(2026, 4, 20), date(2026, 4, 24),
        )
        assert median == 2
        assert band == "FAIL"

    def test_five_consecutive_zero_days_triggers_fail(self):
        # Even with healthy median elsewhere, 5 consecutive zero-entry weekdays
        # MUST trigger FAIL (the hard rollback trigger from the plan).
        # Build: 5 zero-days then 5 days of 10 entries each.
        rows = []
        # Apr 20-24 = zero. Apr 27-May 1 = 10 each.
        for d in ["2026-04-27", "2026-04-28", "2026-04-29", "2026-04-30", "2026-05-01"]:
            rows += [_pre_a_row(d)] * 10
        _, median, _, band, max_zeros = tsm.compute_entry_rate(
            rows, date(2026, 4, 20), date(2026, 5, 1),
        )
        assert max_zeros >= 5
        assert band == "FAIL", "5 consecutive zero days must FAIL even with healthy median elsewhere"

    def test_weekends_excluded_from_consecutive_zeros(self):
        # 3 zero weekdays, weekend, more zeros — the weekend gap should NOT
        # reset the consecutive counter (we only count weekdays).
        rows = [_pre_a_row("2026-04-27")] * 5  # Mon Apr 27 only
        # Tue-Fri Apr 21-24 are zero-days; weekend is skipped; Mon Apr 27 has 5.
        # So consecutive zeros counted: Tue-Fri = 4, then breaks.
        _, _, _, _, max_zeros = tsm.compute_entry_rate(
            rows, date(2026, 4, 21), date(2026, 4, 27),
        )
        assert max_zeros == 4


class TestFactorActivation:
    """Per-factor activation rates — drives the 'is this factor dead?' check."""

    def test_all_factors_at_max_positive_in_every_row(self):
        rows = [_phase_a_row(f1=30, f2=25, f3=25, f4=10, f5=10, f6=5)] * 10
        out = tsm.compute_factor_activation(rows)
        for col, stats in out.items():
            assert stats.pos_pct == 100.0, f"{col} should fire positive in 100% of rows"
            assert stats.neg_pct == 0.0
            assert stats.zero_pct == 0.0

    def test_dead_factor_shows_zero_pct_100(self):
        # Factor 6 (BankNifty) never fires — banknifty_confirming was None throughout
        rows = [_phase_a_row(f6=0)] * 10
        out = tsm.compute_factor_activation(rows)
        f6 = out["score_f6_banknifty"]
        assert f6.zero_pct == 100.0
        assert f6.mean == 0.0
        assert f6.min_val == 0
        assert f6.max_val == 0

    def test_mixed_negative_factor(self):
        # Half the rows have BN diverging (-20), half have BN unknown (0)
        rows = [_phase_a_row(f6=-20) for _ in range(5)]
        rows += [_phase_a_row(f6=0) for _ in range(5)]
        out = tsm.compute_factor_activation(rows)
        f6 = out["score_f6_banknifty"]
        assert f6.neg_pct == 50.0
        assert f6.zero_pct == 50.0
        assert f6.pos_pct == 0.0
        assert f6.mean == -10.0  # average of (-20)*5 and (0)*5

    def test_empty_phase_a_returns_empty_dict(self):
        assert tsm.compute_factor_activation([]) == {}


class TestSuppressionAttribution:
    """Which factor was the bottleneck on near-miss rows?"""

    def test_no_near_misses_returns_empty(self):
        # All rows score above threshold — no near-misses to attribute
        rows = [_phase_a_row(final_score=80, threshold=50)] * 10
        result, n = tsm.compute_suppression_attribution(rows, threshold=50)
        assert n == 0
        assert result == {}

    def test_factor6_bn_diverging_attributed_when_only_uplift(self):
        # Final score 35, threshold 50, gap=15.
        # f1=30, f2=0 (gap-uplift=25), f3=0 (uplift=25), f4=10, f5=0 (uplift=10),
        # f6=-20 (uplift=5+20=25).
        # Multiple factors qualify with uplift ≥ 15 → pick the LARGEST uplift.
        # f2=0 → uplift 25, f3=0 → uplift 25, f6=-20 → uplift 25.
        # Tie broken by sort order (Python's sort is stable for ties on the key,
        # but reverse sorting on (uplift, col) means alphabetically-later col
        # wins on ties of uplift). f6 > f3 > f2 alphabetically when reversed.
        rows = [_phase_a_row(
            final_score=35, threshold=50,
            f1=30, f2=0, f3=0, f4=10, f5=0, f6=-20,
        )]
        result, n = tsm.compute_suppression_attribution(rows, threshold=50)
        assert n == 1
        # The largest single-factor uplift wins. With 25-tied factors and
        # tuple-sort tiebreak by colname desc, f6 ("score_f6_banknifty")
        # comes after f3 and f2 alphabetically — wins the tie.
        assert "score_f6_banknifty" in result

    def test_combined_attribution_when_no_single_factor_flips(self):
        # Final 0, threshold 50. Every factor is 0 already except f6=-20.
        # Uplift potentials: f1=30, f2=25, f3=25, f4=10, f5=10, f6=25.
        # Gap = 50. No single factor's uplift ≥ 50 → "combined".
        rows = [_phase_a_row(
            final_score=0, threshold=50,
            f1=0, f2=0, f3=0, f4=0, f5=0, f6=-20,
        )]
        result, n = tsm.compute_suppression_attribution(rows, threshold=50)
        assert n == 1
        assert result == {"combined": 100.0}

    def test_attribution_normalised_to_percentages(self):
        # 4 rows, 2 attributed to f6, 2 attributed to combined → 50/50
        rows = []
        for _ in range(2):
            # f6 uplift = 25 covers gap=15 → f6 single-handedly flips
            rows.append(_phase_a_row(
                final_score=35, threshold=50,
                f1=10, f2=0, f3=0, f4=0, f5=0, f6=-20,
            ))
        for _ in range(2):
            # No single uplift covers gap=50 → combined
            rows.append(_phase_a_row(
                final_score=0, threshold=50,
                f1=0, f2=0, f3=0, f4=0, f5=0, f6=-20,
            ))
        result, n = tsm.compute_suppression_attribution(rows, threshold=50)
        assert n == 4
        assert result.get("combined") == 50.0
        assert result.get("score_f6_banknifty") == 50.0


class TestBuildMetricsIntegration:
    """End-to-end: row dicts → WindowMetrics with all sections populated."""

    def test_phase_a_only_window(self):
        # 10 entries/day × 5 days, all Phase A
        phase_a_rows = []
        for d in ["2026-04-20", "2026-04-21", "2026-04-22", "2026-04-23", "2026-04-24"]:
            phase_a_rows += [_phase_a_row(date_str=d)] * 10
        m = tsm.build_metrics(
            since=date(2026, 4, 20), until=date(2026, 4, 24),
            phase_a_rows=phase_a_rows, pre_a_rows=[], skipped=0,
        )
        assert m.total_entries == 50
        assert m.phase_a_entries == 50
        assert m.pre_phase_a_entries == 0
        assert m.median_entries_per_day == 10
        assert m.band == "PASS"
        assert m.factor_activation  # populated
        assert m.suppression_n == 0  # all entries above threshold

    def test_mixed_window_with_pre_a_rows(self):
        # 5 Phase A + 5 pre-A on same day
        phase_a_rows = [_phase_a_row(date_str="2026-04-21")] * 5
        pre_a_rows = [_pre_a_row("2026-04-21")] * 5
        m = tsm.build_metrics(
            since=date(2026, 4, 21), until=date(2026, 4, 21),
            phase_a_rows=phase_a_rows, pre_a_rows=pre_a_rows, skipped=0,
        )
        assert m.total_entries == 10
        assert m.phase_a_entries == 5
        assert m.pre_phase_a_entries == 5
        # Note about pre-A rows must be present
        assert any("pre-Phase-A" in n for n in m.notes)

    def test_no_phase_a_rows_emits_note(self):
        pre_a_rows = [_pre_a_row("2026-04-21")] * 10
        m = tsm.build_metrics(
            since=date(2026, 4, 21), until=date(2026, 4, 21),
            phase_a_rows=[], pre_a_rows=pre_a_rows, skipped=0,
        )
        assert m.factor_activation == {}
        assert any("no Phase A rows" in n for n in m.notes)

    def test_skipped_rows_noted(self):
        m = tsm.build_metrics(
            since=date(2026, 4, 21), until=date(2026, 4, 21),
            phase_a_rows=[_phase_a_row()], pre_a_rows=[], skipped=3,
        )
        assert any("3 rows skipped" in n for n in m.notes)


class TestResolveWindow:
    def test_default_30_day_window_ending_today(self):
        import argparse
        args = argparse.Namespace(since=None, until=None)
        since, until = tsm.resolve_window(args)
        assert (until - since).days == 30

    def test_custom_window(self):
        import argparse
        args = argparse.Namespace(since="2026-04-01", until="2026-04-30")
        since, until = tsm.resolve_window(args)
        assert since == date(2026, 4, 1)
        assert until == date(2026, 4, 30)

    def test_inverted_window_raises(self):
        import argparse
        args = argparse.Namespace(since="2026-04-30", until="2026-04-01")
        with pytest.raises(ValueError):
            tsm.resolve_window(args)
