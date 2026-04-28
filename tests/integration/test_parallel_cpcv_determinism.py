"""Parallel CPCV determinism contract.

The Apr 25 2026 audit (independent reviewer) flagged that without an
explicit determinism test, we have no guarantee that ``n_workers=1`` and
``n_workers=N`` produce identical CPCV statistics. This test enforces
the contract: same seed → bit-identical path metrics regardless of how
many subprocesses ran them.

Why this matters:
- The cpcv_median_sharpe gate (and pre-Apr 27 the DSR / cpcv_stability
  gates) depend on the Sharpe distribution. A worker count that quietly
  shifts the distribution would silently change verdicts.
- Running in parallel must be a pure speedup, never a behaviour change.
- Per-path RNG seed = ``base_seed * 2654435761 + path_id`` (Knuth
  multiplicative hash). Workers derive their seed from the path id
  alone; no cross-worker state.
- ``FNO_DISABLE_DECISIONS=1`` is set in the worker so concurrent CSV
  writes don't race.

Skipped gracefully when the GDFL parquet corpus is absent (fresh
checkouts, CI without the recorded data).
"""
from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

GDFL_DIR = Path("data/gdfl_snapshots")


def _has_gdfl_data() -> bool:
    if not GDFL_DIR.exists():
        return False
    return any(GDFL_DIR.glob("gdfl_nifty_*.parquet"))


pytestmark = pytest.mark.skipif(
    not _has_gdfl_data(),
    reason="GDFL parquet corpus missing — populate data/gdfl_snapshots/ to run.",
)


@pytest.mark.slow
def test_parallel_matches_sequential_cpcv_metrics():
    """Run a small CPCV with n_workers=1 and n_workers=2; metrics must match.

    Uses 12 days × 4 folds × 2 test-folds × max_paths=3 = ~3 paths total.
    Small enough to finish in a few minutes; large enough to actually
    exercise the parallel codepath (n_workers=2 means at least one path
    runs in a subprocess).
    """
    from src.backtest.gdfl_market_source import GDFLMarketSource
    from src.backtest.validation.cpcv import CombinatorialPurgedCV
    from src.backtest.validation.parallel_runner import RunnerSpec
    from src.market_data.simulator import NIFTY_SPOT_TOKEN
    from scripts.validate_strategy import build_runner
    from src.backtest.engine import BacktestEngine

    source = GDFLMarketSource(GDFL_DIR, "NIFTY", NIFTY_SPOT_TOKEN)
    avail = source.available_days()
    if len(avail) < 12:
        pytest.skip(f"need at least 12 GDFL days, got {len(avail)}")
    days = avail[:12]

    spec = RunnerSpec(
        strategy_name="portfolio",
        parquet_dir=str(GDFL_DIR),
        underlying="NIFTY",
        spot_token=NIFTY_SPOT_TOKEN,
        initial_capital=1_000_000.0,
        base_seed=42,
    )

    cpcv = CombinatorialPurgedCV(
        n_folds=4, n_test_folds=2, max_paths=3, seed=42, embargo_pct=0.05,
    )
    params = {"underlying": "NIFTY", "quantity_lots": 1}

    # Sequential reference. ``runner`` is a closure used only when
    # ``runner_spec is None``; we pass it for the n_workers=1 path
    # (which falls through to the sequential branch even when a spec is
    # provided IF n_workers <= 1 — see cpcv.evaluate).
    engine = BacktestEngine()
    runner = build_runner(
        engine=engine, strategy_name="portfolio",
        parquet_dir=str(GDFL_DIR), underlying="NIFTY",
        spot_token=NIFTY_SPOT_TOKEN, initial_capital=1_000_000.0, seed=42,
    )

    serial = asyncio.run(
        cpcv.evaluate(
            params, runner, days,
            runner_spec=spec, n_workers=1,
        )
    )

    # Parallel run: n_workers=2 dispatches paths to subprocesses.
    parallel = asyncio.run(
        cpcv.evaluate(
            params, runner, days,
            runner_spec=spec, n_workers=2,
        )
    )

    assert len(serial["paths"]) == len(parallel["paths"]), (
        f"path count differs: serial={len(serial['paths'])} parallel={len(parallel['paths'])}"
    )
    assert len(serial["paths"]) >= 1, "test needs at least 1 path"

    # Per-path metric equality. Compare by path_id since parallel
    # completion order is non-deterministic but we sort by path_id in
    # the aggregator.
    for sp, pp in zip(serial["paths"], parallel["paths"]):
        assert sp.path_id == pp.path_id
        assert sp.test_fold_ids == pp.test_fold_ids
        # Sharpe and total_pnl: floating-point comparison with tight
        # tolerance. Bit-identical is the goal (deterministic engine +
        # same per-path seed) but allow ~1e-9 for any IEEE rounding.
        s_sharpe = float(sp.metrics.get("sharpe_ratio", 0.0))
        p_sharpe = float(pp.metrics.get("sharpe_ratio", 0.0))
        s_pnl = float(sp.metrics.get("total_pnl", 0.0))
        p_pnl = float(pp.metrics.get("total_pnl", 0.0))
        s_n = int(sp.metrics.get("num_trades", 0))
        p_n = int(pp.metrics.get("num_trades", 0))

        assert s_n == p_n, (
            f"path_id={sp.path_id}: num_trades differs serial={s_n} parallel={p_n}"
        )
        assert s_sharpe == pytest.approx(p_sharpe, abs=1e-9), (
            f"path_id={sp.path_id}: sharpe differs "
            f"serial={s_sharpe} parallel={p_sharpe}"
        )
        assert s_pnl == pytest.approx(p_pnl, abs=1e-6), (
            f"path_id={sp.path_id}: total_pnl differs "
            f"serial={s_pnl} parallel={p_pnl}"
        )

    # Aggregate stats must also match.
    assert serial["sharpe_median"] == pytest.approx(
        parallel["sharpe_median"], abs=1e-9
    )
    assert serial["sharpe_mean"] == pytest.approx(
        parallel["sharpe_mean"], abs=1e-9
    )
