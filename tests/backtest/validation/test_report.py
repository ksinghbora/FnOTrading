"""Tests for src/backtest/validation/report.py — gate logic + rendering."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.backtest.validation.regime import RegimeStats
from src.backtest.validation.report import (
    ValidationReport,
    evaluate_gates,
    render_markdown,
)
from src.backtest.validation.splits import StrategySplit
from src.backtest.validation.walk_forward import WFReport, WFWindow


# ─── Fixtures ────────────────────────────────────────────────────────


def _split() -> StrategySplit:
    return StrategySplit(
        train_end=date(2025, 4, 30),
        val_end=date(2025, 8, 31),
        holdout_end=date(2026, 1, 31),
    )


def _passing_cpcv() -> dict:
    rng = np.random.default_rng(1)
    dist = rng.normal(1.2, 0.3, size=40)
    return {
        "paths": [],
        "sharpe_distribution": dist,
        "sharpe_mean": float(dist.mean()),
        "sharpe_median": float(np.median(dist)),
        "sharpe_p05": float(np.percentile(dist, 5)),
        "sharpe_p95": float(np.percentile(dist, 95)),
        "pbo": 0.2,
        "num_trades_mean": 80.0,
    }


def _failing_cpcv() -> dict:
    rng = np.random.default_rng(2)
    dist = rng.normal(-0.2, 0.5, size=40)
    return {
        "paths": [],
        "sharpe_distribution": dist,
        "sharpe_mean": float(dist.mean()),
        "sharpe_median": float(np.median(dist)),
        "sharpe_p05": float(np.percentile(dist, 5)),
        "sharpe_p95": float(np.percentile(dist, 95)),
        "pbo": 0.7,
        "num_trades_mean": 80.0,
    }


def _wf(median_decay: float, frac_pos: float, mean_test: float = 0.4) -> WFReport:
    win = WFWindow(
        idx=0,
        train_start=date(2025, 1, 1),
        train_end=date(2025, 3, 31),
        test_start=date(2025, 4, 2),
        test_end=date(2025, 4, 30),
        params={},
        train_sharpe=1.0,
        test_sharpe=max(0.0, 1.0 - median_decay),
        train_pnl=1000.0,
        test_pnl=500.0,
        num_test_trades=40,
    )
    return WFReport(
        windows=[win],
        median_decay=median_decay,
        fraction_positive_test=frac_pos,
        passed=(median_decay < 0.5 and frac_pos >= 0.7),
        mean_test_sharpe=mean_test,
    )


def _regimes_all_pass() -> dict[str, RegimeStats]:
    return {
        "high_vix": RegimeStats("high_vix", 50, 5000.0, 0.8, 62.0, -300.0, True),
        "mid_vix": RegimeStats("mid_vix", 30, 1500.0, 0.5, 55.0, -200.0, True),
    }


def _regimes_with_failure() -> dict[str, RegimeStats]:
    return {
        "high_vix": RegimeStats("high_vix", 50, 5000.0, 0.8, 62.0, -300.0, True),
        # Sharpe < -0.5, num_trades > 20 → fails
        "mid_vix": RegimeStats("mid_vix", 40, -2000.0, -0.8, 40.0, -900.0, False),
    }


def _cost_curve_pass() -> dict[float, dict]:
    return {
        -1.0: {"sharpe_ratio": 2.0, "profit_factor": 3.5, "total_pnl": 8000.0},
        0.0: {"sharpe_ratio": 1.3, "profit_factor": 2.1, "total_pnl": 5000.0},
        0.5: {"sharpe_ratio": 0.6, "profit_factor": 1.5, "total_pnl": 2500.0},
        1.0: {"sharpe_ratio": 0.1, "profit_factor": 1.05, "total_pnl": 500.0},
    }


def _cost_curve_fail() -> dict[float, dict]:
    return {
        0.0: {"sharpe_ratio": 1.2, "profit_factor": 2.0, "total_pnl": 4000.0},
        0.5: {"sharpe_ratio": -0.3, "profit_factor": 0.8, "total_pnl": -1500.0},
        1.0: {"sharpe_ratio": -1.1, "profit_factor": 0.5, "total_pnl": -5000.0},
    }


def _capacity_df_pass() -> pd.DataFrame:
    return pd.DataFrame({
        "lot_size": [75, 150, 300, 750, 1500],
        "total_pnl": [750.0, 1500.0, 3000.0, 7000.0, 12000.0],
        "pnl_per_lot": [10.0, 10.0, 10.0, 9.33, 8.0],
        "avg_slippage_bps": [1.0, 2.0, 4.0, 9.0, 16.0],
        "num_trades_synthetic_fallback": [0, 0, 0, 0, 0],
    })


def _capacity_df_declining() -> pd.DataFrame:
    return pd.DataFrame({
        "lot_size": [75, 150, 300, 750, 1500],
        "total_pnl": [750.0, 1400.0, 2400.0, 5000.0, 8000.0],
        "pnl_per_lot": [10.0, 9.33, 8.0, 6.67, 5.33],
        "avg_slippage_bps": [1.0, 3.0, 6.0, 12.0, 24.0],
        "num_trades_synthetic_fallback": [0, 0, 0, 0, 0],
    })


# ─── Gate tests ──────────────────────────────────────────────────────


def test_evaluate_gates_all_pass() -> None:
    gates = evaluate_gates(
        cpcv_result=_passing_cpcv(),
        wf_report=_wf(median_decay=0.2, frac_pos=0.8),
        regime_stats=_regimes_all_pass(),
        cost_curve=_cost_curve_pass(),
        capacity_df=_capacity_df_pass(),
    )
    for name, (passed, reason) in gates.items():
        assert passed, f"gate {name} unexpectedly failed: {reason}"


def test_evaluate_gates_cpcv_stability_fail() -> None:
    gates = evaluate_gates(
        cpcv_result=_failing_cpcv(),
        wf_report=_wf(median_decay=0.2, frac_pos=0.8),
        regime_stats=_regimes_all_pass(),
        cost_curve=_cost_curve_pass(),
        capacity_df=_capacity_df_pass(),
    )
    assert gates["cpcv_stability"][0] is False
    # The failing cpcv has pbo=0.7 → should also fail cpcv_pbo
    assert gates["cpcv_pbo"][0] is False


def test_evaluate_gates_pbo_warn_when_missing() -> None:
    cpcv = _passing_cpcv()
    cpcv["pbo"] = None
    gates = evaluate_gates(
        cpcv_result=cpcv,
        wf_report=_wf(median_decay=0.2, frac_pos=0.8),
        regime_stats=_regimes_all_pass(),
        cost_curve=_cost_curve_pass(),
        capacity_df=_capacity_df_pass(),
    )
    assert gates["cpcv_pbo"][0] is True
    assert "WARN" in gates["cpcv_pbo"][1]


def test_evaluate_gates_wf_decay_fail() -> None:
    gates = evaluate_gates(
        cpcv_result=_passing_cpcv(),
        wf_report=_wf(median_decay=0.9, frac_pos=0.8),
        regime_stats=_regimes_all_pass(),
        cost_curve=_cost_curve_pass(),
        capacity_df=_capacity_df_pass(),
    )
    assert gates["wf_decay"][0] is False


def test_evaluate_gates_wf_coverage_fail() -> None:
    gates = evaluate_gates(
        cpcv_result=_passing_cpcv(),
        wf_report=_wf(median_decay=0.2, frac_pos=0.4),
        regime_stats=_regimes_all_pass(),
        cost_curve=_cost_curve_pass(),
        capacity_df=_capacity_df_pass(),
    )
    assert gates["wf_coverage"][0] is False


def test_evaluate_gates_regime_fail() -> None:
    gates = evaluate_gates(
        cpcv_result=_passing_cpcv(),
        wf_report=_wf(median_decay=0.2, frac_pos=0.8),
        regime_stats=_regimes_with_failure(),
        cost_curve=_cost_curve_pass(),
        capacity_df=_capacity_df_pass(),
    )
    assert gates["regime"][0] is False
    assert "mid_vix" in gates["regime"][1]


def test_evaluate_gates_cost_sensitivity_fail() -> None:
    gates = evaluate_gates(
        cpcv_result=_passing_cpcv(),
        wf_report=_wf(median_decay=0.2, frac_pos=0.8),
        regime_stats=_regimes_all_pass(),
        cost_curve=_cost_curve_fail(),
        capacity_df=_capacity_df_pass(),
    )
    assert gates["cost_sensitivity"][0] is False


def test_evaluate_gates_capacity_fail() -> None:
    gates = evaluate_gates(
        cpcv_result=_passing_cpcv(),
        wf_report=_wf(median_decay=0.2, frac_pos=0.8),
        regime_stats=_regimes_all_pass(),
        cost_curve=_cost_curve_pass(),
        capacity_df=_capacity_df_declining(),
    )
    assert gates["capacity"][0] is False


def test_evaluate_gates_capacity_none() -> None:
    gates = evaluate_gates(
        cpcv_result=_passing_cpcv(),
        wf_report=_wf(median_decay=0.2, frac_pos=0.8),
        regime_stats=_regimes_all_pass(),
        cost_curve=_cost_curve_pass(),
        capacity_df=None,
    )
    assert gates["capacity"][0] is True
    assert "N/A" in gates["capacity"][1]


# ─── render_markdown ─────────────────────────────────────────────────


def _build_report(passing: bool, tmp_path: Path) -> tuple[ValidationReport, Path]:
    out = tmp_path / "report.md"
    if passing:
        cpcv = _passing_cpcv()
        wf = _wf(median_decay=0.2, frac_pos=0.8)
        regimes = _regimes_all_pass()
        cost = _cost_curve_pass()
        cap = _capacity_df_pass()
    else:
        cpcv = _failing_cpcv()
        wf = _wf(median_decay=0.9, frac_pos=0.3)
        regimes = _regimes_with_failure()
        cost = _cost_curve_fail()
        cap = _capacity_df_declining()

    gates = evaluate_gates(cpcv, wf, regimes, cost, cap)
    final = all(p for p, _ in gates.values())

    report = ValidationReport(
        strategy="portfolio",
        run_id="portfolio_test",
        split=_split(),
        cpcv_result=cpcv,
        wf_report=wf,
        regime_stats=regimes,
        cost_curve=cost,
        capacity_df=cap,
        holdout_metrics=None,
        gates=gates,
        final_verdict=final,
        train_days=[date(2025, 3, 1), date(2025, 3, 2)],
        val_days=[date(2025, 5, 1)],
        holdout_days=[],
    )
    return report, out


def test_render_markdown_passing(tmp_path: Path) -> None:
    report, out = _build_report(passing=True, tmp_path=tmp_path)
    render_markdown(report, out)
    assert out.exists()
    text = out.read_text()

    # Basic structure
    assert "# Validation Report" in text
    assert "## 1. Executive Summary" in text
    assert "## 2. Split" in text
    assert "## 3. CPCV Distribution" in text
    assert "## 4. Walk-Forward" in text
    assert "## 5. Regime Stratification" in text
    assert "## 6. Cost Sensitivity" in text
    assert "## 7. Capacity" in text
    assert "## 8. Holdout" in text

    # Verdict
    assert "**Final Verdict: PASS**" in text
    # Contains PASS markers (at least one)
    assert "PASS" in text
    # Gate reasons visible in table
    assert "median=" in text or "p05=" in text
    # Holdout preserved
    assert "NOT ACCESSED" in text


def test_render_markdown_failing(tmp_path: Path) -> None:
    report, out = _build_report(passing=False, tmp_path=tmp_path)
    render_markdown(report, out)
    text = out.read_text()

    assert "**Final Verdict: FAIL**" in text
    assert "FAIL" in text
    # Failing regime gets bolded
    assert "**mid_vix**" in text
    # Histogram block present
    assert "```" in text


def test_render_markdown_with_holdout(tmp_path: Path) -> None:
    report, out = _build_report(passing=True, tmp_path=tmp_path)
    report.holdout_metrics = {
        "total_pnl": 1234.56,
        "sharpe_ratio": 1.3,
        "win_rate": 60.0,
        "max_drawdown": -500.0,
        "num_trades": 18,
    }
    render_markdown(report, out)
    text = out.read_text()
    assert "NOT ACCESSED" not in text
    assert "1234.56" in text
    assert "Total P&L" in text


def test_render_markdown_creates_parent_dir(tmp_path: Path) -> None:
    report, _ = _build_report(passing=True, tmp_path=tmp_path)
    nested = tmp_path / "deeply" / "nested" / "out.md"
    render_markdown(report, nested)
    assert nested.exists()


def test_render_markdown_capacity_none(tmp_path: Path) -> None:
    report, out = _build_report(passing=True, tmp_path=tmp_path)
    report.capacity_df = None
    # Recompute gates so capacity becomes N/A, not stale.
    report.gates = evaluate_gates(
        cpcv_result=report.cpcv_result,
        wf_report=report.wf_report,
        regime_stats=report.regime_stats,
        cost_curve=report.cost_curve,
        capacity_df=None,
    )
    report.final_verdict = all(p for p, _ in report.gates.values())
    render_markdown(report, out)
    text = out.read_text()
    assert "N/A" in text
