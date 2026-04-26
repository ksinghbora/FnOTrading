"""Combinatorial Purged Cross-Validation (CPCV).

Implements López de Prado, *Advances in Financial Machine Learning* §7.4:
instead of a single ordered k-fold, enumerate every
``C(n_folds, n_test_folds)`` combination of test-fold identities. Each
combination yields a distinct train/test path; aggregating Sharpe across
paths gives a **distribution** rather than a single point estimate — the
proper object for assessing stability under the realised sample.

Contrast with :class:`src.backtest.purged_kfold.PurgedKFold`:

* PurgedKFold yields ``n_splits`` paths, one per fold-as-test.
* CPCV with ``(n_folds=10, n_test_folds=2)`` yields
  ``C(10, 2) = 45`` paths, so every parameter set gets 45 Sharpe reads
  rather than 10.

Why it matters
--------------
Apr 23 expert review flagged the tuned parameter set as curve-fit in
part because the two-way split gave one Sharpe number per config. With a
distribution we can:

* quote Sharpe p05/p50/p95 rather than pretend point estimates mean
  anything on 100-day samples;
* feed the distribution into PBO (Probability of Backtest Overfitting)
  once multiple param sets have been evaluated — this module leaves
  ``pbo=None`` in single-config mode, to be filled by the caller when
  they compare multiple configs.

Contract with ``runner_fn``
---------------------------
``runner_fn`` is an async callable with signature::

    async def run(train_dates: list[date], params: dict) -> dict:
        return {"metrics": {"sharpe_ratio": ..., "total_pnl": ...,
                           "num_trades": ..., ...}, ...}

Only the *train* dates are passed — CPCV treats "test" as the
held-out portion for evaluation, but the runner itself is trained and
evaluated however the caller chooses (typically: re-optimize on
train_dates, then compute performance on them). For strict OOS
evaluation the caller should use :meth:`split` directly and plumb both
index sets to their runner.

Embargo semantics
-----------------
``embargo_pct * len(dates)`` days are removed from train on **both
sides** of every test region. With ``n_test_folds=2`` the two test
folds can be non-contiguous, so each independent test boundary gets its
own embargo band — matching the isolation pattern of
:class:`PurgedKFold`.
"""

from __future__ import annotations

import itertools
import logging
import math
import random
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class CPCVPath:
    """One combinatorial path result."""

    path_id: int
    test_fold_ids: tuple[int, ...]
    train_dates: list[date]
    test_dates: list[date]
    metrics: dict[str, Any] = field(default_factory=dict)


class CombinatorialPurgedCV:
    """Combinatorial Purged CV with embargo and optional path subsampling.

    Args:
        n_folds: Number of chronological contiguous folds of the date
            universe. Higher gives more combinatorial paths but fewer
            dates per fold.
        n_test_folds: How many folds form the test set in each path.
            Must be in ``[1, n_folds - 1]``.
        embargo_pct: Fraction of total dates embargoed on either side of
            **every** test-fold boundary.
        max_paths: If the total number of combinations exceeds this,
            randomly sample ``max_paths`` without replacement (seeded).
        seed: RNG seed for the subsample — for determinism.
    """

    def __init__(
        self,
        n_folds: int = 10,
        n_test_folds: int = 2,
        embargo_pct: float = 0.01,
        max_paths: int = 50,
        seed: int = 42,
    ):
        if n_folds < 2:
            raise ValueError(f"n_folds must be >= 2, got {n_folds}")
        if not (1 <= n_test_folds < n_folds):
            raise ValueError(
                f"n_test_folds must be in [1, n_folds-1]; got {n_test_folds} "
                f"with n_folds={n_folds}"
            )
        if embargo_pct < 0 or embargo_pct >= 0.5:
            raise ValueError(
                f"embargo_pct must be in [0, 0.5), got {embargo_pct}"
            )
        if max_paths < 1:
            raise ValueError(f"max_paths must be >= 1, got {max_paths}")

        self.n_folds = n_folds
        self.n_test_folds = n_test_folds
        self.embargo_pct = embargo_pct
        self.max_paths = max_paths
        self.seed = seed

    # ─── Fold chunking ───────────────────────────────────────────────

    def _chunk_bounds(self, n: int) -> list[tuple[int, int]]:
        """Return ``[(lo, hi), ...]`` fold bounds (lo inclusive, hi exclusive)."""
        if n < self.n_folds:
            raise ValueError(
                f"Need at least {self.n_folds} dates, got {n}"
            )
        # Mirror PurgedKFold: first (n % n_folds) folds get one extra.
        sizes = [n // self.n_folds] * self.n_folds
        for i in range(n % self.n_folds):
            sizes[i] += 1
        bounds: list[tuple[int, int]] = []
        cursor = 0
        for sz in sizes:
            bounds.append((cursor, cursor + sz))
            cursor += sz
        return bounds

    # ─── Combinatorial enumeration ───────────────────────────────────

    def _select_combos(
        self, n_folds: int
    ) -> list[tuple[int, ...]]:
        """Enumerate fold-id combinations, subsampling to ``max_paths`` if needed."""
        all_combos = list(itertools.combinations(range(n_folds), self.n_test_folds))
        if len(all_combos) <= self.max_paths:
            return all_combos
        rng = random.Random(self.seed)
        # deterministic subsample
        return rng.sample(all_combos, self.max_paths)

    def split(
        self, dates: list[date]
    ) -> Iterator[tuple[list[int], list[int], tuple[int, ...]]]:
        """Yield ``(train_idx, test_idx, test_fold_ids)`` per combinatorial path.

        Indices are positions into ``dates``. Embargo is applied around
        every contiguous test region.
        """
        n = len(dates)
        bounds = self._chunk_bounds(n)
        embargo = max(1, int(math.ceil(self.embargo_pct * n)))
        combos = self._select_combos(len(bounds))

        for fold_ids in combos:
            test_idx_set: set[int] = set()
            for fid in fold_ids:
                lo, hi = bounds[fid]
                test_idx_set.update(range(lo, hi))

            # Purge embargo around EVERY test region boundary. A "region"
            # here is a maximal contiguous run of test indices; with
            # n_test_folds>=2 we can have multiple non-adjacent regions.
            purged_from_train: set[int] = set()
            test_sorted = sorted(test_idx_set)
            # Detect region boundaries
            region_bounds: list[tuple[int, int]] = []
            if test_sorted:
                r_lo = test_sorted[0]
                prev = r_lo
                for i in test_sorted[1:]:
                    if i == prev + 1:
                        prev = i
                    else:
                        region_bounds.append((r_lo, prev + 1))
                        r_lo = i
                        prev = i
                region_bounds.append((r_lo, prev + 1))

            for lo, hi in region_bounds:
                emb_lo = max(0, lo - embargo)
                emb_hi = min(n, hi + embargo)
                purged_from_train.update(range(emb_lo, emb_hi))

            train_idx = [
                i for i in range(n)
                if i not in purged_from_train
            ]
            test_idx = sorted(test_idx_set)

            if not train_idx:
                logger.warning(
                    "[CPCV] fold_ids=%s: empty train after embargo — skipping",
                    fold_ids,
                )
                continue

            yield train_idx, test_idx, fold_ids

    # ─── Async evaluation ────────────────────────────────────────────

    async def evaluate(
        self,
        param_set: dict[str, Any],
        runner_fn: Callable[[list[date], dict[str, Any]], Awaitable[dict[str, Any]]],
        dates: list[date],
        runner_spec: "RunnerSpec | None" = None,
        n_workers: int = 1,
    ) -> dict[str, Any]:
        """Run one CPCV path per fold-combination and aggregate.

        Two execution modes:

        1. **Sequential (default).** Calls ``runner_fn(train_dates,
           param_set)`` for each path in order. ``runner_fn`` must be an
           async callable. Used when ``n_workers <= 1`` or
           ``runner_spec`` is None.

        2. **Parallel.** When ``runner_spec`` is given AND ``n_workers
           > 1``, paths are dispatched to a process pool via
           ``parallel_evaluate_paths``. Each worker reconstructs its own
           ``BacktestEngine`` from the spec, derives a per-path RNG seed
           (``base_seed * 2654435761 + path_id``), and writes nothing
           to the shared decisions CSV (FNO_DISABLE_DECISIONS=1). The
           determinism test
           (``tests/integration/test_parallel_cpcv_determinism.py``)
           guarantees that ``n_workers=1`` and ``n_workers=N`` produce
           identical CPCV path metrics for the same ``base_seed``.

        Returns:
            Dict with ``paths`` (list[CPCVPath]), Sharpe distribution
            stats (``sharpe_median``, ``sharpe_mean``, ``sharpe_p05``,
            ``sharpe_p95``, ``sharpe_distribution`` as np.ndarray),
            ``num_trades_mean``, and ``pbo=None`` placeholder.
        """
        paths: list[CPCVPath] = []

        # Materialise all paths up-front so both modes use the same
        # data. Sequential mode iterates this list; parallel mode hands
        # it to the pool.
        all_paths: list[tuple[int, list[date], list[date], tuple[int, ...]]] = []
        for path_id, (train_idx, test_idx, fold_ids) in enumerate(self.split(dates)):
            train_dates = [dates[i] for i in train_idx]
            test_dates = [dates[i] for i in test_idx]
            all_paths.append((path_id, train_dates, test_dates, fold_ids))

        if runner_spec is not None and n_workers > 1:
            # Lazy import — keeps cpcv.py from depending on the parallel
            # runner module (and its multiprocessing import) when not used.
            from src.backtest.validation.parallel_runner import parallel_evaluate_paths

            results = await parallel_evaluate_paths(
                spec=runner_spec,
                paths_args=all_paths,
                param_set=param_set,
                n_workers=n_workers,
            )
            # Iterate path_id in order so the returned ``paths`` list is
            # deterministic regardless of completion order in the pool.
            for path_id, train_dates, test_dates, fold_ids in all_paths:
                _, _, _, metrics = results.get(
                    path_id, (train_dates, test_dates, fold_ids, {})
                )
                num_trades = int(metrics.get("num_trades", 0))
                if num_trades < 30:
                    logger.warning(
                        "[CPCV] path_id=%d fold_ids=%s: num_trades=%d (< 30) — "
                        "small-sample Sharpe is unreliable",
                        path_id, fold_ids, num_trades,
                    )
                paths.append(CPCVPath(
                    path_id=path_id,
                    test_fold_ids=fold_ids,
                    train_dates=train_dates,
                    test_dates=test_dates,
                    metrics=metrics,
                ))
        else:
            for path_id, train_dates, test_dates, fold_ids in all_paths:
                # Defensive: don't let path logic mutate caller's param dict
                params_copy = dict(param_set)
                result = await runner_fn(train_dates, params_copy)
                metrics = dict(result.get("metrics", {}))

                num_trades = int(metrics.get("num_trades", 0))
                if num_trades < 30:
                    logger.warning(
                        "[CPCV] path_id=%d fold_ids=%s: num_trades=%d (< 30) — "
                        "small-sample Sharpe is unreliable",
                        path_id, fold_ids, num_trades,
                    )

                paths.append(CPCVPath(
                    path_id=path_id,
                    test_fold_ids=fold_ids,
                    train_dates=train_dates,
                    test_dates=test_dates,
                    metrics=metrics,
                ))

        if not paths:
            logger.warning("[CPCV] No paths generated — returning empty distribution")
            empty = np.array([], dtype=float)
            return {
                "paths": [],
                "sharpe_distribution": empty,
                "sharpe_median": 0.0,
                "sharpe_mean": 0.0,
                "sharpe_p05": 0.0,
                "sharpe_p95": 0.0,
                "pbo": None,
                "num_trades_mean": 0.0,
            }

        sharpes = np.array(
            [float(p.metrics.get("sharpe_ratio", 0.0)) for p in paths],
            dtype=float,
        )
        trades = np.array(
            [float(p.metrics.get("num_trades", 0)) for p in paths],
            dtype=float,
        )

        logger.info(
            "[CPCV] %d paths: sharpe median=%.3f mean=%.3f p05=%.3f p95=%.3f",
            len(paths),
            float(np.median(sharpes)), float(np.mean(sharpes)),
            float(np.percentile(sharpes, 5)), float(np.percentile(sharpes, 95)),
        )

        return {
            "paths": paths,
            "sharpe_distribution": sharpes,
            "sharpe_median": float(np.median(sharpes)),
            "sharpe_mean": float(np.mean(sharpes)),
            "sharpe_p05": float(np.percentile(sharpes, 5)),
            "sharpe_p95": float(np.percentile(sharpes, 95)),
            "pbo": None,  # computed by caller with multi-config matrices
            "num_trades_mean": float(np.mean(trades)),
        }
