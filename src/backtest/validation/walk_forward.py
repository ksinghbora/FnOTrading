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
"""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)


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

        results: list[WFWindow] = []

        for idx, (train_lo, train_hi, test_lo, test_hi) in enumerate(bounds):
            train_days = dates[train_lo:train_hi]
            test_days = dates[test_lo:test_hi]

            params = dict(baseline_params)
            if optimizer_fn is not None:
                optimized = optimizer_fn(train_days, dict(baseline_params), self)
                # Defensive copy so downstream mutation can't leak back.
                params = dict(optimized)

            # Don't share the dict with the runner — a shallow copy is
            # enough since params values are typically scalars.
            train_result = await runner_fn(train_days, dict(params))
            test_result = await runner_fn(test_days, dict(params))

            train_metrics = train_result.get("metrics", {})
            test_metrics = test_result.get("metrics", {})

            win = WFWindow(
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
