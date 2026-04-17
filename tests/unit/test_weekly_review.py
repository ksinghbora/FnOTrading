"""Tests for the weekly review aggregator (Apr 17 promotion-gate fix).

Without a structured weekly scorecard the promotion ladder in
docs/PROMOTION_CRITERIA.md has no input. The aggregator must:
  - count entries / submits / fills per strategy
  - convert slippage_pct → bps cleanly
  - flag stop-trading conditions (reconcile discrepancies, CB trips)
"""

import sys
from datetime import date
from pathlib import Path

# Make scripts/ importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import weekly_review as wr


def _rec(tag: str, sid: str = "s1", **extra) -> dict:
    return {"tag": tag, "strategy_id": sid, "timestamp": "2026-04-17T11:30:00.000", **extra}


class TestAggregatorBasics:
    def test_empty_records_returns_empty(self):
        assert wr.aggregate([]) == {}

    def test_unknown_strategy_dropped(self):
        # A record without strategy_id falls into _unknown bucket and is dropped.
        records = [{"tag": "RECONCILE", "timestamp": "2026-04-17T10:00:00", "discrepancy_count": 0}]
        assert wr.aggregate(records) == {}

    def test_counts_entries_submits_fills(self):
        records = [
            _rec("ENTRY"), _rec("ENTRY"),
            _rec("SUBMIT"), _rec("SUBMIT"), _rec("SUBMIT"),
            _rec("FILL", slippage_pct=0.1),
            _rec("FILL", slippage_pct=0.2),
        ]
        sw = wr.aggregate(records)["s1"]
        assert sw.entries == 2
        assert sw.submits == 3
        assert sw.fills == 2

    def test_per_strategy_separation(self):
        records = [
            _rec("ENTRY", sid="strangle_1"),
            _rec("FILL", sid="strangle_1", slippage_pct=0.1),
            _rec("ENTRY", sid="ic_1"),
            _rec("FILL", sid="ic_1", slippage_pct=0.5),
        ]
        stats = wr.aggregate(records)
        assert stats["strangle_1"].entries == 1
        assert stats["ic_1"].entries == 1
        assert stats["strangle_1"].fills == 1
        assert stats["ic_1"].fills == 1


class TestSlippage:
    def test_slippage_converted_pct_to_bps(self):
        records = [_rec("FILL", slippage_pct=0.5)]  # 0.5% = 50 bps
        sw = wr.aggregate(records)["s1"]
        assert sw.slippage_bps == [50.0]

    def test_zero_slippage_excluded(self):
        # The Apr 17 paper-broker logs default slippage to 0 when LTP unknown
        # — those bias the median and must not enter the sample.
        records = [
            _rec("FILL", slippage_pct=0),
            _rec("FILL", slippage_pct=0.2),  # 20 bps
            _rec("FILL", slippage_pct=0.4),  # 40 bps
        ]
        sw = wr.aggregate(records)["s1"]
        assert sw.slippage_bps == [20.0, 40.0]
        assert sw.median_slippage_bps == 30.0

    def test_negative_slippage_uses_absolute_value(self):
        # SELL fills below LTP show as negative slippage_pct
        records = [_rec("FILL", slippage_pct=-0.3)]
        sw = wr.aggregate(records)["s1"]
        assert sw.slippage_bps == [30.0]

    def test_no_fills_returns_zero_median(self):
        sw = wr.aggregate([_rec("ENTRY")])["s1"]
        assert sw.median_slippage_bps == 0.0


class TestRiskFlags:
    def test_reconcile_discrepancy_accumulates(self):
        records = [
            _rec("RECONCILE", discrepancy_count=0),
            _rec("RECONCILE", discrepancy_count=2),
            _rec("RECONCILE", discrepancy_count=1),
        ]
        sw = wr.aggregate(records)["s1"]
        assert sw.reconcile_discrepancies == 3

    def test_circuit_breaker_trip_counted(self):
        records = [_rec("CIRCUIT_BREAKER"), _rec("CIRCUIT_BREAKER")]
        sw = wr.aggregate(records)["s1"]
        assert sw.circuit_breaker_trips == 2

    def test_emergency_close_counted(self):
        records = [_rec("EMERGENCY_CLOSE")]
        sw = wr.aggregate(records)["s1"]
        assert sw.emergency_closes == 1


class TestRender:
    def test_empty_stats_renders_no_activity(self):
        out = wr.render_markdown({}, date(2026, 4, 11), date(2026, 4, 17))
        assert "No strategy activity" in out

    def test_reconcile_discrepancy_emits_stop_trading_flag(self):
        records = [_rec("RECONCILE", discrepancy_count=1)]
        stats = wr.aggregate(records)
        out = wr.render_markdown(stats, date(2026, 4, 17), date(2026, 4, 17))
        assert "STOP-TRADING" in out

    def test_high_slippage_emits_investigate_flag(self):
        records = [_rec("FILL", slippage_pct=0.6)]  # 60 bps > 50 threshold
        stats = wr.aggregate(records)
        out = wr.render_markdown(stats, date(2026, 4, 17), date(2026, 4, 17))
        assert "INVESTIGATE" in out
        assert "median slippage" in out

    def test_partial_fill_emits_investigate_flag(self):
        records = [_rec("ENTRY"), _rec("ENTRY"), _rec("FILL", slippage_pct=0.1)]
        stats = wr.aggregate(records)
        out = wr.render_markdown(stats, date(2026, 4, 17), date(2026, 4, 17))
        assert "INVESTIGATE" in out
        assert "partial-fill" in out

    def test_clean_strategy_shows_no_violations(self):
        records = [
            _rec("ENTRY"),
            _rec("FILL", slippage_pct=0.1),
            _rec("FILL", slippage_pct=0.15),
            _rec("RECONCILE", discrepancy_count=0),
        ]
        stats = wr.aggregate(records)
        out = wr.render_markdown(stats, date(2026, 4, 17), date(2026, 4, 17))
        assert "No promotion-gate violations" in out
