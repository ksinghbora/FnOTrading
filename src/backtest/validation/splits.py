"""Three-way train/val/holdout chronological split with holdout access counter.

Why this exists
---------------
The Apr 23 dual expert review (see ``memory/expert_review_apr23.md``)
identified the two-way chronological train/test split as the single
biggest methodological leak: every tuning iteration quietly pulled signal
out of the "OOS" window. The fix is a three-way split where the holdout
is gated by a persistent access counter — each strategy is allowed to
peek at its holdout **once**. Subsequent accesses raise
:class:`HoldoutOverUsed` unless the caller explicitly passes
``allow_burn=True`` (logging the access but still blocking for future
automation).

Boundary semantics
------------------
All three boundaries are *inclusive*. The train window is
``[first_available_day, train_end]``; val is ``(train_end, val_end]``;
holdout is ``(val_end, holdout_end]``. When a :class:`GDFLMarketSource`
is provided, each window is intersected with its ``available_days()`` —
otherwise the date universe is ``pandas.bdate_range`` (business days
only).

Usage
-----
    split = StrategySplit(
        train_end=date(2025, 12, 31),
        val_end=date(2026, 2, 28),
        holdout_end=date(2026, 4, 22),
    )
    loader = SplitLoader(split, strategy="portfolio", gdfl_source=gdfl)

    # Tuning / CV work on train+val only
    df = loader.load_decisions(loader.train_and_val_days())

    # After tuning is frozen, ONE holdout evaluation is allowed
    final_days = loader.holdout_days()  # second call raises
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from src.backtest.gdfl_market_source import GDFLMarketSource

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StrategySplit:
    """Inclusive boundary dates for a three-way chronological split.

    Invariants: ``train_end < val_end < holdout_end``.
    """

    train_end: date
    val_end: date
    holdout_end: date

    def __post_init__(self) -> None:
        if not (self.train_end < self.val_end < self.holdout_end):
            raise ValueError(
                f"StrategySplit requires train_end < val_end < holdout_end; "
                f"got {self.train_end} / {self.val_end} / {self.holdout_end}"
            )


class HoldoutOverUsed(RuntimeError):
    """Raised when holdout accessed more than `access_limit` times.

    The guard is deliberately binary: once tripped, the strategy's
    tuning decisions are contaminated for this split boundary triple.
    Callers that *intentionally* want to burn the holdout (e.g. final
    production deployment after sign-off) pass ``allow_burn=True``.
    """


class SplitLoader:
    """Three-way split loader with persistent holdout access counter.

    State file layout (JSON at ``state_dir/holdout_access.json``):

        {"<strategy>|<train_end>|<val_end>|<holdout_end>":
            {"count": N, "last_access": "<iso-timestamp>"}}

    The composite key means changing *any* boundary resets the counter,
    which is intentional — if you widen the holdout you're evaluating a
    different hypothesis.
    """

    def __init__(
        self,
        split: StrategySplit,
        strategy: str,
        decisions_dir: Path = Path("data/decisions"),
        gdfl_source: "GDFLMarketSource | None" = None,
        state_dir: Path = Path("data/validation_state"),
        access_limit: int = 1,
    ):
        self.split = split
        self.strategy = strategy
        self.decisions_dir = Path(decisions_dir)
        self.gdfl_source = gdfl_source
        self.state_dir = Path(state_dir)
        self.access_limit = int(access_limit)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self.state_dir / "holdout_access.json"
        self._lock_path = self.state_dir / "holdout_access.lock"

    # ─── Key + state helpers ─────────────────────────────────────────

    def _access_key(self) -> str:
        return (
            f"{self.strategy}|{self.split.train_end.isoformat()}|"
            f"{self.split.val_end.isoformat()}|{self.split.holdout_end.isoformat()}"
        )

    def _read_state(self) -> dict:
        if not self._state_path.exists():
            return {}
        try:
            with self._state_path.open() as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("[SPLIT] Failed to read holdout state %s: %s",
                           self._state_path, exc)
            return {}

    def _write_state(self, state: dict) -> None:
        # Best-effort lock via filelock if installed; otherwise atomic write.
        try:
            import filelock  # type: ignore[import-not-found]
            lock = filelock.FileLock(str(self._lock_path), timeout=10)
            with lock:
                self._atomic_write(state)
        except ImportError:
            try:
                self._atomic_write(state)
            except OSError as exc:
                logger.warning(
                    "[SPLIT] Atomic write failed for %s: %s",
                    self._state_path, exc,
                )

    def _atomic_write(self, state: dict) -> None:
        tmp = self._state_path.with_suffix(".tmp")
        with tmp.open("w") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        tmp.replace(self._state_path)

    def holdout_access_count(self) -> int:
        """Return the current access count for this (strategy, split) tuple."""
        state = self._read_state()
        entry = state.get(self._access_key())
        return int(entry.get("count", 0)) if entry else 0

    # ─── Date enumeration ────────────────────────────────────────────

    def _bdate_range(self, start: date, end: date) -> list[date]:
        """Business-day range, inclusive on both ends."""
        if start > end:
            return []
        idx = pd.bdate_range(start=start, end=end)
        return [ts.date() for ts in idx]

    def _window(self, start: date, end: date) -> list[date]:
        """Enumerate a window and optionally intersect with GDFL availability."""
        bdays = self._bdate_range(start, end)
        if self.gdfl_source is None:
            return bdays
        available = set(self.gdfl_source.available_days())
        return [d for d in bdays if d in available]

    def train_days(self) -> list[date]:
        """All business days up to and including ``split.train_end``.

        Lower bound is inferred from GDFL availability if present; else
        the Unix epoch (so the full bdate_range is returned — in practice
        callers clamp this by passing a reasonable ``train_end``).
        """
        if self.gdfl_source is not None:
            available = self.gdfl_source.available_days()
            if not available:
                return []
            start = available[0]
        else:
            # Without a GDFL source we can't infer a floor; use an
            # arbitrary epoch. Callers drive the window via train_end.
            start = date(2000, 1, 1)
        return self._window(start, self.split.train_end)

    def val_days(self) -> list[date]:
        """Business days in ``(train_end, val_end]``."""
        start = self._day_after(self.split.train_end)
        return self._window(start, self.split.val_end)

    def train_and_val_days(self) -> list[date]:
        """Concatenated train + val window — the CV/walk-forward domain."""
        return self.train_days() + self.val_days()

    def holdout_days(self, allow_burn: bool = False) -> list[date]:
        """Business days in ``(val_end, holdout_end]``. Access-counted.

        Raises :class:`HoldoutOverUsed` if the counter is at or above
        ``access_limit`` and ``allow_burn`` is False. The counter is
        incremented on *every* call (including ``allow_burn=True``), so
        repeated burns are still visible in the audit trail.
        """
        state = self._read_state()
        key = self._access_key()
        entry = state.get(key, {"count": 0, "last_access": None})
        count = int(entry.get("count", 0))

        if count >= self.access_limit and not allow_burn:
            raise HoldoutOverUsed(
                f"Holdout for strategy={self.strategy!r} "
                f"split=({self.split.train_end}/{self.split.val_end}/"
                f"{self.split.holdout_end}) already accessed {count} times "
                f"(limit={self.access_limit}). Pass allow_burn=True to "
                f"explicitly consume another access."
            )

        # Always record access — including burns.
        entry["count"] = count + 1
        entry["last_access"] = datetime.now().isoformat()
        state[key] = entry
        self._write_state(state)

        start = self._day_after(self.split.val_end)
        days = self._window(start, self.split.holdout_end)
        logger.info(
            "[SPLIT] Holdout access #%d for strategy=%s (burn=%s) -> %d days",
            entry["count"], self.strategy, allow_burn, len(days),
        )
        return days

    # ─── Decision CSV loader ─────────────────────────────────────────

    def load_decisions(self, days: list[date]) -> pd.DataFrame:
        """Load and concatenate ``decisions_YYYY-MM-DD.csv`` for the given days.

        Missing files are silently skipped — returning an empty DataFrame
        when none of the requested days have a CSV is not an error.
        """
        frames: list[pd.DataFrame] = []
        for d in days:
            path = self.decisions_dir / f"decisions_{d.isoformat()}.csv"
            if not path.exists():
                continue
            try:
                frames.append(pd.read_csv(path))
            except (pd.errors.EmptyDataError, OSError) as exc:
                logger.warning("[SPLIT] Failed reading %s: %s", path, exc)
                continue
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def available_gdfl_days(self, days: list[date]) -> list[date]:
        """Intersect ``days`` with GDFL parquet availability (if source set)."""
        if self.gdfl_source is None:
            return list(days)
        available = set(self.gdfl_source.available_days())
        return [d for d in days if d in available]

    # ─── Internals ───────────────────────────────────────────────────

    @staticmethod
    def _day_after(d: date) -> date:
        """Return the next *business* day after ``d`` (exclusive boundary helper)."""
        nxt = pd.Timestamp(d) + pd.tseries.offsets.BDay(1)
        return nxt.date()
