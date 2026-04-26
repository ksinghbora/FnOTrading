"""Process-pool based parallel CPCV path runner.

Each CPCV path is an independent backtest of the engine over a specific
non-contiguous list of training dates. Paths share no state — they read
the same parquet corpus (memory-mapped, OS-cached) and write to disjoint
result tuples. That makes them ideal for ``ProcessPoolExecutor``: bypass
the GIL with true parallelism on the M4's 4 performance cores.

Design constraints (Apr 25 2026 audit):

1. **Workers reconstruct everything from primitives.** No closures or
   live engine instances cross the process boundary. Pickling a closure
   that captures a ``BacktestEngine`` is fragile (state, async loops,
   logger handles). The cleanest contract is a frozen ``RunnerSpec``
   dataclass with strings/ints/floats only.

2. **Each worker has its own asyncio event loop.** ``asyncio.run()``
   inside the worker creates a fresh loop. Parent's loop never crosses.

3. **Per-path RNG seed is deterministic.** ``path_seed = base_seed *
   2654435761 + path_id`` (Knuth multiplicative hash). Workers run with
   that seed; running with the same ``base_seed + path_id`` always
   produces identical metrics regardless of n_workers. The
   determinism test in
   ``tests/integration/test_parallel_runner_determinism.py`` enforces
   this contract — n_workers=1 and n_workers=N produce bit-identical
   CPCV path metrics for the same seed.

4. **Spawn start method.** macOS Python 3.14 defaults to ``spawn``,
   which fully isolates child interpreter state from the parent. We
   set it explicitly to avoid surprises if the default ever changes.
   ``spawn`` is also the only safe option on Windows; ``fork``
   inherits live file descriptors which can corrupt shared CSVs.

5. **Decisions CSV is shared but POSIX-atomic per row.** Multiple
   workers may write to the same ``decisions_YYYY-MM-DD.csv`` in
   append mode. POSIX guarantees atomicity for ``write(2)`` calls up
   to ``PIPE_BUF`` (~4 KB on macOS); a single CSV row is well under
   that. Row ordering is non-deterministic, but
   ``scripts/validate_strategy.py`` wipes the decisions dir before
   the final full-window run anyway (Bug 4 fix), so workers' decision
   output is just discarded noise.
"""
from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Knuth's multiplicative hash constant. Used to spread per-path seeds
# across the 32-bit space so neighbouring path_ids don't share low bits.
_SEED_MULTIPLIER = 2654435761


@dataclass(frozen=True)
class RunnerSpec:
    """Picklable specification for reconstructing a backtest in a worker.

    Fields are all primitives; no live objects (engines, sessions, file
    handles). The worker calls ``run_path_in_subprocess`` with this spec
    plus the path-specific args (days, params, path_id).
    """

    strategy_name: str
    parquet_dir: str
    underlying: str
    spot_token: int
    initial_capital: float
    base_seed: int


def _derive_path_seed(base_seed: int, path_id: int) -> int:
    """Deterministic per-path seed: ``base_seed * mult + path_id``.

    Modulo 2**31-1 so it fits in a numpy int32 if any consumer cares.
    """
    return (base_seed * _SEED_MULTIPLIER + path_id) % (2**31 - 1)


def run_path_in_subprocess(
    spec_dict: dict,
    train_dates: list[date],
    params: dict,
    path_id: int,
) -> tuple[int, dict]:
    """Worker entry point: run one CPCV path's training backtest.

    This function is the **module-level callable** that
    ``ProcessPoolExecutor`` ships to each child. Both args and return
    value are picklable.

    Args:
        spec_dict: ``RunnerSpec`` as a plain dict (dataclass instances
            pickle fine via ``spawn`` but a dict avoids any frozen-class
            edge cases on older Python versions).
        train_dates: The exact (possibly non-contiguous) date list this
            path should run. Engine.run honors it via Bug 2 fix.
        params: Strategy parameter overrides.
        path_id: 0-based path index. Used to derive the path's seed.

    Returns:
        ``(path_id, metrics_dict)``. The parent stitches these back
        together by ``path_id`` so the order of completion doesn't
        affect the final result.
    """
    # Disable decision-CSV writes BEFORE importing engine/strategy
    # modules. Workers race on the per-day decisions_*.csv if they all
    # try to write concurrently — header rows would duplicate, row
    # ordering would interleave across workers, and the stratifier's
    # cumcount-based ENTER/EXIT pairing (regime._pair_enter_exit) would
    # silently mis-pair across-worker rows. Setting the env var BEFORE
    # any module-level state is captured guarantees DecisionLogger sees
    # the disabled flag at every log() call.
    #
    # The validation harness already wipes the decisions dir before its
    # single full-window run (Bug 4 fix), so worker decisions were always
    # going to be discarded — this just stops them from being written in
    # the first place, eliminating the parallel-write race.
    os.environ["FNO_DISABLE_DECISIONS"] = "1"

    # Lazy imports inside the worker — keep the parent process's import
    # graph small (no engine/strategy modules until a worker is actually
    # spawned). Also matters for ``spawn``: each child re-imports.
    from src.backtest.engine import BacktestEngine
    from src.backtest.gdfl_market_source import GDFLMarketSource

    seed = _derive_path_seed(spec_dict["base_seed"], path_id)

    # Reconstruct market source per worker. Parquet files are memory-
    # mapped at the OS level so multiple workers reading the same files
    # share the page cache cleanly — no per-worker disk I/O beyond the
    # first read of each day.
    source = GDFLMarketSource(
        Path(spec_dict["parquet_dir"]),
        spec_dict["underlying"],
        spec_dict["spot_token"],
    )
    available = set(source.available_days())
    days_filtered = [d for d in train_dates if d in available]
    if not days_filtered:
        return path_id, {
            "sharpe_ratio": 0.0,
            "total_pnl": 0.0,
            "num_trades": 0,
            "_path_id": path_id,
            "_skipped": True,
        }

    engine = BacktestEngine()
    result = asyncio.run(
        engine.run(
            strategy_name=spec_dict["strategy_name"],
            strategy_params=dict(params),
            initial_capital=spec_dict["initial_capital"],
            seed=seed,
            market_source=source,
            days=days_filtered,
        )
    )
    metrics = dict(result.get("metrics", {}))
    metrics["_path_id"] = path_id
    return path_id, metrics


async def parallel_evaluate_paths(
    spec: RunnerSpec,
    paths_args: list[tuple[int, list[date], list[date], tuple[int, ...]]],
    param_set: dict[str, Any],
    n_workers: int,
) -> dict[int, tuple[list[date], list[date], tuple[int, ...], dict]]:
    """Run all CPCV paths across ``n_workers`` subprocesses.

    Args:
        spec: The frozen runner spec (paths/underlying/seed).
        paths_args: List of ``(path_id, train_dates, test_dates, fold_ids)``
            tuples produced by ``CombinatorialPurgedCV.split``.
        param_set: Strategy parameter overrides (same for all paths).
        n_workers: Number of subprocesses. ``1`` falls through to the
            sequential path so the same code path covers both modes.

    Returns:
        ``{path_id: (train_dates, test_dates, fold_ids, metrics)}``. The
        caller iterates by sorted path_id to produce a deterministic
        ordering regardless of completion order.
    """
    if not paths_args:
        return {}

    spec_dict = {
        "strategy_name": spec.strategy_name,
        "parquet_dir": spec.parquet_dir,
        "underlying": spec.underlying,
        "spot_token": spec.spot_token,
        "initial_capital": spec.initial_capital,
        "base_seed": spec.base_seed,
    }

    # ``spawn`` is the macOS / Windows default, but be explicit so
    # behaviour doesn't change if the parent process inherits a
    # different start method (e.g. ``forkserver``).
    ctx = multiprocessing.get_context("spawn")

    out: dict[int, tuple[list[date], list[date], tuple[int, ...], dict]] = {}

    # Sequential fallback: keep the same return-shape so callers don't
    # branch. Useful for debugging when n_workers=1.
    if n_workers <= 1:
        for path_id, train_dates, test_dates, fold_ids in paths_args:
            _, metrics = run_path_in_subprocess(
                spec_dict, train_dates, dict(param_set), path_id
            )
            out[path_id] = (train_dates, test_dates, fold_ids, metrics)
        return out

    logger.info(
        "[CPCV] running %d paths across %d workers (spawn) — base_seed=%d",
        len(paths_args), n_workers, spec.base_seed,
    )

    loop = asyncio.get_event_loop()
    with ProcessPoolExecutor(max_workers=n_workers, mp_context=ctx) as pool:
        futures = {}
        for path_id, train_dates, test_dates, fold_ids in paths_args:
            fut = pool.submit(
                run_path_in_subprocess,
                spec_dict, train_dates, dict(param_set), path_id,
            )
            futures[fut] = (path_id, train_dates, test_dates, fold_ids)

        completed = 0
        for fut in as_completed(futures):
            completed += 1
            path_id_meta, train_dates, test_dates, fold_ids = futures[fut]
            try:
                _path_id, metrics = fut.result()
            except Exception as exc:
                logger.error(
                    "[CPCV] path_id=%d failed in worker: %s — recording empty metrics",
                    path_id_meta, exc,
                )
                metrics = {
                    "sharpe_ratio": 0.0, "total_pnl": 0.0, "num_trades": 0,
                    "_path_id": path_id_meta, "_failed": True, "_error": str(exc),
                }
            out[path_id_meta] = (train_dates, test_dates, fold_ids, metrics)
            if completed % max(1, len(paths_args) // 10) == 0:
                logger.info(
                    "[CPCV] %d/%d paths complete", completed, len(paths_args),
                )

    return out
