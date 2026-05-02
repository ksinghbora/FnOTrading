"""Tests for the Apr 30 2026 Phase 3 honest-rename:

  - ``CombinatorialPurgedCV.evaluate(..., evaluation_mode=...)``
  - Default mode ``"train_in_sample"`` runs the runner on each path's
    train_dates → in-sample fold-stability metric (preserves
    historical behaviour).
  - Mode ``"test_oos"`` runs the runner on each path's test_dates →
    proper out-of-sample evaluation.
  - Result dict carries ``evaluation_mode`` so downstream renderers
    label the section correctly.
  - Invalid modes raise ``ValueError`` (fail-fast vs silent miscount).
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest


def _dates(n: int = 60) -> list[date]:
    """A clean run of business-style dates without holiday gaps —
    plenty for CPCV's fold splitting at small n_folds."""
    base = date(2025, 1, 1)
    return [base + timedelta(days=i) for i in range(n)]


def _make_cv(n_folds: int = 5, n_test_folds: int = 1, max_paths: int = 5):
    from src.backtest.validation.cpcv import CombinatorialPurgedCV
    return CombinatorialPurgedCV(
        n_folds=n_folds,
        n_test_folds=n_test_folds,
        max_paths=max_paths,
        seed=0,
    )


# ── Default mode: train_in_sample ─────────────────────────────────


def test_evaluate_default_mode_calls_runner_with_train_dates():
    """Without an explicit mode, the evaluator must keep the historical
    behaviour: each path runs on train_dates. That's what the
    fold_stability_* gates were calibrated against."""
    cv = _make_cv()
    seen_dates: list[list[date]] = []

    async def runner(dates_arg, params):
        seen_dates.append(list(dates_arg))
        return {"metrics": {"sharpe_ratio": 1.2, "num_trades": 50}}

    result = asyncio.run(cv.evaluate({}, runner, _dates(), n_workers=1))
    assert result["evaluation_mode"] == "train_in_sample"
    # Every path must have been called with its TRAIN slice. The slice
    # excludes the test fold, so it's strictly a SUBSET of the full
    # date list.
    full_set = set(_dates())
    for ds in seen_dates:
        assert set(ds).issubset(full_set)
        # Should be a non-trivial subset (not the full window)
        assert len(ds) < len(full_set)


def test_evaluate_default_mode_result_carries_mode_field():
    cv = _make_cv()

    async def runner(dates_arg, params):
        return {"metrics": {"sharpe_ratio": 0.5, "num_trades": 40}}

    result = asyncio.run(cv.evaluate({}, runner, _dates(), n_workers=1))
    assert "evaluation_mode" in result
    assert result["evaluation_mode"] == "train_in_sample"


# ── OOS mode: test_oos ────────────────────────────────────────────


def test_evaluate_oos_mode_calls_runner_with_test_dates():
    cv = _make_cv()
    seen: list[list[date]] = []

    async def runner(dates_arg, params):
        seen.append(list(dates_arg))
        return {"metrics": {"sharpe_ratio": 0.0, "num_trades": 10}}

    result = asyncio.run(cv.evaluate(
        {}, runner, _dates(),
        n_workers=1,
        evaluation_mode="test_oos",
    ))
    assert result["evaluation_mode"] == "test_oos"
    # The test fold is much smaller than the train slice — at
    # n_folds=5/n_test=1 the test slice is ~20% of the data, so
    # each call's date count is dramatically smaller than under
    # train_in_sample.
    full_n = len(_dates())
    for ds in seen:
        # Test slice ≈ full_n / 5 = 12 dates each (with embargo it's slightly smaller)
        assert len(ds) <= full_n // 4
        assert len(ds) >= 1


def test_oos_mode_distinct_test_folds_per_path():
    """Each CPCV path holds out a distinct combination of fold indices —
    so the test_dates passed to the runner should differ across calls.
    If they didn't, every path would be running on the same window
    (which would be a regression that train_in_sample doesn't surface)."""
    cv = _make_cv(n_folds=4, n_test_folds=1, max_paths=4)
    seen: list[tuple[date, ...]] = []

    async def runner(dates_arg, params):
        seen.append(tuple(dates_arg))
        return {"metrics": {"sharpe_ratio": 0.0, "num_trades": 5}}

    asyncio.run(cv.evaluate(
        {}, runner, _dates(40),
        n_workers=1,
        evaluation_mode="test_oos",
    ))
    # At n_folds=4 with one test fold, distinct paths should have
    # disjoint or non-identical test windows.
    assert len(seen) >= 2
    assert len(set(seen)) >= 2  # at least 2 unique test windows


# ── Validation: bad modes ─────────────────────────────────────────


def test_evaluate_rejects_unknown_mode():
    """Fail fast on typos — silent miscounting (e.g., 'test_oss' →
    treated as in_sample) would be a hard-to-spot bug. Raising on
    construction surfaces the issue immediately."""
    cv = _make_cv()

    async def runner(dates_arg, params):
        return {"metrics": {"sharpe_ratio": 0.0, "num_trades": 1}}

    with pytest.raises(ValueError, match="evaluation_mode"):
        asyncio.run(cv.evaluate(
            {}, runner, _dates(),
            n_workers=1,
            evaluation_mode="invalid_mode",
        ))


# ── Report integration: section heading + gate names by mode ──────


def test_render_markdown_labels_section_with_evaluation_mode():
    """The Fold-Stability Distribution section heading is the same
    regardless of mode, but the body's first line annotates the
    evaluation_mode so the reader knows whether the numbers are
    in-sample or OOS."""
    from datetime import date as _date
    from pathlib import Path
    from unittest.mock import patch
    import numpy as np

    from src.backtest.validation.report import (
        ValidationReport, render_markdown,
    )
    from src.backtest.validation.splits import StrategySplit
    from src.backtest.validation.walk_forward import WFReport, WFWindow

    # Minimal fixtures — re-use the real classes so we don't drift
    # from the production schema.
    win = WFWindow(
        idx=0, train_start=_date(2025, 1, 1), train_end=_date(2025, 3, 1),
        test_start=_date(2025, 3, 2), test_end=_date(2025, 3, 31),
        params={}, train_sharpe=1.0, test_sharpe=1.0,
        train_pnl=100.0, test_pnl=50.0, num_test_trades=10,
    )
    wf = WFReport(
        windows=[win], median_decay=0.1, fraction_positive_test=0.8,
        passed=True, mean_test_sharpe=1.0,
    )
    rng = np.random.default_rng(1)
    dist = rng.normal(1.0, 0.3, size=20)

    # May 2 2026: CPCV moved from section 3 to section 4 (diagnostic)
    # because WF is now the primary OOS verdict in section 3.
    for mode, expected_note, expected_heading in (
        ("train_in_sample", "in-sample fold stability", "## 4. Fold-Stability Distribution (diagnostic)"),
        ("test_oos", "true OOS", "## 4. CPCV Out-Of-Sample Distribution (diagnostic)"),
    ):
        cpcv = {
            "paths": [],
            "sharpe_distribution": dist,
            "sharpe_mean": float(dist.mean()),
            "sharpe_median": float(np.median(dist)),
            "sharpe_p05": float(np.percentile(dist, 5)),
            "sharpe_p95": float(np.percentile(dist, 95)),
            "pbo": None,
            "num_trades_mean": 80.0,
            "evaluation_mode": mode,
        }
        rep = ValidationReport(
            strategy="ic",
            run_id=f"ic_{mode}",
            split=StrategySplit(
                train_end=_date(2025, 3, 31),
                val_end=_date(2025, 4, 30),
                holdout_end=_date(2025, 5, 31),
            ),
            cpcv_result=cpcv,
            wf_report=wf,
            regime_stats={},
            cost_curve={0.5: {"sharpe_ratio": 1.0, "profit_factor": 1.5, "total_pnl": 1000.0}},
            capacity_df=None,
            holdout_metrics=None,
            gates={},
            final_verdict=False,
        )
        out = Path(f"/tmp/_test_render_{mode}.md")
        render_markdown(rep, out)
        text = out.read_text()
        assert expected_heading in text
        assert f"`{mode}`" in text
        assert expected_note in text


# ── Phase 5: mode-aware gate keys + thresholds ─────────────────────


def _cpcv_with_median(mode: str, median_sharpe: float, p05: float = 0.0):
    """Minimal cpcv_result dict for evaluate_gates."""
    return {
        "paths": [],
        "sharpe_distribution": [median_sharpe],
        "sharpe_mean": median_sharpe,
        "sharpe_median": median_sharpe,
        "sharpe_p05": p05,
        "sharpe_p95": median_sharpe,
        "pbo": None,
        "num_trades_mean": 80.0,
        "evaluation_mode": mode,
    }


def _wf_passing():
    """Minimal WFReport that passes wf_decay + wf_coverage so we can
    isolate the CPCV-related gate behaviour."""
    from datetime import date as _date
    from src.backtest.validation.walk_forward import WFReport, WFWindow
    win = WFWindow(
        idx=0, train_start=_date(2025, 1, 1), train_end=_date(2025, 3, 1),
        test_start=_date(2025, 3, 2), test_end=_date(2025, 3, 31),
        params={}, train_sharpe=1.0, test_sharpe=1.0,
        train_pnl=100.0, test_pnl=50.0, num_test_trades=40,
    )
    return WFReport(
        windows=[win], median_decay=0.1, fraction_positive_test=0.8,
        passed=True, mean_test_sharpe=1.0,
    )


def _cost_passing():
    return {0.5: {"sharpe_ratio": 1.0, "profit_factor": 1.5, "total_pnl": 1000.0}}


def test_default_mode_emits_fold_stability_diagnostic_key():
    """May 2 2026 refactor: train_in_sample mode produces a DIAGNOSTIC
    cpcv key (cpcv_fold_stability_diagnostic), never a pass/fail gate.
    WF is now the primary OOS verdict. Even very poor cpcv medians
    must produce ``passed=True`` because cpcv is informative-only."""
    from src.backtest.validation.report import evaluate_gates
    # Even a strongly negative median (-2.0) must NOT fail — diagnostic only
    cpcv = _cpcv_with_median("train_in_sample", median_sharpe=-2.0)
    gates = evaluate_gates(
        cpcv_result=cpcv, wf_report=_wf_passing(),
        regime_stats={}, cost_curve=_cost_passing(), capacity_df=None,
    )
    assert "cpcv_fold_stability_diagnostic" in gates
    assert "cpcv_oos_median_diagnostic" not in gates
    # Old gating keys must be gone
    assert "fold_stability_median_sharpe" not in gates
    assert "fold_stability_pbo" not in gates
    # Diagnostic always passes regardless of median value
    assert gates["cpcv_fold_stability_diagnostic"][0] is True


def test_oos_mode_emits_oos_diagnostic_key():
    """test_oos eval-mode renames the diagnostic key but it's still
    informative-only."""
    from src.backtest.validation.report import evaluate_gates
    cpcv = _cpcv_with_median("test_oos", median_sharpe=-1.0)
    gates = evaluate_gates(
        cpcv_result=cpcv, wf_report=_wf_passing(),
        regime_stats={}, cost_curve=_cost_passing(), capacity_df=None,
    )
    assert "cpcv_oos_median_diagnostic" in gates
    assert "cpcv_fold_stability_diagnostic" not in gates
    assert gates["cpcv_oos_median_diagnostic"][0] is True


def test_legacy_cpcv_result_without_mode_defaults_to_fold_stability_diagnostic():
    """Old cached cpcv_result dicts may lack ``evaluation_mode``. Default
    to fold-stability mode key (and still diagnostic-only)."""
    from src.backtest.validation.report import evaluate_gates
    legacy = _cpcv_with_median("train_in_sample", 0.5)
    legacy.pop("evaluation_mode")  # simulate pre-Phase-3 result dict
    gates = evaluate_gates(
        cpcv_result=legacy, wf_report=_wf_passing(),
        regime_stats={}, cost_curve=_cost_passing(), capacity_df=None,
    )
    assert "cpcv_fold_stability_diagnostic" in gates
    assert "cpcv_oos_median_diagnostic" not in gates
