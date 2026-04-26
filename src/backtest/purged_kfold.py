"""Purged K-Fold Cross-Validation with embargo.

Implements Marcos López de Prado's purged/embargoed CV scheme (Advances in
Financial Machine Learning, ch. 7) for time-series strategy tuning. Prevents
the temporal-leakage failure mode that produced the Apr 23 curve-fit on
Jul-Aug 2025 loss days (see memory/expert_review_apr23.md).

Why this is needed
------------------
Standard k-fold randomly shuffles samples across train/test folds. For
time-series strategies that's fatal:

  1. A trade opened on day T influences bar/tick values for several days
     after T through slippage, implied vol shifts, or pin risk.
  2. If fold boundaries are porous, a trade opened near the boundary leaks
     its realised outcome into the training fold across the boundary.
  3. The classifier learns to recognise THAT trade from its post-entry
     fingerprints — OOS performance collapses.

Two defences:

  * **Purging** — remove train samples whose information window overlaps
    the test fold. Here we model the information window as the embargo band
    on either side of the test fold.
  * **Embargo** — keep a physical gap (in days) between train and test so
    stale signals can decay.

Usage
-----
    from src.backtest.purged_kfold import PurgedKFold

    dates = [date(2025, 1, 6), date(2025, 1, 7), ...]  # business days
    kf = PurgedKFold(n_splits=5, embargo_pct=0.01)
    for train_idx, test_idx in kf.split(dates):
        # train_idx / test_idx are positions into `dates`
        ...

    # Or evaluate a parameter set:
    result = await kf.evaluate(
        param_set={"premium_stop_loss_pct": 0.25},
        strategy_fn=my_runner,
        dates=dates,
    )
    # result = {"mean_oos_sharpe", "std_oos_sharpe", "oos_pf",
    #           "fold_results", "accepted"}

Contract with `strategy_fn`
---------------------------
`strategy_fn` is an async callable accepting kwargs:

    async def runner(*, param_set: dict, train_dates: list[date],
                     test_dates: list[date]) -> dict: ...

It must return a dict with at minimum `sharpe` and `profit_factor` measured
on the **test** window. `param_set` is passed unchanged — the runner decides
how to plug those params into whatever strategy/backtest it wraps.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class FoldResult:
    """One (train, test) fold result."""

    fold_num: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    train_len: int
    test_len: int
    purged_len: int  # how many dates were dropped from train due to purge/embargo
    sharpe: float
    profit_factor: float
    extra: dict[str, Any] = field(default_factory=dict)


class PurgedKFold:
    """Purged K-Fold Cross-Validator with embargo for time-series backtests.

    Args:
        n_splits: Number of chronological folds. Each fold takes a turn as
            the test fold; all other folds form the train set (minus the
            embargo band).
        embargo_pct: Fraction of the total date range used as embargo band
            on either side of the test fold. Any train date inside this
            band is purged. E.g. embargo_pct=0.01 with 100 dates => 1 day
            of embargo each side.

    Rejection rule:
        A parameter set is considered curve-fit when
        `std_oos_sharpe > mean_oos_sharpe` — i.e. the per-fold OOS Sharpe
        is noisier than its mean. This is a classical tell for overfitting
        in financial ML.
    """

    def __init__(self, n_splits: int = 5, embargo_pct: float = 0.01):
        if n_splits < 2:
            raise ValueError(f"n_splits must be >= 2, got {n_splits}")
        if embargo_pct < 0 or embargo_pct >= 0.5:
            raise ValueError(
                f"embargo_pct must be in [0, 0.5), got {embargo_pct}"
            )
        self.n_splits = n_splits
        self.embargo_pct = embargo_pct

    # ─── Split generator ─────────────────────────────────────────────────

    def split(
        self, dates: list[date]
    ) -> Iterator[tuple[list[int], list[int]]]:
        """Yield (train_indices, test_indices) for each fold.

        Indices are positions into the input `dates` list. Folds are
        chronological — no shuffling — with an embargo band of
        `embargo_pct * len(dates)` days purged from train around the test
        fold boundaries.

        Each test fold is disjoint from its train fold, and no train date
        falls within `embargo_days` days of the test fold's start or end.
        """
        n = len(dates)
        if n < self.n_splits:
            raise ValueError(
                f"Need at least {self.n_splits} dates to split into "
                f"{self.n_splits} folds, got {n}"
            )

        embargo_days = max(1, int(math.ceil(self.embargo_pct * n)))

        # Chronological fold boundaries. Use numpy-style even distribution:
        # the first (n % n_splits) folds get one extra element.
        fold_sizes = [n // self.n_splits] * self.n_splits
        for i in range(n % self.n_splits):
            fold_sizes[i] += 1

        fold_bounds: list[tuple[int, int]] = []
        cursor = 0
        for sz in fold_sizes:
            fold_bounds.append((cursor, cursor + sz))
            cursor += sz
        # Sanity
        assert fold_bounds[-1][1] == n

        for fold_idx, (test_start, test_end) in enumerate(fold_bounds):
            test_idx = list(range(test_start, test_end))

            # Train = everything outside [test_start, test_end), MINUS the
            # embargo band on either side of the test fold.
            purge_lo = max(0, test_start - embargo_days)
            purge_hi = min(n, test_end + embargo_days)

            train_idx = [
                i for i in range(n)
                if i < purge_lo or i >= purge_hi
            ]

            if not train_idx:
                # Degenerate case — tiny dataset. Skip so callers don't
                # silently train on an empty window.
                logger.warning(
                    "[PURGED_KFOLD] fold %d: empty train set after purge "
                    "(n=%d, embargo=%d) — skipping",
                    fold_idx, n, embargo_days,
                )
                continue

            yield train_idx, test_idx

    # ─── Evaluation wrapper ──────────────────────────────────────────────

    async def evaluate(
        self,
        param_set: dict[str, Any],
        strategy_fn: Callable[..., Any],
        dates: list[date],
    ) -> dict[str, Any]:
        """Run `strategy_fn` on every fold with `param_set`, aggregate OOS.

        Args:
            param_set: Parameter dict passed verbatim to `strategy_fn`.
            strategy_fn: Async callable with signature
                ``async def fn(*, param_set, train_dates, test_dates) -> dict``
                returning at minimum ``{"sharpe": float, "profit_factor": float}``.
                Anything else in the returned dict is preserved in
                `FoldResult.extra` for downstream analysis.
            dates: Full chronological date universe.

        Returns:
            dict with:
              - ``mean_oos_sharpe`` — mean of per-fold OOS Sharpe
              - ``std_oos_sharpe`` — std dev of per-fold OOS Sharpe (stability)
              - ``oos_pf`` — mean of per-fold OOS profit factor
              - ``fold_results`` — list[FoldResult]
              - ``accepted`` — False if std_oos_sharpe > mean_oos_sharpe
                (curve-fit tell); True otherwise.
        """
        fold_results: list[FoldResult] = []

        for fold_num, (train_idx, test_idx) in enumerate(self.split(dates), start=1):
            train_dates = [dates[i] for i in train_idx]
            test_dates = [dates[i] for i in test_idx]
            purged_len = len(dates) - len(train_idx) - len(test_idx)

            logger.info(
                "[PURGED_KFOLD] fold %d/%d: train=%d test=%d purged=%d "
                "test_window=%s..%s",
                fold_num, self.n_splits,
                len(train_dates), len(test_dates), purged_len,
                test_dates[0], test_dates[-1],
            )

            result = await strategy_fn(
                param_set=param_set,
                train_dates=train_dates,
                test_dates=test_dates,
            )

            sharpe = float(result.get("sharpe", 0.0))
            pf = float(result.get("profit_factor", 0.0))
            extra = {
                k: v for k, v in result.items()
                if k not in ("sharpe", "profit_factor")
            }

            fold_results.append(FoldResult(
                fold_num=fold_num,
                train_start=train_dates[0],
                train_end=train_dates[-1],
                test_start=test_dates[0],
                test_end=test_dates[-1],
                train_len=len(train_dates),
                test_len=len(test_dates),
                purged_len=purged_len,
                sharpe=sharpe,
                profit_factor=pf,
                extra=extra,
            ))

        if not fold_results:
            return {
                "mean_oos_sharpe": 0.0,
                "std_oos_sharpe": 0.0,
                "oos_pf": 0.0,
                "fold_results": [],
                "accepted": False,
            }

        sharpes = [f.sharpe for f in fold_results]
        pfs = [f.profit_factor for f in fold_results]
        mean_sharpe = sum(sharpes) / len(sharpes)
        # Population std (ddof=0) — matches numpy default and avoids an
        # undefined value when only 1 fold is usable.
        var = sum((s - mean_sharpe) ** 2 for s in sharpes) / len(sharpes)
        std_sharpe = math.sqrt(var)
        mean_pf = sum(pfs) / len(pfs)

        # Curve-fit rejection: noisier than mean ⇒ not stable across regimes.
        # Also reject on non-positive mean (nothing to stabilise anyway).
        accepted = mean_sharpe > 0 and std_sharpe <= mean_sharpe

        logger.info(
            "[PURGED_KFOLD] %d-fold summary: mean_sharpe=%.3f std_sharpe=%.3f "
            "pf=%.3f accepted=%s",
            len(fold_results), mean_sharpe, std_sharpe, mean_pf, accepted,
        )

        return {
            "mean_oos_sharpe": round(mean_sharpe, 4),
            "std_oos_sharpe": round(std_sharpe, 4),
            "oos_pf": round(mean_pf, 4),
            "fold_results": fold_results,
            "accepted": bool(accepted),
        }
