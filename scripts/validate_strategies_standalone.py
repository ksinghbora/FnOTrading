"""Per-strategy standalone CPCV validation harness.

Phase 3a-revised (Apr 26 2026): the wide_baseline ran the COMBINED
``portfolio_bt`` strategy. Its per-mode breakdown shows what happens
*inside* the combined strategy, NOT what each strategy would do
standalone. Live paper trading shows materially different per-trade
gross than backtest, suggesting combined-strategy decision logic
suppresses entries that the standalone strategies would take.

This script runs each strategy STANDALONE on the same train+val
window with the audit-clean harness. Holdout (Aug 2025 → Feb 2026)
is reserved.

Usage::

    uv run python scripts/validate_strategies_standalone.py
    uv run python scripts/validate_strategies_standalone.py --strategies short_strangle,iron_condor

Output:
- ``reports/standalone_v1/{strategy}_validation.md`` per strategy
- ``reports/standalone_v1/SUMMARY.md`` cross-strategy verdict matrix

Discipline:
- Each run uses the SAME --workers, --cpcv-folds, --cpcv-max-paths
- Holdout window NOT touched (the harness's SplitLoader enforces this)
- No parameter tuning between runs — strategies use their default
  params from ``src/strategy/params.py``
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

# Default strategies to validate. Order matters for the summary table.
DEFAULT_STRATEGIES = [
    "short_strangle",
    "iron_condor",
    "short_straddle",
]

# Splits — locked here so all strategies use the same windows.
TRAIN_END = "2025-05-30"
VAL_END = "2025-07-31"
HOLDOUT_END = "2026-02-27"

# CPCV config — locked. Reviewer-corrected PBO threshold (0.5) applies
# inside validate_strategy.py.
CPCV_FOLDS = 10
CPCV_N_TEST_FOLDS = 2
CPCV_MAX_PATHS = 50

# WF config — fits the 227-day combined train+val window
WF_TRAIN_DAYS = 90
WF_TEST_DAYS = 30
WF_STEP_DAYS = 15

REPORT_DIR = Path("reports/standalone_v1")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--strategies", default=",".join(DEFAULT_STRATEGIES),
        help="Comma-separated list of strategies to validate",
    )
    p.add_argument(
        "--workers", type=int, default=4,
        help="Parallel CPCV workers per strategy (default 4 for M4 perf cores)",
    )
    p.add_argument(
        "--parquet-dir", default="data/gdfl_snapshots",
        help="GDFL parquet directory",
    )
    p.add_argument(
        "--smoke", action="store_true",
        help="Smoke mode: shrink CPCV (5 paths) for quick sanity check",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print commands without executing",
    )
    return p.parse_args()


def _run_strategy_validation(
    strategy: str,
    args: argparse.Namespace,
) -> tuple[str, int, float]:
    """Run validate_strategy.py for one strategy. Returns (strategy, rc, secs)."""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORT_DIR / f"{strategy}_validation.md"

    cmd = [
        "uv", "run", "python", "scripts/validate_strategy.py",
        "--strategy", strategy,
        "--train-end", TRAIN_END,
        "--val-end", VAL_END,
        "--holdout-end", HOLDOUT_END,
        "--parquet-dir", args.parquet_dir,
        "--underlying", "NIFTY",
        "--workers", str(args.workers),
        "--out", str(out_path),
        "--log-level", "WARNING",
    ]

    # Smoke mode shrinks CPCV
    if args.smoke:
        cmd += ["--cpcv-max-paths", "5", "--cpcv-folds", "5"]
    else:
        cmd += [
            "--cpcv-folds", str(CPCV_FOLDS),
            "--cpcv-n-test-folds", str(CPCV_N_TEST_FOLDS),
            "--cpcv-max-paths", str(CPCV_MAX_PATHS),
            "--wf-train-days", str(WF_TRAIN_DAYS),
            "--wf-test-days", str(WF_TEST_DAYS),
            "--wf-step-days", str(WF_STEP_DAYS),
        ]

    print(f"\n{'=' * 70}")
    print(f"Validating: {strategy}")
    print(f"{'=' * 70}")
    print(f"Command: {' '.join(cmd)}")

    if args.dry_run:
        return strategy, 0, 0.0

    t0 = time.time()
    log_path = REPORT_DIR / f"{strategy}_run.log"
    with log_path.open("w") as logfh:
        proc = subprocess.run(cmd, stdout=logfh, stderr=subprocess.STDOUT)
    elapsed = time.time() - t0

    print(f"  → exit {proc.returncode}, {elapsed/60:.1f} min")
    print(f"  → report: {out_path}")
    print(f"  → log:    {log_path}")

    return strategy, proc.returncode, elapsed


def _extract_verdict(report_path: Path) -> dict:
    """Pull headline metrics from a validate_strategy.py report."""
    if not report_path.exists():
        return {"verdict": "MISSING", "reason": "report not produced"}
    text = report_path.read_text()
    out = {"verdict": "?"}
    # Find executive summary table
    for line in text.splitlines():
        if line.startswith("**Final Verdict:"):
            verdict = line.split("**Final Verdict:")[-1].strip().rstrip("*").strip()
            out["verdict"] = verdict.split()[0] if verdict else "?"
        if "Median Sharpe:" in line:
            out["median_sharpe"] = line.split("Median Sharpe:")[-1].strip()
        if "5th pct Sharpe:" in line:
            out["p05_sharpe"] = line.split("5th pct Sharpe:")[-1].strip()
        if line.startswith("- Paths:"):
            out["cpcv_paths"] = line.split(":")[-1].strip()
    # Per-gate status — light parse from the executive summary
    gates = []
    in_gates = False
    for line in text.splitlines():
        if line.startswith("| Gate"):
            in_gates = True
            continue
        if in_gates and line.startswith("| ---"):
            continue
        if in_gates and line.startswith("|"):
            cols = [c.strip() for c in line.split("|")[1:-1]]
            if len(cols) >= 2:
                gates.append((cols[0], cols[1]))
        elif in_gates and not line.startswith("|"):
            in_gates = False
    out["gates"] = gates
    return out


def _write_summary(strategies: list[str]) -> None:
    """Cross-strategy comparison report."""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = REPORT_DIR / "SUMMARY.md"

    lines = [
        "# Standalone Strategy Validation — Summary",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Window: train_end={TRAIN_END}, val_end={VAL_END}, holdout_end={HOLDOUT_END}",
        f"Holdout: {HOLDOUT_END} reserved (single-access, NOT used here).",
        "",
        "## Verdict matrix",
        "",
        "| Strategy | Verdict | Median Sharpe | p05 Sharpe | CPCV paths |",
        "|---|---|---|---|---|",
    ]
    for s in strategies:
        v = _extract_verdict(REPORT_DIR / f"{s}_validation.md")
        lines.append(
            f"| {s} | {v.get('verdict', '?')} | "
            f"{v.get('median_sharpe', '?')} | "
            f"{v.get('p05_sharpe', '?')} | "
            f"{v.get('cpcv_paths', '?')} |"
        )

    lines.append("")
    lines.append("## Per-strategy gate status")
    lines.append("")
    for s in strategies:
        v = _extract_verdict(REPORT_DIR / f"{s}_validation.md")
        lines.append(f"### {s}")
        lines.append("")
        if "gates" in v and v["gates"]:
            lines.append("| Gate | Status |")
            lines.append("|---|---|")
            for g, st in v["gates"]:
                lines.append(f"| {g} | {st} |")
        else:
            lines.append("(no gates parsed)")
        lines.append("")

    lines.append("## Files")
    lines.append("")
    for s in strategies:
        lines.append(f"- [{s} full report](./{s}_validation.md)")
        lines.append(f"- [{s} run log](./{s}_run.log)")
    lines.append("")

    summary_path.write_text("\n".join(lines))
    print(f"\n✓ Summary written: {summary_path}")


def main() -> None:
    args = parse_args()
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]

    print(f"Standalone validation plan:")
    print(f"  Strategies:    {strategies}")
    print(f"  Train end:     {TRAIN_END}")
    print(f"  Val end:       {VAL_END}")
    print(f"  Holdout end:   {HOLDOUT_END} (reserved)")
    print(f"  CPCV paths:    {5 if args.smoke else CPCV_MAX_PATHS}")
    print(f"  Workers each:  {args.workers}")
    print(f"  Mode:          {'SMOKE' if args.smoke else 'FULL'}")
    print()

    results = []
    for strategy in strategies:
        result = _run_strategy_validation(strategy, args)
        results.append(result)

    print()
    print("=" * 70)
    print("All runs complete")
    print("=" * 70)
    for s, rc, secs in results:
        status = "OK" if rc == 0 else f"FAIL exit={rc}"
        print(f"  {s:25s}: {status}, {secs/60:.1f} min")

    if not args.dry_run:
        _write_summary(strategies)


if __name__ == "__main__":
    main()
