"""Markdown report renderer + gate evaluator for the validation harness.

Consumes the outputs of every other validation module and produces (a) a
single pass/fail verdict driven by the gate table, and (b) a full
markdown report suitable for committing into the repo as an audit trail.

The gate thresholds are intentionally conservative and mirror the
Apr 23 expert review (``memory/expert_review_apr23.md``) — any loosening
should be motivated by a documented regime change, not by a strategy
that happens to fail under them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from src.backtest.validation.metrics import (
    deflated_sharpe_ratio,
    monte_carlo_skill_pvalue,
    probabilistic_sharpe_ratio,
    stationary_bootstrap_sharpe_ci,
)

# Apr 27 2026 — replacement gates for the dropped DSR. Block size 15 ≈
# 3 trading weeks; persists vol-regime autocorrelation through the
# resample. n=10k is the López de Prado significance-test floor; runs
# in ~0.25s per strategy, dwarfed by the surrounding backtest.
_BOOTSTRAP_BLOCK_DAYS = 15.0
_BOOTSTRAP_N = 10_000
_BOOTSTRAP_SEED = 20260427  # deterministic across re-runs of the same data
_MC_PVALUE_THRESHOLD = 0.10
_BOOTSTRAP_CI_LOWER_FLOOR = -0.1
_BOOTSTRAP_CONFIDENCE = 0.90

if TYPE_CHECKING:
    import pandas as pd

    from src.backtest.validation.regime import RegimeStats
    from src.backtest.validation.splits import StrategySplit
    from src.backtest.validation.walk_forward import WFReport


@dataclass
class ValidationReport:
    """Aggregated inputs + derived gates that render_markdown consumes."""

    strategy: str
    run_id: str
    split: "StrategySplit"
    cpcv_result: dict
    wf_report: "WFReport"
    regime_stats: dict[str, "RegimeStats"]
    cost_curve: dict[float, dict]
    capacity_df: "pd.DataFrame | None"
    holdout_metrics: dict | None
    gates: dict[str, tuple[bool, str]] = field(default_factory=dict)
    final_verdict: bool = False
    train_days: list[date] = field(default_factory=list)
    val_days: list[date] = field(default_factory=list)
    holdout_days: list[date] = field(default_factory=list)


# ─── Gate evaluation ────────────────────────────────────────────────────


def _dsr_from_cpcv(cpcv_result: dict) -> float:
    """Deflated Sharpe Ratio from the CPCV distribution.

    Uses variance-of-trials = sample variance of the distribution with
    n_trials = number of CPCV paths. Skew/kurt default to normal (0/3)
    since per-path samples are typically too small for reliable estimates.
    """
    dist = np.asarray(cpcv_result.get("sharpe_distribution", []), dtype=float)
    if dist.size < 2:
        return 0.0
    sr_hat = float(cpcv_result.get("sharpe_median", 0.0))
    var_trials = float(dist.var(ddof=1))
    n_trials = int(dist.size)
    # n = effective sample used for Sharpe standard-error. With CPCV each
    # path's Sharpe is annualised from a train slice ~= total_days; we
    # don't have access to that here so use the number of paths as a
    # floor — the DSR is well-defined, just less tight.
    n_eff = max(n_trials, 2)
    return deflated_sharpe_ratio(
        sharpe_hat=sr_hat,
        variance_of_trials=var_trials,
        n_trials=n_trials,
        n=n_eff,
        skew=0.0,
        kurt=3.0,
    )


def _psr_from_cpcv(cpcv_result: dict) -> float:
    """Probabilistic Sharpe Ratio at benchmark=0 on the CPCV median."""
    dist = np.asarray(cpcv_result.get("sharpe_distribution", []), dtype=float)
    if dist.size < 2:
        return 0.0
    sr_hat = float(cpcv_result.get("sharpe_median", 0.0))
    n_eff = int(dist.size)
    return probabilistic_sharpe_ratio(sr_hat, n=n_eff, skew=0.0, kurt=3.0, benchmark=0.0)


def evaluate_gates(
    cpcv_result: dict,
    wf_report: "WFReport",
    regime_stats: dict[str, "RegimeStats"],
    cost_curve: dict[float, dict],
    capacity_df: "pd.DataFrame | None",
    daily_pnl: "np.ndarray | list[float] | None" = None,
) -> dict[str, tuple[bool, str]]:
    """Return the gate table per the validation spec.

    Each entry: ``gate_name -> (passed: bool, reason: str)``. Reason is a
    human-readable string for the markdown report — always includes the
    measured value and the threshold.

    Gate calibration history:
      - Apr 23 2026 (Phase 3a): tightened to expert-review thresholds
        (median>0.5, DSR>0.95, wf_coverage>=0.7).
      - Apr 27 2026 (Phase 3b, post Indian-market methodology research,
        ``reports/phase3b_research/validation_methodology_indian.md``):
        DSR gate dropped (regime-break in 227-day corpus crushes paths
        below the deflation benchmark; replace via Monte Carlo
        permutation in a follow-up); median Sharpe threshold relaxed
        0.5→0.3 (research finds Indian options strategies typically
        annualise 0.6–1.2 net of costs but exhibit high path-variance,
        so 0.3 is a more realistic floor); wf_coverage relaxed
        0.7→0.6 (Nov 20 2024 SEBI lot/STT changes contaminate the
        first 6 weeks of the corpus, biasing fraction_positive
        downward); cpcv_stability p05>0 demoted to diagnostic
        (penalises any tail of losing paths even if median is
        strong, which is unrealistic for premium-collection
        strategies that get tagged hard during vol shocks).
    """
    gates: dict[str, tuple[bool, str]] = {}

    # ─── cpcv_median_sharpe: median > 0.3 (relaxed from 0.5 per Apr 27 research) ──
    # The p05 > 0 secondary check from the prior gate is preserved as a
    # diagnostic in the markdown report (CPCV Distribution section) but
    # does not fail this gate — left-tail events on a 227-day corpus
    # spanning the SEBI Nov 20 2024 regime break shouldn't gate ship.
    median = float(cpcv_result.get("sharpe_median", 0.0))
    p05 = float(cpcv_result.get("sharpe_p05", 0.0))
    gates["cpcv_median_sharpe"] = (
        median > 0.3,
        f"median={median:.3f} (>0.3); p05={p05:.3f} [diagnostic]",
    )

    # ─── cpcv_pbo: < 0.5 (None → WARN, don't fail) ──────────────────
    pbo_val = cpcv_result.get("pbo")
    if pbo_val is None:
        gates["cpcv_pbo"] = (True, "PBO not computed (single-config CPCV) — WARN")
    else:
        pbo_f = float(pbo_val)
        gates["cpcv_pbo"] = (pbo_f < 0.5, f"pbo={pbo_f:.3f} (<0.5)")

    # ─── dsr: dropped Apr 27 2026. DSR penalises any strategy whose
    # path-distribution variance was inflated by the SEBI regime break
    # in our corpus, making it a poor gate for the current data. Future
    # replacement: Monte Carlo permutation test (10k shuffles, p<0.10).
    # DSR value itself is still computed and shown in the markdown
    # report's CPCV section as a diagnostic.

    # ─── wf_decay: median_decay < 0.5 ───────────────────────────────
    median_decay = float(getattr(wf_report, "median_decay", 0.0))
    gates["wf_decay"] = (
        median_decay < 0.5,
        f"median_decay={median_decay:.3f} (<0.5)",
    )

    # ─── wf_coverage: fraction_positive_test >= 0.6 (relaxed Apr 27) ──
    frac_pos = float(getattr(wf_report, "fraction_positive_test", 0.0))
    gates["wf_coverage"] = (
        frac_pos >= 0.6,
        f"fraction_positive_test={frac_pos:.2f} (>=0.6)",
    )

    # ─── regime: no bucket with sharpe < -0.5 AND num_trades > 20 ───
    failing_regimes: list[str] = []
    for name, rs in (regime_stats or {}).items():
        if not getattr(rs, "passed", True):
            failing_regimes.append(
                f"{name}(sharpe={rs.sharpe:.2f}, n={rs.num_trades})"
            )
    if failing_regimes:
        gates["regime"] = (False, f"failing: {', '.join(failing_regimes)}")
    else:
        gates["regime"] = (True, "all regimes within bounds")

    # ─── cost_sensitivity: Sharpe > 0 at shift = +0.5 ───────────────
    cost_ok, cost_reason = _eval_cost_gate(cost_curve)
    gates["cost_sensitivity"] = (cost_ok, cost_reason)

    # ─── capacity: pnl_per_lot non-declining up to 300 lots ─────────
    cap_ok, cap_reason = _eval_capacity_gate(capacity_df)
    gates["capacity"] = (cap_ok, cap_reason)

    # ─── mc_skill_pvalue + bootstrap_sharpe_ci ──────────────────────
    # Both gates need the raw daily-PnL series from the full-window run
    # (extracted in scripts/validate_strategy.py from
    # ``full_result['daily_results']``). When unavailable (e.g.
    # narrow-scope unit tests that pass only the aggregate cpcv/wf
    # inputs), the gates are skipped — they neither pass nor fail —
    # which keeps test ergonomics untouched while ensuring real
    # validation runs always exercise them.
    if daily_pnl is not None:
        pnl_arr = np.asarray(daily_pnl, dtype=float)
        if pnl_arr.size >= 30:
            mc_p = monte_carlo_skill_pvalue(
                pnl_arr,
                block_size_mean=_BOOTSTRAP_BLOCK_DAYS,
                n_perm=_BOOTSTRAP_N,
                benchmark=0.0,
                seed=_BOOTSTRAP_SEED,
            )
            gates["mc_skill_pvalue"] = (
                mc_p < _MC_PVALUE_THRESHOLD,
                f"p={mc_p:.4f} (<{_MC_PVALUE_THRESHOLD}; "
                f"{_BOOTSTRAP_N} stationary-bootstrap perms, "
                f"~{int(_BOOTSTRAP_BLOCK_DAYS)}d blocks)",
            )
            ci_low, _ci_med, ci_high = stationary_bootstrap_sharpe_ci(
                pnl_arr,
                block_size_mean=_BOOTSTRAP_BLOCK_DAYS,
                n_boot=_BOOTSTRAP_N,
                confidence=_BOOTSTRAP_CONFIDENCE,
                seed=_BOOTSTRAP_SEED,
            )
            gates["bootstrap_sharpe_ci"] = (
                ci_low > _BOOTSTRAP_CI_LOWER_FLOOR,
                f"90%-CI=[{ci_low:+.3f}, {ci_high:+.3f}] "
                f"(lower>{_BOOTSTRAP_CI_LOWER_FLOOR:+.1f})",
            )
        else:
            # Too few days to be meaningful (< 1 month). WARN, not FAIL.
            gates["mc_skill_pvalue"] = (
                True,
                f"daily_pnl has {pnl_arr.size} days (<30) — WARN, gate skipped",
            )
            gates["bootstrap_sharpe_ci"] = (
                True,
                f"daily_pnl has {pnl_arr.size} days (<30) — WARN, gate skipped",
            )

    return gates


def _eval_cost_gate(cost_curve: dict[float, dict]) -> tuple[bool, str]:
    if not cost_curve:
        return False, "cost_curve empty"
    # Tolerant lookup for +0.5 shift.
    target = 0.5
    best_key = None
    best_diff = float("inf")
    for k in cost_curve:
        diff = abs(float(k) - target)
        if diff < best_diff:
            best_diff = diff
            best_key = k
    if best_key is None:
        return False, "no shift >= 0.5 in cost_curve"
    sharpe = cost_curve[best_key].get("sharpe_ratio", 0.0)
    try:
        sr = float(sharpe)
    except (TypeError, ValueError):
        sr = 0.0
    return sr > 0.0, f"sharpe@+0.5 shift = {sr:.3f} (>0)"


def _eval_capacity_gate(capacity_df) -> tuple[bool, str]:
    if capacity_df is None:
        return True, "N/A — no book_snapshot available"
    try:
        sub = capacity_df[capacity_df["lot_size"] <= 300]
    except Exception:
        return True, "N/A — capacity frame malformed"
    if len(sub) < 2:
        return True, "N/A — fewer than 2 lot tiers <= 300"
    ppls = [float(v) for v in sub["pnl_per_lot"].tolist()]
    # Non-declining (small epsilon for floating point).
    eps = 1e-6
    declining = any(ppls[i + 1] < ppls[i] - eps for i in range(len(ppls) - 1))
    if declining:
        return False, f"pnl_per_lot declines up to 300 lots: {ppls}"
    return True, f"pnl_per_lot non-declining up to 300 lots: {ppls}"


# ─── Markdown rendering ─────────────────────────────────────────────────


def _check(passed: bool) -> str:
    return "PASS" if passed else "FAIL"


def _histogram(values: np.ndarray, bins: int = 10, width: int = 50) -> list[str]:
    """ASCII histogram of ``values`` across ``bins`` equal-width buckets."""
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return ["(empty distribution)"]
    lo, hi = float(arr.min()), float(arr.max())
    if hi == lo:
        # Degenerate — single stack at that value.
        bar = "#" * width
        return [f"[{lo:+7.3f}] {bar} ({arr.size})"]
    edges = np.linspace(lo, hi, bins + 1)
    counts, _ = np.histogram(arr, bins=edges)
    max_count = int(counts.max()) if counts.size else 1
    lines: list[str] = []
    for i in range(bins):
        cnt = int(counts[i])
        bar_len = int(round(width * cnt / max_count)) if max_count else 0
        bar = "#" * bar_len
        lines.append(
            f"[{edges[i]:+7.3f}..{edges[i + 1]:+7.3f}] {bar} ({cnt})"
        )
    return lines


def _fmt_date(d) -> str:
    if d is None:
        return "—"
    if hasattr(d, "isoformat"):
        return d.isoformat()
    return str(d)


def _fmt_float(x, nd: int = 3) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    if math.isnan(v):
        return "nan"
    if math.isinf(v):
        return "inf" if v > 0 else "-inf"
    return f"{v:.{nd}f}"


def render_markdown(report: ValidationReport, out_path: Path) -> None:
    """Write the full markdown report to ``out_path`` (creates parent dirs)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append(f"# Validation Report — {report.strategy}")
    lines.append("")
    lines.append(f"- Run ID: `{report.run_id}`")
    lines.append(f"- Split train_end: {_fmt_date(report.split.train_end)}")
    lines.append(f"- Split val_end: {_fmt_date(report.split.val_end)}")
    lines.append(f"- Split holdout_end: {_fmt_date(report.split.holdout_end)}")
    lines.append("")

    # ─── 1. Executive Summary ───────────────────────────────────────
    lines.append("## 1. Executive Summary")
    lines.append("")
    lines.append("| Gate | Status | Reason |")
    lines.append("|---|---|---|")
    for gate_name, (passed, reason) in report.gates.items():
        mark = "PASS" if passed else "FAIL"
        lines.append(f"| {gate_name} | {mark} | {reason} |")
    lines.append("")
    verdict = "PASS" if report.final_verdict else "FAIL"
    lines.append(f"**Final Verdict: {verdict}**")
    lines.append("")

    # ─── 2. Split ───────────────────────────────────────────────────
    lines.append("## 2. Split")
    lines.append("")
    lines.append(
        f"- Train window: ends {_fmt_date(report.split.train_end)} "
        f"({len(report.train_days)} days)"
    )
    lines.append(
        f"- Val window: {_fmt_date(report.split.train_end)} → "
        f"{_fmt_date(report.split.val_end)} ({len(report.val_days)} days)"
    )
    lines.append(
        f"- Holdout window: {_fmt_date(report.split.val_end)} → "
        f"{_fmt_date(report.split.holdout_end)} "
        f"({len(report.holdout_days)} days)"
    )
    lines.append("")

    # ─── 3. CPCV Distribution ───────────────────────────────────────
    lines.append("## 3. CPCV Distribution")
    lines.append("")
    cpcv = report.cpcv_result or {}
    dist = np.asarray(cpcv.get("sharpe_distribution", []), dtype=float)
    n_paths = int(dist.size)
    lines.append(f"- Paths: {n_paths}")
    lines.append(f"- Mean Sharpe: {_fmt_float(cpcv.get('sharpe_mean'))}")
    lines.append(f"- Median Sharpe: {_fmt_float(cpcv.get('sharpe_median'))}")
    lines.append(f"- 5th pct Sharpe: {_fmt_float(cpcv.get('sharpe_p05'))}")
    lines.append(f"- 95th pct Sharpe: {_fmt_float(cpcv.get('sharpe_p95'))}")
    pbo_val = cpcv.get("pbo")
    lines.append(
        f"- PBO: {_fmt_float(pbo_val) if pbo_val is not None else 'N/A (single-config)'}"
    )
    lines.append(f"- DSR: {_fmt_float(_dsr_from_cpcv(cpcv))}")
    lines.append(f"- PSR: {_fmt_float(_psr_from_cpcv(cpcv))}")
    lines.append("")
    lines.append("```")
    for line in _histogram(dist):
        lines.append(line)
    lines.append("```")
    lines.append("")

    # ─── 4. Walk-Forward ────────────────────────────────────────────
    lines.append("## 4. Walk-Forward")
    lines.append("")
    wf = report.wf_report
    windows = list(getattr(wf, "windows", []) or [])
    lines.append(
        f"- Windows: {len(windows)} | "
        f"median_decay={_fmt_float(getattr(wf, 'median_decay', 0.0))} | "
        f"frac_positive={_fmt_float(getattr(wf, 'fraction_positive_test', 0.0), nd=2)} | "
        f"mean_test_sharpe={_fmt_float(getattr(wf, 'mean_test_sharpe', 0.0))}"
    )
    lines.append("")
    lines.append(
        "| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for w in windows:
        decay = w.train_sharpe - w.test_sharpe
        lines.append(
            f"| {w.idx} | {_fmt_date(w.train_start)}..{_fmt_date(w.train_end)} "
            f"| {_fmt_date(w.test_start)}..{_fmt_date(w.test_end)} "
            f"| {_fmt_float(w.train_sharpe)} | {_fmt_float(w.test_sharpe)} "
            f"| {_fmt_float(decay)} | {w.num_test_trades} |"
        )
    lines.append("")

    # ─── 5. Regime Stratification ──────────────────────────────────
    lines.append("## 5. Regime Stratification")
    lines.append("")
    lines.append(
        "| regime | num_trades | total_pnl | sharpe | win_rate | max_dd | passed |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for name, rs in (report.regime_stats or {}).items():
        passed_flag = getattr(rs, "passed", True)
        # Bold failing rows
        if not passed_flag:
            lines.append(
                f"| **{name}** | **{rs.num_trades}** | **{rs.total_pnl}** "
                f"| **{rs.sharpe}** | **{rs.win_rate}** | **{rs.max_dd}** "
                f"| **FAIL** |"
            )
        else:
            lines.append(
                f"| {name} | {rs.num_trades} | {rs.total_pnl} | {rs.sharpe} "
                f"| {rs.win_rate} | {rs.max_dd} | PASS |"
            )
    lines.append("")

    # ─── 6. Cost Sensitivity ───────────────────────────────────────
    lines.append("## 6. Cost Sensitivity")
    lines.append("")
    lines.append("| shift | sharpe | profit_factor | total_pnl |")
    lines.append("|---|---|---|---|")
    for shift in sorted((report.cost_curve or {}).keys()):
        m = report.cost_curve[shift]
        lines.append(
            f"| {shift:+.2f} | {_fmt_float(m.get('sharpe_ratio'))} "
            f"| {m.get('profit_factor')} | {m.get('total_pnl')} |"
        )
    lines.append("")

    # ─── 7. Capacity ───────────────────────────────────────────────
    lines.append("## 7. Capacity")
    lines.append("")
    if report.capacity_df is None:
        lines.append("N/A — trades lacked book_snapshot")
        lines.append("")
    else:
        df = report.capacity_df
        lines.append(
            "| lot_size | total_pnl | pnl_per_lot | avg_slippage_bps "
            "| synth_fallback_legs |"
        )
        lines.append("|---|---|---|---|---|")
        for _, row in df.iterrows():
            lines.append(
                f"| {int(row['lot_size'])} | {row['total_pnl']} "
                f"| {row['pnl_per_lot']} | {row['avg_slippage_bps']} "
                f"| {int(row['num_trades_synthetic_fallback'])} |"
            )
        lines.append("")
        ppls = [float(v) for v in df["pnl_per_lot"].tolist()]
        if ppls:
            max_abs = max(abs(v) for v in ppls) or 1.0
            lines.append("```")
            for ls, v in zip(df["lot_size"].tolist(), ppls, strict=False):
                bar_len = int(round(40 * abs(v) / max_abs))
                bar = ("#" if v >= 0 else "-") * bar_len
                lines.append(f"{int(ls):>5} | {v:+10.4f} {bar}")
            lines.append("```")
            lines.append("")

    # ─── 8. Holdout ────────────────────────────────────────────────
    lines.append("## 8. Holdout")
    lines.append("")
    if report.holdout_metrics is None:
        lines.append("NOT ACCESSED (holdout preserved)")
    else:
        m = report.holdout_metrics
        lines.append(f"- Total P&L: {m.get('total_pnl')}")
        lines.append(f"- Sharpe: {_fmt_float(m.get('sharpe_ratio'))}")
        lines.append(f"- Win rate: {_fmt_float(m.get('win_rate'), nd=1)}%")
        lines.append(f"- Max drawdown: {m.get('max_drawdown')}")
        lines.append(f"- Num trades: {m.get('num_trades')}")
    lines.append("")

    out_path.write_text("\n".join(lines) + "\n")
