"""Tests for chain recorder's data-quality and atomicity guarantees.

Background: the Apr 17 audit found chain CSVs corrupted by:
  - Saturday/Sunday simulator data (recorder ran on closed-market days).
  - All-zero rows when the broker feed was momentarily dead.
  - Append-mode writes that could tear under crash.

These tests pin the contracts that prevent each class of corruption.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from src.market_data.chain_recorder import ChainSnapshotRecorder


# ── Test doubles ────────────────────────────────────────────────────


@dataclass
class _Greeks:
    iv: float = 0.20
    delta: float = 0.5
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0


@dataclass
class _Opt:
    ltp: Decimal = Decimal("0")
    bid_price: Decimal = Decimal("0")
    ask_price: Decimal = Decimal("0")
    oi: int = 0
    volume: int = 0
    greeks: _Greeks | None = field(default_factory=_Greeks)


@dataclass
class _StrikeEntry:
    strike: float
    ce: _Opt | None = None
    pe: _Opt | None = None


@dataclass
class _Chain:
    strikes: list[_StrikeEntry] = field(default_factory=list)


class _FakeChainBuilder:
    def __init__(self, spot: float, strikes: list[_StrikeEntry], expiry: date):
        self._spot_prices = {"NIFTY": Decimal(str(spot))}
        self._chains = {"NIFTY": {expiry: _Chain(strikes=strikes)}}


class _FakeClock:
    def __init__(self, now: datetime, holiday: bool = False):
        self._now = now
        self._holiday = holiday

    def now(self) -> datetime:
        return self._now

    def is_trading_holiday(self, _d: date) -> bool:
        return self._holiday


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def tmp_recorder(tmp_path: Path):
    """Builds a recorder pointed at a temp dir; caller injects builder + clock."""
    def _make(builder, clock):
        rec = ChainSnapshotRecorder(
            chain_builder=builder, clock=clock,
            output_dir=tmp_path, interval_seconds=60, num_strikes=20,
        )
        return rec, tmp_path
    return _make


# ── _take_snapshot: weekend / holiday gate ─────────────────────────


class TestTakeSnapshotWeekendGate:
    """Defense-in-depth: _take_snapshot refuses to write on non-trading days.

    Regression pin for the Apr 18 2026 incident: a recorder process that
    had been running since before the loop-level gate was committed wrote
    chain_2026-04-18.csv on Saturday. The running process was on pre-fix
    code; the loop never filtered the day. A gate inside _take_snapshot
    means the contract survives a stale process, a direct caller, or a
    future refactor that moves the scheduling out of _record_loop.
    """

    def test_refuses_write_on_weekend(self, tmp_recorder, caplog):
        spot = 23000.0
        strikes = [
            _StrikeEntry(
                spot,
                ce=_Opt(ltp=Decimal("100")),
                pe=_Opt(ltp=Decimal("100")),
            ),
        ]
        builder = _FakeChainBuilder(spot, strikes, date(2026, 4, 21))
        # Saturday, Apr 18 2026, 09:15 IST — the actual bug's clock.
        sat_clock = _FakeClock(datetime(2026, 4, 18, 9, 15, 0), holiday=True)
        rec, outdir = tmp_recorder(builder, sat_clock)

        with caplog.at_level("WARNING"):
            rec._take_snapshot(sat_clock.now())

        # Nothing written, counter untouched, warning emitted.
        assert list(outdir.glob("chain_*.csv")) == []
        assert rec._snapshots_today == 0
        assert any("non-trading day" in m for m in caplog.messages)

    def test_refuses_write_on_nse_holiday(self, tmp_recorder, caplog):
        """Weekday NSE holidays also gate (holiday=True, weekday date)."""
        spot = 23000.0
        strikes = [
            _StrikeEntry(
                spot,
                ce=_Opt(ltp=Decimal("100")),
                pe=_Opt(ltp=Decimal("100")),
            ),
        ]
        builder = _FakeChainBuilder(spot, strikes, date(2026, 4, 21))
        # Thursday Apr 2 2026 (weekday=3) — stand-in for a real NSE holiday.
        hol_clock = _FakeClock(datetime(2026, 4, 2, 10, 0, 0), holiday=True)
        rec, outdir = tmp_recorder(builder, hol_clock)

        with caplog.at_level("WARNING"):
            rec._take_snapshot(hol_clock.now())

        assert list(outdir.glob("chain_*.csv")) == []
        assert rec._snapshots_today == 0
        assert any("non-trading day" in m for m in caplog.messages)


# ── _take_snapshot: degraded handling ───────────────────────────────


class TestTakeSnapshotDegraded:
    def test_skips_when_all_legs_zero(self, tmp_recorder, caplog):
        """When every CE+PE has ltp=bid=ask=0, refuse to write."""
        spot = 23000.0
        # 3 strikes, every leg has zero pricing
        strikes = [
            _StrikeEntry(
                strike=spot + (i - 1) * 50,
                ce=_Opt(ltp=Decimal("0"), bid_price=Decimal("0"), ask_price=Decimal("0")),
                pe=_Opt(ltp=Decimal("0"), bid_price=Decimal("0"), ask_price=Decimal("0")),
            )
            for i in range(3)
        ]
        expiry = date(2026, 4, 21)
        builder = _FakeChainBuilder(spot, strikes, expiry)
        clock = _FakeClock(datetime(2026, 4, 17, 10, 0, 0))
        rec, outdir = tmp_recorder(builder, clock)

        with caplog.at_level("WARNING"):
            rec._take_snapshot(clock.now())

        # Nothing written
        assert list(outdir.glob("chain_*.csv")) == []
        # Counter not incremented
        assert rec._snapshots_today == 0
        # Warning emitted
        assert any("DEGRADED" in m for m in caplog.messages)

    def test_writes_when_at_least_one_leg_priceable(self, tmp_recorder):
        spot = 23000.0
        strikes = [
            _StrikeEntry(
                strike=spot,
                ce=_Opt(ltp=Decimal("100"), bid_price=Decimal("0"), ask_price=Decimal("0")),
                pe=_Opt(ltp=Decimal("0"), bid_price=Decimal("99"), ask_price=Decimal("101")),
            ),
        ]
        expiry = date(2026, 4, 21)
        builder = _FakeChainBuilder(spot, strikes, expiry)
        clock = _FakeClock(datetime(2026, 4, 17, 10, 0, 0))
        rec, outdir = tmp_recorder(builder, clock)

        rec._take_snapshot(clock.now())

        files = list(outdir.glob("chain_*.csv"))
        assert len(files) == 1
        assert rec._snapshots_today == 1
        with open(files[0]) as f:
            rows = list(csv.DictReader(f))
        # 1 strike × 2 legs
        assert len(rows) == 2
        assert {r["option_type"] for r in rows} == {"CE", "PE"}

    def test_partial_priceable_warns_but_writes(self, tmp_recorder, caplog):
        """If <50% legs priceable, warn but still write (downstream can decide)."""
        spot = 23000.0
        # 4 strikes × 2 legs = 8 legs. Only 1 priceable → 12.5% < 50%.
        strikes = []
        for i in range(4):
            strikes.append(_StrikeEntry(
                strike=spot + (i - 2) * 50,
                ce=_Opt(ltp=Decimal("0"), bid_price=Decimal("0"), ask_price=Decimal("0")),
                pe=_Opt(ltp=Decimal("0"), bid_price=Decimal("0"), ask_price=Decimal("0")),
            ))
        # Make one leg priceable
        strikes[2].ce = _Opt(ltp=Decimal("50"))
        expiry = date(2026, 4, 21)
        builder = _FakeChainBuilder(spot, strikes, expiry)
        clock = _FakeClock(datetime(2026, 4, 17, 10, 0, 0))
        rec, outdir = tmp_recorder(builder, clock)

        with caplog.at_level("WARNING"):
            rec._take_snapshot(clock.now())

        # Written despite low priceable %
        assert len(list(outdir.glob("chain_*.csv"))) == 1
        assert any("PARTIAL" in m for m in caplog.messages)


# ── _take_snapshot: atomic writes ──────────────────────────────────


class TestAtomicWrite:
    def test_temp_file_does_not_remain(self, tmp_recorder):
        spot = 23000.0
        strikes = [_StrikeEntry(spot, ce=_Opt(ltp=Decimal("100")), pe=_Opt(ltp=Decimal("100")))]
        builder = _FakeChainBuilder(spot, strikes, date(2026, 4, 21))
        clock = _FakeClock(datetime(2026, 4, 17, 10, 0, 0))
        rec, outdir = tmp_recorder(builder, clock)

        rec._take_snapshot(clock.now())

        # No leftover .tmp file
        assert list(outdir.glob("*.tmp")) == []
        # Day file exists
        assert (outdir / "chain_2026-04-17.csv").exists()

    def test_appends_preserve_earlier_rows(self, tmp_recorder):
        spot = 23000.0
        strikes = [_StrikeEntry(spot, ce=_Opt(ltp=Decimal("100")), pe=_Opt(ltp=Decimal("100")))]
        builder = _FakeChainBuilder(spot, strikes, date(2026, 4, 21))
        clock = _FakeClock(datetime(2026, 4, 17, 10, 0, 0))
        rec, outdir = tmp_recorder(builder, clock)

        rec._take_snapshot(clock.now())
        # Advance 1 minute
        clock._now = clock._now + timedelta(minutes=1)
        rec._take_snapshot(clock.now())

        path = outdir / "chain_2026-04-17.csv"
        with open(path) as f:
            rows = list(csv.DictReader(f))
        # 2 snapshots × 1 strike × 2 legs
        assert len(rows) == 4
        # Two distinct timestamps preserved
        assert len({r["time"][:16] for r in rows}) == 2

    def test_does_not_cross_day_boundary(self, tmp_recorder):
        """Each day gets its own file even from the same recorder instance."""
        spot = 23000.0
        strikes = [_StrikeEntry(spot, ce=_Opt(ltp=Decimal("100")), pe=_Opt(ltp=Decimal("100")))]
        builder = _FakeChainBuilder(spot, strikes, date(2026, 4, 21))
        clock = _FakeClock(datetime(2026, 4, 17, 15, 29, 0))
        rec, outdir = tmp_recorder(builder, clock)

        rec._take_snapshot(clock.now())
        # Roll to next morning
        clock._now = datetime(2026, 4, 20, 9, 30, 0)
        rec._take_snapshot(clock.now())

        files = sorted(p.name for p in outdir.glob("chain_*.csv"))
        assert files == ["chain_2026-04-17.csv", "chain_2026-04-20.csv"]
