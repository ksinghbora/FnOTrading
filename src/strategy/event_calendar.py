"""Event-day calendar and Friday square-off helpers.

Roadmap P1 #11 & #12 (Apr 23 expert review):

* **P1 #11** — RBI policy / Fed FOMC / Union Budget / CPI / IIP days currently
  only reduce the premium-leg score by -5/-15/-25 (see
  ``portfolio_strategy.py::_evaluate_premium`` ~line 522). Soft penalties do
  not prevent 3-5 blow-up days/year on Indian option premium books. This
  module raises a hard block for HARD_BLOCK-severity events.

* **P1 #12** — Weekend-gap risk from Monday-open jumps after event weekends
  is not modelled by a minute-cadence backtest. This module exposes
  :func:`is_friday_for_premium` so the strategy can force-flat premium legs
  at 14:55 on Fridays.

CSV schema (``data/event_days.csv``)::

    date, event_type, severity
    2026-02-01, BUDGET, HARD_BLOCK
    2026-04-09, RBI_POLICY, HARD_BLOCK
    2026-04-12, CPI_IN, SOFT_CAUTION

``event_type`` — one of ``RBI_POLICY``, ``FED_FOMC``, ``BUDGET``,
``CPI_IN``, ``IIP_IN``, ``ELECTION``, ``OTHER``.
``severity``   — ``HARD_BLOCK`` (skip entries entirely) or
                 ``SOFT_CAUTION`` (keep the existing score penalty).

Unknown types pass through but are logged once at load time. Missing
columns or a missing file return an empty calendar (no hard blocks) rather
than raising — the soft-penalty path in ``portfolio_strategy.py`` is still
in effect as a fallback.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


KNOWN_EVENT_TYPES: frozenset[str] = frozenset({
    "RBI_POLICY",
    "FED_FOMC",
    "BUDGET",
    "CPI_IN",
    "IIP_IN",
    "ELECTION",
    "OTHER",
})

HARD_BLOCK = "HARD_BLOCK"
SOFT_CAUTION = "SOFT_CAUTION"
VALID_SEVERITIES: frozenset[str] = frozenset({HARD_BLOCK, SOFT_CAUTION})


@dataclass(frozen=True)
class EventEntry:
    date: date
    event_type: str
    severity: str


class EventCalendar:
    """Read-only calendar of high-impact macro events.

    The CSV is loaded eagerly on construction (it is small — a few dozen
    rows/year). Failed rows are logged and skipped, not raised: a malformed
    calendar must not crash the strategy runner.
    """

    def __init__(self, csv_path: Path | str = "data/event_days.csv") -> None:
        self.csv_path: Path = Path(csv_path)
        self._by_date: dict[date, list[EventEntry]] = {}
        self._load()

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        if not self.csv_path.exists():
            logger.warning(
                "[EVENT_CAL] %s not found — no hard-block events will fire.",
                self.csv_path,
            )
            return
        unknown_types: set[str] = set()
        row_count = 0
        try:
            with self.csv_path.open("r", newline="") as fh:
                reader = csv.DictReader(
                    (row for row in fh if not row.lstrip().startswith("#"))
                )
                if reader.fieldnames is None:
                    logger.error("[EVENT_CAL] %s has no header row", self.csv_path)
                    return
                # Normalise fieldnames for case-insensitive matching
                normalised = {f.strip().lower(): f for f in reader.fieldnames}
                date_col = normalised.get("date")
                type_col = normalised.get("event_type")
                sev_col = normalised.get("severity")
                if not date_col or not type_col:
                    logger.error(
                        "[EVENT_CAL] %s missing required columns "
                        "(need date, event_type; have %s)",
                        self.csv_path, reader.fieldnames,
                    )
                    return
                for raw in reader:
                    row_count += 1
                    try:
                        entry = self._parse_row(
                            raw, date_col, type_col, sev_col, unknown_types,
                        )
                    except Exception as exc:
                        logger.warning(
                            "[EVENT_CAL] skipping row %d: %s (%s)",
                            row_count, exc, raw,
                        )
                        continue
                    if entry is None:
                        continue
                    self._by_date.setdefault(entry.date, []).append(entry)
        except Exception as exc:  # pragma: no cover
            logger.error("[EVENT_CAL] failed reading %s: %s", self.csv_path, exc)
            return

        if unknown_types:
            logger.info(
                "[EVENT_CAL] loaded with unknown event types: %s",
                sorted(unknown_types),
            )
        logger.info(
            "[EVENT_CAL] loaded %d event days from %s (%d HARD_BLOCK)",
            len(self._by_date), self.csv_path, self.hard_block_count(),
        )

    @staticmethod
    def _parse_row(
        raw: dict[str, str],
        date_col: str,
        type_col: str,
        sev_col: str | None,
        unknown_types: set[str],
    ) -> EventEntry | None:
        raw_date = (raw.get(date_col) or "").strip()
        if not raw_date:
            return None  # blank line
        try:
            parsed_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            parsed_date = datetime.fromisoformat(raw_date).date()
        event_type = (raw.get(type_col) or "OTHER").strip().upper()
        if event_type not in KNOWN_EVENT_TYPES:
            unknown_types.add(event_type)
        severity_raw = (raw.get(sev_col) if sev_col else None) or SOFT_CAUTION
        severity = severity_raw.strip().upper()
        if severity not in VALID_SEVERITIES:
            severity = SOFT_CAUTION
        return EventEntry(parsed_date, event_type, severity)

    # ------------------------------------------------------------------ #
    # Query API
    # ------------------------------------------------------------------ #
    def is_hard_blocked(self, target_date: date) -> tuple[bool, str | None]:
        """Return ``(True, event_type)`` iff any HARD_BLOCK event that day.

        ``event_type`` is the first HARD_BLOCK event found; other events on
        the same day still reduce the score via the existing soft-penalty
        path but do not change the hard-block verdict.
        """
        entries = self._by_date.get(target_date, [])
        for entry in entries:
            if entry.severity == HARD_BLOCK:
                return True, entry.event_type
        return False, None

    def get_events(self, target_date: date) -> list[EventEntry]:
        """Return every event configured for ``target_date`` (may be empty)."""
        return list(self._by_date.get(target_date, []))

    @staticmethod
    def is_friday_for_premium(target_date: date) -> bool:
        """True iff ``target_date`` is a Friday.

        Premium-selling books on Friday carry weekend-gap risk — Monday
        open jumps after event weekends (FOMC decisions, RBI emergencies,
        geopolitical shocks) are the largest single-tick moves in the
        dataset. ``portfolio_strategy.on_tick`` uses this to force-flat
        the premium leg at 14:55 on Fridays.
        """
        return target_date.weekday() == 4  # 0=Mon, 4=Fri

    def hard_block_count(self) -> int:
        """Total number of HARD_BLOCK entries across all dates."""
        return sum(
            1 for entries in self._by_date.values()
            for e in entries if e.severity == HARD_BLOCK
        )

    def __len__(self) -> int:
        return sum(len(entries) for entries in self._by_date.values())


__all__ = [
    "EventCalendar",
    "EventEntry",
    "HARD_BLOCK",
    "SOFT_CAUTION",
    "KNOWN_EVENT_TYPES",
]
