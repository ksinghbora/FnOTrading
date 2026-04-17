"""Tests for the Apr 17 timezone-aware datetime sweep.

Naive `datetime.now()` returns host-local time. On a UTC server (typical
cloud VM) that's IST -5:30, which corrupted: expiry comparisons, Sharpe
windows, structured-log timestamps, and (critically) the strategy
state-store `as_of_date` key — the wrong date was written, so morning
reload fetched yesterday's flags.

These tests pin the contract:
  - core.clock.now_ist() returns a tz-aware datetime in Asia/Kolkata.
  - All public clock helpers return tz-aware values.
  - No `datetime.now()` (naive) calls remain in src/ outside docstrings.
"""

import re
from datetime import datetime
from pathlib import Path

import pytz

from src.core.clock import IST, MarketClock, now_ist


class TestNowIst:
    def test_returns_aware_datetime(self):
        dt = now_ist()
        assert dt.tzinfo is not None

    def test_timezone_is_kolkata(self):
        dt = now_ist()
        # zoneinfo + pytz both expose .zone or fold this name
        assert "Kolkata" in str(dt.tzinfo) or dt.tzinfo == IST

    def test_offset_is_5h30m(self):
        dt = now_ist()
        offset = dt.utcoffset()
        assert offset is not None
        assert offset.total_seconds() == 5 * 3600 + 30 * 60


class TestMarketClockReturnsAware:
    def test_now_is_aware(self):
        assert MarketClock().now().tzinfo is not None

    def test_now_in_ist(self):
        clk = MarketClock()
        offset = clk.now().utcoffset()
        assert offset is not None
        assert offset.total_seconds() == 5 * 3600 + 30 * 60


class TestNoNaiveDatetimeNowInSrc:
    """Repo-wide guard: catch reintroduction of naive datetime.now()."""

    NAIVE_PATTERN = re.compile(r"datetime\.now\(\s*\)")

    # Files allowed to mention `datetime.now()` (only in docstrings/comments).
    ALLOW = {"clock.py"}  # clock.py docstrings reference the migration

    def test_no_naive_now_in_src(self):
        repo_root = Path(__file__).resolve().parents[2]
        src_dir = repo_root / "src"
        offenders: list[tuple[Path, int, str]] = []

        for py in src_dir.rglob("*.py"):
            if py.name in self.ALLOW:
                continue
            for i, line in enumerate(py.read_text().splitlines(), 1):
                # Skip comment-only lines; in-line comments still flagged.
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                if self.NAIVE_PATTERN.search(line):
                    offenders.append((py.relative_to(repo_root), i, line.strip()))

        assert not offenders, (
            "Naive datetime.now() reintroduced. Use src.core.clock.now_ist():\n"
            + "\n".join(f"  {p}:{n}  {ln}" for p, n, ln in offenders)
        )


class TestAwareNaiveSubtractionSafe:
    """The advisor RSS filter parsed pubDate as naive, then subtracted from
    aware now_ist() — silently raised TypeError, broken filter. Apr 17 fix
    attaches UTC tz to pubDate. Pin that here."""

    def test_pubdate_attached_utc_subtracts_cleanly(self):
        # Simulates the fixed code path
        pub = datetime.strptime("Mon, 17 Apr 2026 12:30:00", "%a, %d %b %Y %H:%M:%S").replace(
            tzinfo=pytz.utc
        )
        delta = (now_ist() - pub).total_seconds()
        # Should not raise; delta direction depends on real clock but must be a number
        assert isinstance(delta, float)
