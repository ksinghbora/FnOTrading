"""Tests for the counterfactual variants framework (Apr 17 trader-analysis #15).

The framework records what *other* parameter choices would have decided at
every live decision point. The tests pin down:

  - evaluate_entry's skip hierarchy (expiry → VIX → PCR → score)
  - evaluate_trail_activation's two-gate (time + decay)
  - the never-raises contract on record_entry_decision
  - CSV header + per-variant rows on disk
  - diff_summary's agree/disagree/stricter/looser bookkeeping
"""

from __future__ import annotations

import csv
from datetime import time
from pathlib import Path

import pytest

from src.strategy.counterfactual import (
    CounterfactualEvaluator,
    VariantSpec,
    diff_summary,
)


class TestVariantSpecEntryHierarchy:
    """First-match-wins skip reasons: expiry > VIX > PCR > score."""

    def test_expiry_block_fires_first_when_default(self):
        v = VariantSpec(name="default", score_threshold=65)
        # All other gates would also pass — expiry must take priority
        assert v.evaluate_entry(
            base_score=80, base_threshold=65,
            features={"vix": 15.0, "pcr_oi": 1.0}, is_expiry=True,
        ) == "SKIP-expiry"

    def test_expiry_override_lets_entry_through(self):
        v = VariantSpec(name="trade_expiry", skip_entry_on_expiry_day=False)
        assert v.evaluate_entry(
            base_score=80, base_threshold=65,
            features={"vix": 15.0}, is_expiry=True,
        ) == "ENTER"

    def test_vix_low_skip(self):
        v = VariantSpec(name="vix_band", vix_entry_min=14.0, vix_entry_max=20.0)
        out = v.evaluate_entry(
            base_score=80, base_threshold=65,
            features={"vix": 12.5}, is_expiry=False,
        )
        assert out.startswith("SKIP-vix_low")
        assert "12.5" in out and "14.0" in out

    def test_vix_high_skip(self):
        v = VariantSpec(name="vix_band", vix_entry_max=20.0)
        out = v.evaluate_entry(
            base_score=80, base_threshold=65,
            features={"vix": 25.0}, is_expiry=False,
        )
        assert out.startswith("SKIP-vix_high")

    def test_vix_zero_treated_as_unknown_does_not_block(self):
        # A zero / missing VIX should not skip — it's "unknown" not "low"
        v = VariantSpec(name="vix_min", vix_entry_min=14.0)
        assert v.evaluate_entry(
            base_score=80, base_threshold=65,
            features={"vix": 0}, is_expiry=False,
        ) == "ENTER"

    def test_vix_inside_band_passes_to_score_check(self):
        v = VariantSpec(name="vix_band", vix_entry_min=14.0, vix_entry_max=20.0,
                        score_threshold=70)
        # Score below threshold → score skip (proves we got past VIX)
        out = v.evaluate_entry(
            base_score=68, base_threshold=65,
            features={"vix": 16.0}, is_expiry=False,
        )
        assert out.startswith("SKIP-score")

    def test_pcr_filter_skips_extreme_pcr(self):
        v = VariantSpec(name="pcr_on", pcr_filter_enabled=True)
        out = v.evaluate_entry(
            base_score=80, base_threshold=65,
            features={"vix": 15.0, "pcr_oi": 0.4}, is_expiry=False,
        )
        assert out.startswith("SKIP-pcr")

    def test_pcr_filter_disabled_ignores_extreme_pcr(self):
        v = VariantSpec(name="pcr_off", pcr_filter_enabled=False)
        assert v.evaluate_entry(
            base_score=80, base_threshold=65,
            features={"vix": 15.0, "pcr_oi": 0.4}, is_expiry=False,
        ) == "ENTER"

    def test_score_below_variant_threshold_skips(self):
        v = VariantSpec(name="strict", score_threshold=80)
        out = v.evaluate_entry(
            base_score=70, base_threshold=65,
            features={"vix": 15.0}, is_expiry=False,
        )
        assert out == "SKIP-score(70<80)"

    def test_score_at_or_above_threshold_enters(self):
        v = VariantSpec(name="strict", score_threshold=80)
        assert v.evaluate_entry(
            base_score=80, base_threshold=65,
            features={"vix": 15.0}, is_expiry=False,
        ) == "ENTER"

    def test_score_inherits_base_when_unspecified(self):
        v = VariantSpec(name="inherit")
        # base_threshold=65 → score 64 should skip, 65 should enter
        assert v.evaluate_entry(
            base_score=64, base_threshold=65,
            features={"vix": 15.0}, is_expiry=False,
        ) == "SKIP-score(64<65)"
        assert v.evaluate_entry(
            base_score=65, base_threshold=65,
            features={"vix": 15.0}, is_expiry=False,
        ) == "ENTER"


class TestVariantSpecTrailActivation:
    """Trail-stop activation needs both time and decay gates passed."""

    def test_before_activation_time_returns_false(self):
        v = VariantSpec(name="trail_11", trail_stop_activate_after_time=time(11, 0))
        assert v.evaluate_trail_activation(
            decay_pct=10.0, now_t=time(10, 30),
            base_after_time=time(10, 15), base_min_decay=5.0,
        ) is False

    def test_after_time_but_decay_below_min_returns_false(self):
        v = VariantSpec(name="trail_11", trail_stop_activate_after_time=time(11, 0),
                        trail_stop_min_decay_pct=8.0)
        assert v.evaluate_trail_activation(
            decay_pct=5.0, now_t=time(11, 30),
            base_after_time=time(10, 15), base_min_decay=5.0,
        ) is False

    def test_both_gates_pass_returns_true(self):
        v = VariantSpec(name="trail_11", trail_stop_activate_after_time=time(11, 0),
                        trail_stop_min_decay_pct=5.0)
        assert v.evaluate_trail_activation(
            decay_pct=6.0, now_t=time(11, 30),
            base_after_time=time(10, 15), base_min_decay=5.0,
        ) is True

    def test_inherits_base_after_time_when_unspecified(self):
        v = VariantSpec(name="inherit_time")
        # base_after_time=time(10, 15) — at 10:00 should be false
        assert v.evaluate_trail_activation(
            decay_pct=10.0, now_t=time(10, 0),
            base_after_time=time(10, 15), base_min_decay=5.0,
        ) is False

    def test_inherits_base_min_decay_when_unspecified(self):
        v = VariantSpec(name="inherit_decay")
        # base_min_decay=5.0 — decay 4 should be false, 5 should be true
        assert v.evaluate_trail_activation(
            decay_pct=4.0, now_t=time(11, 0),
            base_after_time=time(10, 15), base_min_decay=5.0,
        ) is False
        assert v.evaluate_trail_activation(
            decay_pct=5.0, now_t=time(11, 0),
            base_after_time=time(10, 15), base_min_decay=5.0,
        ) is True


class TestCounterfactualEvaluatorWritesCsv:
    """Round-trip: variants → CSV → reload → expected fields present."""

    def test_writes_header_and_one_row_per_variant(self, tmp_path: Path):
        ev = CounterfactualEvaluator(
            variants=[
                VariantSpec(name="threshold_70", score_threshold=70),
                VariantSpec(name="threshold_80", score_threshold=80),
            ],
            out_dir=tmp_path,
        )
        ev.record_entry_decision(
            strategy_id="strangle_1",
            base_decision="ENTER",
            base_score=72,
            base_threshold=65,
            features={"vix": 15.0, "spot": 24500.0, "pcr_oi": 1.05},
            is_expiry=False,
        )

        files = list(tmp_path.glob("*.csv"))
        assert len(files) == 1
        with files[0].open() as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 2  # one row per variant
        names = {r["variant_name"] for r in rows}
        assert names == {"threshold_70", "threshold_80"}

        by_name = {r["variant_name"]: r for r in rows}
        # threshold_70: 72 >= 70 → ENTER (agrees with base)
        assert by_name["threshold_70"]["variant_decision"] == "ENTER"
        # threshold_80: 72 < 80 → SKIP
        assert by_name["threshold_80"]["variant_decision"].startswith("SKIP-score")
        # Base context preserved
        assert by_name["threshold_70"]["base_decision"] == "ENTER"
        assert by_name["threshold_70"]["base_score"] == "72"
        assert by_name["threshold_70"]["strategy_id"] == "strangle_1"

    def test_appends_within_same_day(self, tmp_path: Path):
        ev = CounterfactualEvaluator(
            variants=[VariantSpec(name="v1", score_threshold=70)],
            out_dir=tmp_path,
        )
        for score in (72, 68):
            ev.record_entry_decision(
                strategy_id="s1", base_decision="ENTER",
                base_score=score, base_threshold=65,
                features={"vix": 15.0}, is_expiry=False,
            )
        files = list(tmp_path.glob("*.csv"))
        assert len(files) == 1
        with files[0].open() as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 2

    def test_never_raises_on_io_failure(self, tmp_path: Path):
        # Point out_dir at a path that can't be created (existing file as parent)
        clash = tmp_path / "blocker"
        clash.write_text("not a directory")
        ev = CounterfactualEvaluator(
            variants=[VariantSpec(name="v1", score_threshold=70)],
            out_dir=clash / "child",  # parent exists as file → mkdir fails
        )
        # Must NOT raise
        ev.record_entry_decision(
            strategy_id="s1", base_decision="ENTER",
            base_score=72, base_threshold=65,
            features={"vix": 15.0}, is_expiry=False,
        )

    def test_never_raises_on_bad_features(self, tmp_path: Path):
        ev = CounterfactualEvaluator(
            variants=[VariantSpec(name="v1", vix_entry_min=14.0)],
            out_dir=tmp_path,
        )
        # vix as a non-numeric string — still must not crash live path
        ev.record_entry_decision(
            strategy_id="s1", base_decision="ENTER",
            base_score=72, base_threshold=65,
            features={"vix": "garbage"}, is_expiry=False,
        )

    def test_empty_variants_writes_nothing(self, tmp_path: Path):
        ev = CounterfactualEvaluator(variants=[], out_dir=tmp_path)
        ev.record_entry_decision(
            strategy_id="s1", base_decision="ENTER",
            base_score=72, base_threshold=65,
            features={"vix": 15.0}, is_expiry=False,
        )
        assert list(tmp_path.glob("*.csv")) == []


class TestDiffSummary:
    """Counterfactual rows → per-variant agree/disagree/stricter/looser."""

    def test_agree_when_both_enter(self):
        rows = [{"variant_name": "v1", "base_decision": "ENTER", "variant_decision": "ENTER"}]
        out = diff_summary(rows)
        assert out["v1"] == {"agree": 1, "disagree": 0, "stricter": 0, "looser": 0}

    def test_stricter_when_variant_skips_a_taken_trade(self):
        rows = [
            {"variant_name": "v1", "base_decision": "ENTER",
             "variant_decision": "SKIP-score(68<70)"},
        ]
        out = diff_summary(rows)
        assert out["v1"] == {"agree": 0, "disagree": 1, "stricter": 1, "looser": 0}

    def test_looser_when_variant_enters_a_skipped_trade(self):
        rows = [
            {"variant_name": "v1", "base_decision": "SKIP-score(64<65)",
             "variant_decision": "ENTER"},
        ]
        out = diff_summary(rows)
        assert out["v1"] == {"agree": 0, "disagree": 1, "stricter": 0, "looser": 1}

    def test_agree_when_both_skip_for_same_reason(self):
        rows = [
            {"variant_name": "v1", "base_decision": "SKIP-vix_low",
             "variant_decision": "SKIP-vix_low"},
        ]
        out = diff_summary(rows)
        assert out["v1"]["agree"] == 1

    def test_per_variant_isolation(self):
        rows = [
            # v1 agrees, v2 is stricter
            {"variant_name": "v1", "base_decision": "ENTER", "variant_decision": "ENTER"},
            {"variant_name": "v2", "base_decision": "ENTER",
             "variant_decision": "SKIP-score(68<80)"},
            {"variant_name": "v1", "base_decision": "ENTER", "variant_decision": "ENTER"},
            {"variant_name": "v2", "base_decision": "ENTER",
             "variant_decision": "SKIP-score(70<80)"},
        ]
        out = diff_summary(rows)
        assert out["v1"]["agree"] == 2
        assert out["v2"]["stricter"] == 2
        assert out["v2"]["disagree"] == 2

    def test_rows_without_variant_name_skipped(self):
        rows = [
            {"variant_name": "", "base_decision": "ENTER", "variant_decision": "ENTER"},
            {"base_decision": "ENTER", "variant_decision": "ENTER"},
        ]
        assert diff_summary(rows) == {}

    def test_empty_rows_returns_empty(self):
        assert diff_summary([]) == {}
