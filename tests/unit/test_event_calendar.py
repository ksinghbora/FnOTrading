"""Tests for the event-day calendar (roadmap P1 #11, #12).

Verifies:
1. A known HARD_BLOCK event returns ``(True, event_type)``.
2. SOFT_CAUTION events pass the hard-block check but are still listed.
3. ``is_friday_for_premium`` maps Mon-Fri to the right weekday.
4. Malformed rows / missing columns don't raise — loader logs and skips.
5. The bundled ``data/event_days.csv`` covers the expected Indian high-impact
   days (sanity check of curated content).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from textwrap import dedent

import pytest

from src.strategy.event_calendar import (
    HARD_BLOCK,
    SOFT_CAUTION,
    EventCalendar,
    EventEntry,
)


class TestHardBlockLookup:
    def test_known_event_returns_type(self, tmp_path: Path) -> None:
        csv = tmp_path / "events.csv"
        csv.write_text(
            dedent(
                """\
                date,event_type,severity
                2025-02-01,BUDGET,HARD_BLOCK
                2025-04-09,RBI_POLICY,HARD_BLOCK
                2025-02-12,CPI_IN,SOFT_CAUTION
                """
            )
        )
        cal = EventCalendar(csv_path=csv)
        hard, etype = cal.is_hard_blocked(date(2025, 2, 1))
        assert hard is True
        assert etype == "BUDGET"
        hard, etype = cal.is_hard_blocked(date(2025, 4, 9))
        assert hard is True
        assert etype == "RBI_POLICY"

    def test_soft_caution_does_not_hard_block(self, tmp_path: Path) -> None:
        csv = tmp_path / "events.csv"
        csv.write_text(
            dedent(
                """\
                date,event_type,severity
                2025-02-12,CPI_IN,SOFT_CAUTION
                """
            )
        )
        cal = EventCalendar(csv_path=csv)
        hard, etype = cal.is_hard_blocked(date(2025, 2, 12))
        assert hard is False
        assert etype is None
        events = cal.get_events(date(2025, 2, 12))
        assert len(events) == 1
        assert events[0].severity == SOFT_CAUTION

    def test_unknown_date_returns_false(self, tmp_path: Path) -> None:
        csv = tmp_path / "events.csv"
        csv.write_text("date,event_type,severity\n2025-02-01,BUDGET,HARD_BLOCK\n")
        cal = EventCalendar(csv_path=csv)
        hard, etype = cal.is_hard_blocked(date(2099, 1, 1))
        assert hard is False
        assert etype is None

    def test_multiple_events_one_day_prefers_hard_block(self, tmp_path: Path) -> None:
        csv = tmp_path / "events.csv"
        csv.write_text(
            dedent(
                """\
                date,event_type,severity
                2025-04-09,CPI_IN,SOFT_CAUTION
                2025-04-09,RBI_POLICY,HARD_BLOCK
                """
            )
        )
        cal = EventCalendar(csv_path=csv)
        hard, etype = cal.is_hard_blocked(date(2025, 4, 9))
        assert hard is True
        assert etype == "RBI_POLICY"


class TestFridayDetection:
    def test_friday_is_true(self) -> None:
        assert EventCalendar.is_friday_for_premium(date(2026, 4, 24)) is True  # Friday

    @pytest.mark.parametrize(
        "d",
        [
            date(2026, 4, 20),  # Monday
            date(2026, 4, 21),  # Tuesday
            date(2026, 4, 22),  # Wednesday
            date(2026, 4, 23),  # Thursday
            date(2026, 4, 25),  # Saturday
            date(2026, 4, 26),  # Sunday
        ],
    )
    def test_non_friday_is_false(self, d: date) -> None:
        assert EventCalendar.is_friday_for_premium(d) is False


class TestCSVRobustness:
    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        missing = tmp_path / "absent.csv"
        cal = EventCalendar(csv_path=missing)
        assert len(cal) == 0
        hard, etype = cal.is_hard_blocked(date(2025, 2, 1))
        assert hard is False and etype is None

    def test_missing_columns_return_empty(self, tmp_path: Path) -> None:
        """If the CSV is missing the required date or event_type column,
        the loader logs and returns an empty calendar rather than raising.

        This is the behaviour requested by the roadmap — a corrupt calendar
        must not break the strategy runner.
        """
        csv = tmp_path / "bad.csv"
        csv.write_text("foo,bar\n1,2\n")
        cal = EventCalendar(csv_path=csv)
        assert len(cal) == 0
        hard, _ = cal.is_hard_blocked(date(2025, 2, 1))
        assert hard is False

    def test_comment_and_blank_lines_are_skipped(self, tmp_path: Path) -> None:
        csv = tmp_path / "events.csv"
        csv.write_text(
            dedent(
                """\
                # This is a comment at the top
                date,event_type,severity
                # an inner comment
                2025-02-01,BUDGET,HARD_BLOCK

                2025-02-12,CPI_IN,SOFT_CAUTION
                """
            )
        )
        cal = EventCalendar(csv_path=csv)
        assert len(cal) == 2

    def test_malformed_row_is_skipped_not_raised(self, tmp_path: Path) -> None:
        csv = tmp_path / "events.csv"
        csv.write_text(
            dedent(
                """\
                date,event_type,severity
                not-a-date,BUDGET,HARD_BLOCK
                2025-02-01,BUDGET,HARD_BLOCK
                """
            )
        )
        cal = EventCalendar(csv_path=csv)
        assert len(cal) == 1
        hard, etype = cal.is_hard_blocked(date(2025, 2, 1))
        assert hard is True
        assert etype == "BUDGET"

    def test_missing_severity_column_defaults_to_soft(self, tmp_path: Path) -> None:
        """Severity column is optional — missing → SOFT_CAUTION default."""
        csv = tmp_path / "events.csv"
        csv.write_text("date,event_type\n2025-02-12,CPI_IN\n")
        cal = EventCalendar(csv_path=csv)
        events = cal.get_events(date(2025, 2, 12))
        assert len(events) == 1
        assert events[0].severity == SOFT_CAUTION
        hard, _ = cal.is_hard_blocked(date(2025, 2, 12))
        assert hard is False


class TestBundledCalendar:
    """Sanity checks on the curated ``data/event_days.csv`` shipped in
    the repo. These lock in the minimum coverage the roadmap requires:
    Budget days, quarterly RBI, key FOMC dates.
    """

    def _repo_calendar(self) -> EventCalendar:
        repo_root = Path(__file__).resolve().parents[2]
        return EventCalendar(csv_path=repo_root / "data" / "event_days.csv")

    def test_union_budget_days_are_hard_blocked(self) -> None:
        cal = self._repo_calendar()
        if len(cal) == 0:
            pytest.skip("bundled event_days.csv not present in this worktree")
        for y in (2024, 2025, 2026):
            hard, etype = cal.is_hard_blocked(date(y, 2, 1))
            assert hard, f"Feb 1 {y} should be HARD_BLOCK (Union Budget)"
            assert etype == "BUDGET"

    def test_rbi_2025_decisions_are_hard_blocked(self) -> None:
        cal = self._repo_calendar()
        if len(cal) == 0:
            pytest.skip("bundled event_days.csv not present in this worktree")
        # Apr 9 2025 — confirmed RBI MPC decision day (first FY26 meeting)
        hard, etype = cal.is_hard_blocked(date(2025, 4, 9))
        assert hard is True
        assert etype == "RBI_POLICY"

    def test_hard_block_count_is_nontrivial(self) -> None:
        cal = self._repo_calendar()
        if len(cal) == 0:
            pytest.skip("bundled event_days.csv not present in this worktree")
        # Quarterly RBI × 3yrs + Budget × 3yrs + 2024 election = 22+ HARD_BLOCKs
        assert cal.hard_block_count() >= 20
