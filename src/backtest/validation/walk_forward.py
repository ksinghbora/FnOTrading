"""Rolling walk-forward validation with optional per-window re-optimization.

The canonical generalization test: train on a fixed-length rolling
window, evaluate on the immediately-following test window, step forward,
repeat. The decay from train Sharpe to test Sharpe is the single most
honest estimator of how a parameter set will age out-of-sample.

Apr 23 methodology review (``memory/expert_review_apr23.md``) named this
as the primary rollout gate: no set of parameters ships until median
decay < 0.5 and the fraction of test windows with positive Sharpe is
>= 0.7. Those thresholds are encoded in :attr:`WFReport.passed`.

Optimizer hook
--------------
``optimizer_fn`` lets callers re-tune parameters on each window's train
slice before the test runs. The hook's signature is::

    def optimizer_fn(train_days: list[date], baseline_params: dict,
                    validator: WalkForwardValidator) -> dict:
        ...  # returns the params to use for this window

If the hook is omitted, every window uses a copy of ``baseline_params``
unchanged — useful as a baseline to verify the harness plumbing itself
before wiring optimization in.

Windowing semantics
-------------------
Dates are treated as an already-filtered business-day list (callers
typically source them from :class:`SplitLoader.train_and_val_days()`).
Windows step by integer index, not by calendar days, to keep spacing
uniform when the universe has holiday gaps.

Given ``train_window_days=W``, ``test_window_days=T``, ``step_days=S``,
``embargo_days=E``:

* Window ``i`` trains on indices ``[i*S, i*S + W)``
* Window ``i`` tests  on indices ``[i*S + W + E, i*S + W + E + T)``
* Stop when ``i*S + W + E + T`` would exceed ``len(dates)``.

Parallel walk-forward (workers > 1)
-------------------------------------
When ``workers > 1`` is passed to :meth:`run`, each window's train and
test runs are dispatched to a ``ProcessPoolExecutor`` using the same
spawn-based primitives-only pattern as ``parallel_runner.py``.

Determinism: the per-window seed is derived as
``(base_seed * _SEED_MULTIPLIER + window_idx) % (2**31-1)`` — identical
to the CPCV path-seed derivation. Running with the same base seed always
produces bit-identical window metrics regardless of worker count.

``FNO_DISABLE_DECISIONS=1`` is set in each worker to prevent concurrent
decision-CSV writes from racing (same fix as CPCV workers).
"""

from __future__ import annotations

import asyncio
import logging
import math
import multiprocessing
import os
from collections.abc import Awaitable, Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Knuth multiplicative hash — same constant used in parallel_runner.py
# so seeds are spread consistently across the 32-bit space.
_SEED_MULTIPLIER = 2654435761


def _derive_window_seed(base_seed: int, window_idx: int) -> int:
    """Deterministic per-window seed: ``(base_seed * mult + window_idx) % (2**31-1)``.

    Mirrors ``_derive_path_seed`` in ``parallel_runner.py``. Using the same
    scheme keeps the seed space uniform: neighbouring window indices don't
    share low bits, and the result fits in a numpy int32 if any consumer cares.
    """
    return (base_seed * _SEED_MULTIPLIER + window_idx) % (2**31 - 1)


@dataclass
class WFWindow:
    """One walk-forward window result."""

    idx: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    params: dict[str, Any]
    train_sharpe: float
    test_sharpe: float
    train_pnl: float
    test_pnl: float
    num_test_trades: int
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WFReport:
    """Aggregate walk-forward report."""

    windows: list[WFWindow]
    median_decay: float
    fraction_positive_test: float
    passed: bool
    mean_test_sharpe: float


# ─── Parallel worker function (module-level for picklability) ─────────


def _run_wf_window_in_subprocess(
    spec_dict: dict,
    train_dates: list[date],
    test_dates: list[date],
    params: dict,
    window_idx: int,
) -> tuple[int, dict, dict]:
    """Worker entry point: run one WF window's train + test backtests.

    Must be a module-level function so ``ProcessPoolExecutor`` can pickle
    it under the ``spawn`` start method. Args and return value are all
    picklable primitives/dicts.

    Args:
        spec_dict: RunnerSpec fields as a plain dict.
        train_dates: Business-day list for the training slice.
        test_dates: Business-day list for the testing slice.
        params: Strategy parameter overrides.
        window_idx: 0-based window index; used to derive the per-window seed.

    Returns:
        ``(window_idx, train_metrics_dict, test_metrics_dict)``.
    """
    # Disable decision-CSV writes before any engine/strategy imports.
    # Parallel WF workers share the same decisions/ directory and would
    # race on per-day CSV appends — same issue as CPCV workers (Bug 4).
    os.environ["FNO_DISABLE_DECISIONS"] = "1"

    # Lazy imports inside worker — keeps parent process import graph small,
    # and is required for ``spawn`` since each child re-imports from scratch.
    from src.backtest.engine import BacktestEngine
    from src.backtest.gdfl_market_source import GDFLMarketSource

    seed = _derive_window_seed(spec_dict["base_seed"], window_idx)

    source = GDFLMarketSource(
        Path(spec_dict["parquet_dir"]),
        spec_dict["underlying"],
        spec_dict["spot_token"],
    )
    available = set(source.available_days())

    def _run_slice(days: list[date]) -> dict:
        filtered = [d for d in days if d in available]
        if not filtered:
            return {"sharpe_ratio": 0.0, "total_pnl": 0.0, "num_trades": 0}
        engine = BacktestEngine()
        result = asyncio.run(
            engine.run(
                strategy_name=spec_dict["strategy_name"],
                strategy_params=dict(params),
                initial_capital=spec_dict["initial_capital"],
                seed=seed,
                market_source=source,
                days=filtered,
            )
        )
        return dict(result.get("metrics", {}))

    train_metrics = _run_slice(train_dates)
    test_metrics = _run_slice(test_dates)
    return window_idx, train_metrics, test_metrics


class WalkForwardValidator:
    """Rolling walk-forward harness with optional optimizer hook.

    Args:
        train_window_days: Number of dates in each training slice.
        test_window_days: Number of dates in each testing slice.
        step_days: How many dates to advance between consecutive
            training windows.
        embargo_days: Number of dates skipped between train_end and
            test_start (leakage guard).
    """

    # Rollout gate (mirrors expert_review_apr23.md):
    DECAY_THRESHOLD = 0.5
    POSITIVE_FRACTION_THRESHOLD = 0.7

    def __init__(
        self,
        train_window_days: int = 90,
        test_window_days: int = 30,
        step_days: int = 15,
        embargo_days: int = 1,
    ):
        if train_window_days < 1 or test_window_days < 1:
            raise ValueError("train/test window sizes must be >= 1")
        if step_days < 1:
            raise ValueError("step_days must be >= 1")
        if embargo_days < 0:
            raise ValueError("embargo_days must be >= 0")

        self.train_window_days = int(train_window_days)
        self.test_window_days = int(test_window_days)
        self.step_days = int(step_days)
        self.embargo_days = int(embargo_days)

    # ─── Window generation ───────────────────────────────────────────

    def _window_bounds(
        self, n: int
    ) -> list[tuple[int, int, int, int]]:
        """Enumerate ``(train_lo, train_hi, test_lo, test_hi)`` tuples.

        Bounds use Python half-open convention ``[lo, hi)``. Stop when
        ``test_hi > n`` would exceed the date array.
        """
        W = self.train_window_days
        T = self.test_window_days
        S = self.step_days
        E = self.embargo_days

        windows: list[tuple[int, int, int, int]] = []
        i = 0
        while True:
            train_lo = i * S
            train_hi = train_lo + W
            test_lo = train_hi + E
            test_hi = test_lo + T
            if test_hi > n:
                break
            windows.append((train_lo, train_hi, test_lo, test_hi))
            i += 1
        return windows

    # ─── Async orchestration ─────────────────────────────────────────

    async def run(
        self,
        dates: list[date],
        runner_fn: Callable[[list[date], dict[str, Any]], Awaitable[dict[str, Any]]],
        baseline_params: dict[str, Any],
        optimizer_fn: Callable[
            [list[date], dict[str, Any], "WalkForwardValidator"],
            dict[str, Any],
        ] | None = None,
        *,
        workers: int = 1,
        runner_spec: Any | None = None,
    ) -> WFReport:
        """Run the walk-forward sweep, returning a :class:`WFReport`.

        For each window:

        1. If ``optimizer_fn`` is provided, call it with
           ``(train_days, copy(baseline_params), self)`` → per-window params.
           Otherwise use a fresh copy of ``baseline_params``.
        2. Run ``runner_fn(train_days, params)`` → train metrics.
        3. Run ``runner_fn(test_days, params)`` → test metrics.
        4. Append a :class:`WFWindow`.

        Neither ``baseline_params`` nor any returned params dict is
        mutated across windows — each call gets its own shallow copy.

        Args:
            dates: Business-day list for the full evaluation window.
            runner_fn: Async callable ``(days, params) → result_dict``.
                Used when ``workers <= 1`` or ``runner_spec is None``.
            baseline_params: Default strategy parameter set.
            optimizer_fn: Optional per-window parameter optimizer.
            workers: Number of parallel subprocess workers.
                ``1`` (default) uses the sequential async path — preserves
                backwards compatibility and works without parquet data.
                ``> 1`` dispatches windows to a ``ProcessPoolExecutor``;
                requires ``runner_spec`` to be supplied.
            runner_spec: A :class:`parallel_runner.RunnerSpec` (or any
                object with the same fields as its ``__dict__``). Required
                when ``workers > 1``. Ignored when ``workers <= 1``.
        """
        n = len(dates)
        bounds = self._window_bounds(n)

        if not bounds:
            logger.warning(
                "[WF] No windows fit: n=%d train=%d test=%d embargo=%d",
                n, self.train_window_days, self.test_window_days,
                self.embargo_days,
            )
            return WFReport(
                windows=[], median_decay=0.0, fraction_positive_test=0.0,
                passed=False, mean_test_sharpe=0.0,
            )

        # Resolve per-window params BEFORE dispatching (optimizer_fn may
        # not be picklable, so we call it in the parent process).
        window_params_list: list[dict[str, Any]] = []
        for idx, (train_lo, train_hi, _tlo, _thi) in enumerate(bounds):
            train_days_for_opt = dates[train_lo:train_hi]
            params = dict(baseline_params)
            if optimizer_fn is not None:
                optimized = optimizer_fn(train_days_for_opt, dict(baseline_params), self)
                params = dict(optimized)
            window_params_list.append(params)

        if workers > 1 and runner_spec is not None:
            return await self._run_parallel(
                dates, bounds, window_params_list, runner_spec, workers
            )
        return await self._run_sequential(
            dates, bounds, window_params_list, runner_fn
        )

    # ─── Sequential path ─────────────────────────────────────────────

    async def _run_sequential(
        self,
        dates: list[date],
        bounds: list[tuple[int, int, int, int]],
        window_params_list: list[dict[str, Any]],
        runner_fn: Callable[[list[date], dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> WFReport:
        results: list[WFWindow] = []

        for idx, (train_lo, train_hi, test_lo, test_hi) in enumerate(bounds):
            train_days = dates[train_lo:train_hi]
            test_days = dates[test_lo:test_hi]
            params = window_params_list[idx]

            train_result = await runner_fn(train_days, dict(params))
            test_result = await runner_fn(test_days, dict(params))

            train_metrics = train_result.get("metrics", {})
            test_metrics = test_result.get("metrics", {})

            win = self._make_window(idx, train_days, test_days, params, train_metrics, test_metrics)
            results.append(win)

            logger.info(
                "[WF] window %d: train [%s..%s] sharpe=%.3f "
                "test [%s..%s] sharpe=%.3f decay=%.3f",
                idx,
                win.train_start, win.train_end, win.train_sharpe,
                win.test_start, win.test_end, win.test_sharpe,
                win.train_sharpe - win.test_sharpe,
            )

        return self._build_report(results)

    # ─── Parallel path ───────────────────────────────────────────────

    async def _run_parallel(
        self,
        dates: list[date],
        bounds: list[tuple[int, int, int, int]],
        window_params_list: list[dict[str, Any]],
        runner_spec: Any,
        workers: int,
    ) -> WFReport:
        """Dispatch all windows to a ProcessPoolExecutor, collect results.

        Per-window seed is derived deterministically from the runner_spec's
        base_seed + window_idx, mirroring the CPCV path-seed derivation.
        Running with the same base_seed always produces bit-identical
        window metrics regardless of worker count.
        """
        # Convert RunnerSpec (dataclass) to a plain dict for picklability
        spec_dict = (
            dict(runner_spec.__dict__)
            if hasattr(runner_spec, "__dict__")
            else {
                "strategy_name": runner_spec.strategy_name,
                "parquet_dir": runner_spec.parquet_dir,
                "underlying": runner_spec.underlying,
                "spot_token": runner_spec.spot_token,
                "initial_capital": runner_spec.initial_capital,
                "base_seed": runner_spec.base_seed,
            }
        )

        logger.info(
            "[WF] running %d windows across %d workers (spawn) — base_seed=%d",
            len(bounds), workers, spec_dict["base_seed"],
        )

        ctx = multiprocessing.get_context("spawn")

        # Map: future → (idx, train_days, test_days, params)
        pending: dict[Any, tuple[int, list[date], list[date], dict]] = {}

        # Pre-build all window slices so we can submit without holding the lock
        window_slices = [
            (idx, dates[trl:trh], dates[tsl:tsh], window_params_list[idx])
            for idx, (trl, trh, tsl, tsh) in enumerate(bounds)
        ]

        collected: dict[int, tuple[dict, dict]] = {}  # idx → (train_m, test_m)

        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
            for idx, train_days, test_days, params in window_slices:
                fut = pool.submit(
                    _run_wf_window_in_subprocess,
                    spec_dict,
                    list(train_days),
                    list(test_days),
                    dict(params),
                    idx,
                )
                pending[fut] = (idx, train_days, test_days, params)

            completed = 0
            for fut in as_completed(pending):
                completed += 1
                idx_meta, train_days, test_days, params = pending[fut]
                try:
                    win_idx, train_metrics, test_metrics = fut.result()
                except Exception as exc:
                    logger.error(
                        "[WF] window %d failed in worker: %s — recording zeros",
                        idx_meta, exc,
                    )
                    train_metrics = {"sharpe_ratio": 0.0, "total_pnl": 0.0, "num_trades": 0}
                    test_metrics = {"sharpe_ratio": 0.0, "total_pnl": 0.0, "num_trades": 0}
                    win_idx = idx_meta
                collected[win_idx] = (train_metrics, test_metrics)
                if completed % max(1, len(bounds) // 5) == 0:
                    logger.info("[WF] %d/%d windows complete", completed, len(bounds))

        # Reconstruct results in deterministic order (by window_idx)
        results: list[WFWindow] = []
        for idx, (trl, trh, tsl, tsh) in enumerate(bounds):
            train_days = dates[trl:trh]
            test_days = dates[tsl:tsh]
            params = window_params_list[idx]
            train_metrics, test_metrics = collected.get(idx, ({}, {}))
            win = self._make_window(idx, train_days, test_days, params, train_metrics, test_metrics)
            results.append(win)

            logger.info(
                "[WF] window %d: train [%s..%s] sharpe=%.3f "
                "test [%s..%s] sharpe=%.3f decay=%.3f",
                idx,
                win.train_start, win.train_end, win.train_sharpe,
                win.test_start, win.test_end, win.test_sharpe,
                win.train_sharpe - win.test_sharpe,
            )

        return self._build_report(results)

    # ─── Helpers ─────────────────────────────────────────────────────

    def _make_window(
        self,
        idx: int,
        train_days: list[date],
        test_days: list[date],
        params: dict[str, Any],
        train_metrics: dict,
        test_metrics: dict,
    ) -> WFWindow:
        return WFWindow(
            idx=idx,
            train_start=train_days[0],
            train_end=train_days[-1],
            test_start=test_days[0],
            test_end=test_days[-1],
            params=params,
            train_sharpe=float(train_metrics.get("sharpe_ratio", 0.0)),
            test_sharpe=float(test_metrics.get("sharpe_ratio", 0.0)),
            train_pnl=float(train_metrics.get("total_pnl", 0.0)),
            test_pnl=float(test_metrics.get("total_pnl", 0.0)),
            num_test_trades=int(test_metrics.get("num_trades", 0)),
            extra={
                "train_metrics": dict(train_metrics),
                "test_metrics": dict(test_metrics),
            },
        )

    # ─── Aggregation ─────────────────────────────────────────────────

    def _build_report(self, windows: list[WFWindow]) -> WFReport:
        if not windows:
            return WFReport(
                windows=[], median_decay=0.0, fraction_positive_test=0.0,
                passed=False, mean_test_sharpe=0.0,
            )

        decays = sorted(w.train_sharpe - w.test_sharpe for w in windows)
        mid = len(decays) // 2
        if len(decays) % 2 == 1:
            median_decay = decays[mid]
        else:
            median_decay = 0.5 * (decays[mid - 1] + decays[mid])

        positives = sum(1 for w in windows if w.test_sharpe > 0)
        frac_positive = positives / len(windows)
        mean_test = sum(w.test_sharpe for w in windows) / len(windows)

        passed = (
            median_decay < self.DECAY_THRESHOLD
            and frac_positive >= self.POSITIVE_FRACTION_THRESHOLD
        )

        logger.info(
            "[WF] %d windows: median_decay=%.3f frac_positive=%.2f "
            "mean_test_sharpe=%.3f passed=%s",
            len(windows), median_decay, frac_positive, mean_test, passed,
        )

        return WFReport(
            windows=windows,
            median_decay=float(median_decay),
            fraction_positive_test=float(frac_positive),
            passed=bool(passed),
            mean_test_sharpe=float(mean_test),
        )
