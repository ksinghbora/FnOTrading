"""Run the full statistical validation stack on a strategy.

End-to-end driver:
  1. Three-way split on GDFL availability.
  2. CPCV + walk-forward on train ∪ val.
  3. Regime stratification on decisions CSVs for train ∪ val.
  4. Cost sensitivity + capacity on the trade set collected during a
     single full-window backtest (needed because CPCV paths aggregate
     metrics only, not trade rosters).
  5. Holdout once — only when ``--burn-holdout`` is passed.
  6. Gate evaluation + markdown report.

Exits 0 on PASS verdict, 1 on FAIL. Error paths (missing data, etc.)
exit with code 2 so CI can distinguish "strategy failed gates" from
"harness couldn't run at all".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtest.engine import BacktestEngine
from src.backtest.gdfl_market_source import GDFLMarketSource
from src.backtest.validation import (
    CapacityUnavailable,
    CombinatorialPurgedCV,
    SplitLoader,
    StrategySplit,
    WalkForwardValidator,
    load_event_dates,
    sensitivity_curve,
    simulate_capacity,
    stratify,
)
from src.backtest.validation.report import (
    ValidationReport,
    evaluate_gates,
    render_markdown,
)
from src.market_data.simulator import BANKNIFTY_SPOT_TOKEN, NIFTY_SPOT_TOKEN

logger = logging.getLogger("validate_strategy")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--strategy", required=True)
    p.add_argument(
        "--train-start", default="",
        help="Optional ISO date (YYYY-MM-DD) to clip the train window from "
             "below — useful for smoke tests. Defaults to earliest available.",
    )
    p.add_argument("--train-end", required=True, help="ISO date (YYYY-MM-DD)")
    p.add_argument("--val-end", required=True)
    p.add_argument("--holdout-end", required=True)
    p.add_argument("--parquet-dir", required=True)
    p.add_argument("--underlying", default="NIFTY")
    p.add_argument("--spot-token", type=int, default=NIFTY_SPOT_TOKEN)
    p.add_argument("--cpcv-folds", type=int, default=10)
    p.add_argument("--cpcv-n-test-folds", type=int, default=2)
    p.add_argument("--cpcv-max-paths", type=int, default=50)
    p.add_argument(
        "--oos-cpcv", action="store_true",
        help=(
            "Apr 30 2026 Phase 3 honest-rename: when set, the CPCV "
            "evaluator runs each path on test_dates (proper OOS). "
            "Default is train_in_sample — runs on train_dates and "
            "produces an in-sample fold-stability distribution. The "
            "report's `fold_stability_*` gates are calibrated against "
            "the default mode; OOS Sharpe distributions are typically "
            "lower (real OOS variance > resampled-train variance) so "
            "expect to recalibrate gates when flipping this on."
        ),
    )
    p.add_argument(
        "--workers", type=int, default=1,
        help=(
            "Number of parallel CPCV worker subprocesses. 1 = sequential "
            "(default; reproduces legacy behaviour). 4 = saturate the M4's "
            "performance cores. Workers each spawn a fresh Python interpreter, "
            "reconstruct the engine + market source from primitive args, and "
            "run with FNO_DISABLE_DECISIONS=1 to avoid CSV write races. "
            "Determinism is enforced by tests/integration/test_parallel_cpcv_determinism.py "
            "— same seed produces bit-identical CPCV stats regardless of worker count."
        ),
    )
    p.add_argument("--wf-train-days", type=int, default=90)
    p.add_argument("--wf-test-days", type=int, default=30)
    p.add_argument("--wf-step-days", type=int, default=15)
    p.add_argument(
        "--wf-workers", type=int, default=1,
        help=(
            "Number of parallel walk-forward worker subprocesses. 1 = sequential "
            "(default; reproduces legacy behaviour). > 1 dispatches each window's "
            "train+test runs to a ProcessPoolExecutor. Each worker reconstructs "
            "the engine + market source from primitive args and runs with "
            "FNO_DISABLE_DECISIONS=1 to avoid CSV write races. "
            "Determinism: per-window seed = base_seed * 2654435761 + window_idx; "
            "same seed → bit-identical WF metrics regardless of worker count. "
            "Requires parquet data (--parquet-dir) — falls back to sequential "
            "if runner_spec cannot be built."
        ),
    )
    p.add_argument(
        "--shifts", default="-1,-0.5,-0.25,0,0.25,0.5,1.0",
        help="Comma-separated spread shift multiples",
    )
    p.add_argument(
        "--capacity-lots", default="75,150,300,750,1500",
        help="Comma-separated lot sizes for capacity curve",
    )
    p.add_argument("--burn-holdout", action="store_true")
    p.add_argument(
        "--out", default="",
        help="Output markdown path; default reports/validation/{strategy}_{ts}.md",
    )
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--baseline-params-json", default="")
    p.add_argument("--log-level", default="INFO")
    p.add_argument(
        "--initial-capital", type=float, default=1_000_000.0,
        help="Starting capital for every backtest",
    )
    return p.parse_args()


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def load_baseline_params(path: str) -> dict:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"--baseline-params-json {p} does not exist")
    with p.open() as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"--baseline-params-json {p} must contain a JSON object")
    return data


def build_runner(
    engine: BacktestEngine,
    strategy_name: str,
    parquet_dir: str,
    underlying: str,
    spot_token: int,
    initial_capital: float,
    seed: int,
):
    """Build an async ``runner_fn(days, params) -> result_dict`` closure.

    Creates a fresh ``GDFLMarketSource`` per call so parquet caches don't
    bleed between windows (the source holds a single day in memory).
    """

    async def runner(days: list[date], params: dict) -> dict:
        if not days:
            return {
                "metrics": {
                    "sharpe_ratio": 0.0,
                    "total_pnl": 0.0,
                    "num_trades": 0,
                },
                "trades": [],
                "daily_results": [],
            }
        source = GDFLMarketSource(parquet_dir, underlying, spot_token)
        available = set(source.available_days())
        days_filtered = [d for d in days if d in available]
        if not days_filtered:
            return {
                "metrics": {
                    "sharpe_ratio": 0.0,
                    "total_pnl": 0.0,
                    "num_trades": 0,
                },
                "trades": [],
                "daily_results": [],
            }
        # Apr 25 2026: pass the EXPLICIT day list (not start+num_days).
        # CPCV produces non-contiguous train indices like [0,1,5,6,7,...]
        # with gaps for held-out test folds. Without an explicit list, the
        # engine would silently expand to a contiguous slice and leak test
        # days into train. The ``days`` parameter is the audit-clean path.
        result = await engine.run(
            strategy_name=strategy_name,
            strategy_params=dict(params),
            initial_capital=initial_capital,
            seed=seed,
            market_source=source,
            days=days_filtered,
        )
        return result

    return runner


async def main_async(args: argparse.Namespace) -> int:
    setup_logging(args.log_level)

    split = StrategySplit(
        train_end=date.fromisoformat(args.train_end),
        val_end=date.fromisoformat(args.val_end),
        holdout_end=date.fromisoformat(args.holdout_end),
    )

    parquet_dir = Path(args.parquet_dir)
    if not parquet_dir.exists():
        logger.error("parquet-dir %s does not exist", parquet_dir)
        return 2

    # SplitLoader uses a GDFLMarketSource only for day availability — fine
    # to instantiate a throwaway one. The engine-side source is recreated
    # per call inside the runner closure.
    gdfl_for_loader = GDFLMarketSource(parquet_dir, args.underlying, args.spot_token)
    loader = SplitLoader(
        split=split,
        strategy=args.strategy,
        gdfl_source=gdfl_for_loader,
    )

    train_days = loader.train_days()
    if args.train_start:
        ts = date.fromisoformat(args.train_start)
        train_days = [d for d in train_days if d >= ts]
    val_days = loader.val_days()
    combined = train_days + val_days
    if not combined:
        logger.error(
            "No GDFL data for window train_end=%s val_end=%s — aborting",
            split.train_end, split.val_end,
        )
        return 2

    logger.info(
        "[VALIDATE] strategy=%s train=%d val=%d combined=%d",
        args.strategy, len(train_days), len(val_days), len(combined),
    )

    baseline_params = load_baseline_params(args.baseline_params_json)

    engine = BacktestEngine()
    runner = build_runner(
        engine=engine,
        strategy_name=args.strategy,
        parquet_dir=str(parquet_dir),
        underlying=args.underlying,
        spot_token=args.spot_token,
        initial_capital=args.initial_capital,
        seed=args.seed,
    )

    # ─── CPCV on train ∪ val ───────────────────────────────────────
    cpcv = CombinatorialPurgedCV(
        n_folds=args.cpcv_folds,
        n_test_folds=args.cpcv_n_test_folds,
        max_paths=args.cpcv_max_paths,
        seed=args.seed,
    )
    logger.info(
        "[VALIDATE] Running CPCV on %d days (workers=%d)",
        len(combined), args.workers,
    )
    # Build a RunnerSpec for parallel mode. Sequential mode (workers=1)
    # ignores the spec and uses the closure ``runner`` defined above.
    from src.backtest.validation.parallel_runner import RunnerSpec
    runner_spec = RunnerSpec(
        strategy_name=args.strategy,
        parquet_dir=str(parquet_dir),
        underlying=args.underlying,
        spot_token=args.spot_token,
        initial_capital=args.initial_capital,
        base_seed=args.seed,
    )
    cpcv_result = await cpcv.evaluate(
        baseline_params, runner, combined,
        runner_spec=runner_spec,
        n_workers=args.workers,
        evaluation_mode="test_oos" if args.oos_cpcv else "train_in_sample",
    )

    # ─── Walk-forward on train ∪ val ───────────────────────────────
    wf = WalkForwardValidator(
        train_window_days=args.wf_train_days,
        test_window_days=args.wf_test_days,
        step_days=args.wf_step_days,
    )
    logger.info(
        "[VALIDATE] Running walk-forward on %d days (wf-workers=%d)",
        len(combined), args.wf_workers,
    )
    wf_report = await wf.run(
        combined, runner, baseline_params, optimizer_fn=None,
        workers=args.wf_workers,
        runner_spec=runner_spec if args.wf_workers > 1 else None,
    )

    # ─── Wipe decisions for the validation window ──────────────────
    # Apr 25 2026 audit Bug 4: CPCV + WF paths above each ran the
    # engine which APPENDED decision rows to the per-day CSVs. Without
    # wiping, the stratifier would see ~40× duplicated decisions (each
    # day appears in many CPCV paths × WF windows × historical runs).
    # Past validation reports had inflated trade counts for this reason.
    # The full-window run below is the SINGLE source of truth for
    # stratification — wipe everything else first.
    decisions_dir = Path("data/decisions")
    if decisions_dir.exists():
        wiped = 0
        for d in combined:
            path = decisions_dir / f"decisions_{d.isoformat()}.csv"
            if path.exists():
                path.unlink()
                wiped += 1
        if wiped:
            logger.info(
                "[VALIDATE] wiped %d decisions CSVs for the %d-day validation window "
                "(prevents Bug 4 duplication: stratifier reads only the full-window run's decisions)",
                wiped, len(combined),
            )

    # ─── Full-window run to collect trades for cost + capacity ─────
    logger.info(
        "[VALIDATE] Running full-window backtest on %d days to collect trades + decisions",
        len(combined),
    )
    full_result = await runner(combined, baseline_params)
    trades = list(full_result.get("trades", []))
    # Daily-PnL series for the MC permutation + block-bootstrap-CI gates
    # added Apr 27 2026 alongside the DSR drop. The engine emits one row
    # per session in ``daily_results``; we read the ``pnl`` field which
    # is the end-of-day P&L net of charges (₹).
    daily_pnl_series = [
        float(d.get("pnl", 0.0))
        for d in full_result.get("daily_results", [])
    ]
    logger.info(
        "[VALIDATE] collected %d trades, %d daily-PnL points",
        len(trades),
        len(daily_pnl_series),
    )

    # ─── Regime stratification (must run AFTER full-window write) ──
    decisions = loader.load_decisions(combined)
    event_dates = load_event_dates()
    regime_stats = stratify(decisions, event_dates=event_dates)

    # ─── Cost sensitivity ──────────────────────────────────────────
    shifts = tuple(float(x) for x in args.shifts.split(",") if x.strip())
    cost_curve = sensitivity_curve(
        trades, shifts=shifts, initial_capital=args.initial_capital
    )

    # ─── Capacity (optional) ───────────────────────────────────────
    lot_sizes = tuple(int(x) for x in args.capacity_lots.split(",") if x.strip())
    capacity_df = None
    try:
        capacity_df = simulate_capacity(trades, lot_sizes=lot_sizes)
    except CapacityUnavailable as exc:
        logger.warning("[VALIDATE] Capacity analysis skipped: %s", exc)

    # ─── Holdout (only if --burn-holdout) ──────────────────────────
    holdout_metrics = None
    if args.burn_holdout:
        logger.warning("[VALIDATE] BURNING HOLDOUT — this access is counted")
        holdout_days = loader.holdout_days(allow_burn=True)
        if holdout_days:
            holdout_result = await runner(holdout_days, baseline_params)
            holdout_metrics = dict(holdout_result.get("metrics", {}))
        else:
            logger.warning("[VALIDATE] Holdout requested but no days available")

    # ─── Build report + evaluate gates ─────────────────────────────
    gates = evaluate_gates(
        cpcv_result=cpcv_result,
        wf_report=wf_report,
        regime_stats=regime_stats,
        cost_curve=cost_curve,
        capacity_df=capacity_df,
        daily_pnl=daily_pnl_series,
    )
    final_verdict = all(passed for passed, _ in gates.values())

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    run_id = f"{args.strategy}_{ts}"
    out_path = (
        Path(args.out) if args.out
        else Path(f"reports/validation/{run_id}.md")
    )

    holdout_days_view: list[date] = []
    if args.burn_holdout:
        # We've already consumed it — avoid double-counting by reading
        # the raw window from the split object.
        holdout_days_view = loader._bdate_range(
            loader._day_after(split.val_end), split.holdout_end,
        )

    report = ValidationReport(
        strategy=args.strategy,
        run_id=run_id,
        split=split,
        cpcv_result=cpcv_result,
        wf_report=wf_report,
        regime_stats=regime_stats,
        cost_curve=cost_curve,
        capacity_df=capacity_df,
        holdout_metrics=holdout_metrics,
        gates=gates,
        final_verdict=final_verdict,
        train_days=train_days,
        val_days=val_days,
        holdout_days=holdout_days_view,
    )

    render_markdown(report, out_path)
    print(f"Report written to: {out_path.resolve()}")
    print(f"Final Verdict: {'PASS' if final_verdict else 'FAIL'}")
    return 0 if final_verdict else 1


def main() -> int:
    args = parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
