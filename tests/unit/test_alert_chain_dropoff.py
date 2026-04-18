"""Tests for scripts/alert_chain_dropoff.py — silent WS death detector.

The hourly dropoff alert is the only signal that catches a stuck recorder
between the morning verify and the nightly audit. Its contracts:
  1. _count_rows_up_to_hour() correctly filters by the hour in the time
     column, not by file-position order.
  2. _load_baseline() walks back, skips weekends, returns 0 if no samples.
  3. The "absolute floor" alert path fires regardless of baseline.
  4. The "% drop" alert path fires when today is materially below baseline.
  5. Duplicate alerts for the same (day, hour) are suppressed via state.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
import pytz

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import alert_chain_dropoff as acd  # noqa: E402

IST = pytz.timezone("Asia/Kolkata")


def _at_hour(d: date, hour: int) -> datetime:
    """Pin wall clock to a specific hour on the test date so the alert
    script reads from the same hour our fixtures wrote data at. Without
    this, the test passes when the developer's wall clock happens to be
    in the right window and fails outside it (the original flake)."""
    return IST.localize(datetime(d.year, d.month, d.day, hour, 30, 0))


def _write_chain_csv(path: Path, hours_to_rows: dict[int, int]) -> None:
    """Write a chain CSV with `n` rows at each given hour.

    Time format must match the recorder's ISO format with TZ
    (the function reads chars 11-13 for the hour).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "time", "underlying", "expiry", "strike", "option_type",
            "ltp", "iv", "delta", "gamma", "theta", "vega",
            "oi", "volume", "bid_price", "ask_price",
        ])
        writer.writeheader()
        for hour, count in hours_to_rows.items():
            for i in range(count):
                writer.writerow({
                    "time": f"2026-04-17T{hour:02d}:30:{i % 60:02d}+05:30",
                    "underlying": "NIFTY", "expiry": "2026-04-21",
                    "strike": 24000, "option_type": "CE",
                    "ltp": 100.0, "iv": 0.18,
                    "delta": 0.5, "gamma": 0.001, "theta": -1.0, "vega": 5.0,
                    "oi": 1000, "volume": 100, "bid_price": 99.5, "ask_price": 100.5,
                })


# ── _count_rows_up_to_hour ─────────────────────────────────────────


def test_count_rows_up_to_hour_only_counts_through_cutoff(tmp_path: Path):
    p = tmp_path / "chain_2026-04-17.csv"
    _write_chain_csv(p, {9: 100, 10: 150, 11: 200, 12: 50})
    # cutoff=10 → rows at hours 9 and 10 only (100+150)
    assert acd._count_rows_up_to_hour(p, 10) == 250
    # cutoff=12 → all rows
    assert acd._count_rows_up_to_hour(p, 12) == 500


def test_count_rows_returns_zero_when_file_missing(tmp_path: Path):
    assert acd._count_rows_up_to_hour(tmp_path / "nope.csv", 12) == 0


def test_count_rows_skips_unparseable_time_field(tmp_path: Path):
    p = tmp_path / "chain.csv"
    p.write_text(
        "time,underlying,strike\n"
        "garbage,NIFTY,24000\n"           # too short, skipped
        "2026-04-17TXX:30:00+05:30,NIFTY,24000\n"  # non-numeric hour, skipped
        "2026-04-17T10:30:00+05:30,NIFTY,24000\n"  # valid, counted
    )
    assert acd._count_rows_up_to_hour(p, 23) == 1


# ── _load_baseline ─────────────────────────────────────────────────


def test_load_baseline_averages_recent_trading_days(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(acd, "CHAIN_DIR", tmp_path)

    # April 2026 calendar: 13 Mon, 14 Tue, 15 Wed, 16 Thu, 17 Fri (Good Fri),
    # ... pick days that are actually weekdays AND non-holiday so MarketClock
    # accepts them. Use mid-Mar to avoid GoodFri etc.
    # Today = 2026-03-25 (Wed). Walk back to Mar 24, 23, 20, 19, 18, 17, 16
    # all weekdays. We don't worry about NSE holidays in this synthetic set
    # because the test only requires *some* baseline samples to exist.
    today = date(2026, 3, 25)
    for day in (date(2026, 3, 24), date(2026, 3, 23), date(2026, 3, 20),
                date(2026, 3, 19), date(2026, 3, 18)):
        _write_chain_csv(tmp_path / f"chain_{day.isoformat()}.csv", {12: 6000})

    baseline, samples = acd._load_baseline(today, hour=12, lookback_days=5)
    assert len(samples) == 5
    assert baseline == 6000.0


def test_load_baseline_empty_when_no_history(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(acd, "CHAIN_DIR", tmp_path)
    baseline, samples = acd._load_baseline(date(2026, 3, 25), hour=12)
    assert baseline == 0.0
    assert samples == []


def test_load_baseline_skips_weekend_days(tmp_path: Path, monkeypatch):
    """Saturday/Sunday must not appear in samples even if a CSV exists.

    The recorder's weekday gate prevents this in practice but if a stray
    file exists we still skip it.
    """
    monkeypatch.setattr(acd, "CHAIN_DIR", tmp_path)
    today = date(2026, 3, 25)  # Wed
    # Mar 22 is a Sunday — write data but baseline should ignore it
    _write_chain_csv(tmp_path / "chain_2026-03-22.csv", {12: 99999})
    _write_chain_csv(tmp_path / "chain_2026-03-23.csv", {12: 5000})
    _write_chain_csv(tmp_path / "chain_2026-03-24.csv", {12: 5000})

    baseline, samples = acd._load_baseline(today, hour=12, lookback_days=2)
    assert all(s.day.weekday() < 5 for s in samples), \
        "weekend days must be filtered out of baseline"
    assert baseline == 5000.0


# ── Alert decision ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_absolute_floor_triggers_alert(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(acd, "CHAIN_DIR", tmp_path)
    monkeypatch.setattr(acd, "ALERT_STATE", tmp_path / "state.json")

    # Today has 50 rows (below floor of 100), no baseline
    today = date(2026, 4, 15)  # Wed
    _write_chain_csv(tmp_path / f"chain_{today.isoformat()}.csv", {10: 50})

    # Block actual Telegram send
    sent = []
    async def _capture(msg: str) -> bool:
        sent.append(msg)
        return True
    monkeypatch.setattr(acd, "_send_telegram", _capture)

    # --anytime so we don't get filtered by market hours
    rc = await acd.run(
        today, dry_run=False, skip_market_hours_gate=True, now=_at_hour(today, 10)
    )
    assert rc == 0
    assert sent, "absolute floor should trigger an alert"
    assert "absolute" in sent[0].lower() or "below" in sent[0].lower()


@pytest.mark.asyncio
async def test_pct_drop_triggers_alert(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(acd, "CHAIN_DIR", tmp_path)
    monkeypatch.setattr(acd, "ALERT_STATE", tmp_path / "state.json")

    today = date(2026, 3, 25)  # Wed (no NSE holiday)
    # Baseline ~6000 across recent days
    for d in (date(2026, 3, 23), date(2026, 3, 24)):
        _write_chain_csv(tmp_path / f"chain_{d.isoformat()}.csv", {10: 6000})
    # Today only 2000 — well below 30% drop threshold
    _write_chain_csv(tmp_path / f"chain_{today.isoformat()}.csv", {10: 2000})

    sent = []
    async def _capture(msg: str) -> bool:
        sent.append(msg)
        return True
    monkeypatch.setattr(acd, "_send_telegram", _capture)

    rc = await acd.run(
        today,
        drop_pct=30.0,
        dry_run=False,
        skip_market_hours_gate=True,
        now=_at_hour(today, 10),
    )
    assert rc == 0
    assert sent, "drop > 30%% should trigger an alert"


@pytest.mark.asyncio
async def test_no_alert_when_within_bounds(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(acd, "CHAIN_DIR", tmp_path)
    monkeypatch.setattr(acd, "ALERT_STATE", tmp_path / "state.json")

    today = date(2026, 3, 25)
    for d in (date(2026, 3, 23), date(2026, 3, 24)):
        _write_chain_csv(tmp_path / f"chain_{d.isoformat()}.csv", {10: 6000})
    # Today at 5500 — only 8% below baseline, no alert
    _write_chain_csv(tmp_path / f"chain_{today.isoformat()}.csv", {10: 5500})

    sent = []
    async def _capture(msg: str) -> bool:
        sent.append(msg)
        return True
    monkeypatch.setattr(acd, "_send_telegram", _capture)

    rc = await acd.run(
        today,
        drop_pct=30.0,
        dry_run=False,
        skip_market_hours_gate=True,
        now=_at_hour(today, 10),
    )
    assert rc == 0
    assert not sent, "within-bounds row count should NOT alert"


@pytest.mark.asyncio
async def test_duplicate_alerts_suppressed_per_day_hour(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(acd, "CHAIN_DIR", tmp_path)
    monkeypatch.setattr(acd, "ALERT_STATE", tmp_path / "state.json")

    today = date(2026, 3, 25)
    _write_chain_csv(tmp_path / f"chain_{today.isoformat()}.csv", {10: 50})

    sent = []
    async def _capture(msg: str) -> bool:
        sent.append(msg)
        return True
    monkeypatch.setattr(acd, "_send_telegram", _capture)

    # First call alerts
    await acd.run(
        today, dry_run=False, skip_market_hours_gate=True, now=_at_hour(today, 10)
    )
    assert len(sent) == 1
    # Second call same hour: suppressed
    rc = await acd.run(
        today, dry_run=False, skip_market_hours_gate=True, now=_at_hour(today, 10)
    )
    assert rc == 1
    assert len(sent) == 1, "duplicate alert should be suppressed"


@pytest.mark.asyncio
async def test_force_flag_overrides_suppression(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(acd, "CHAIN_DIR", tmp_path)
    monkeypatch.setattr(acd, "ALERT_STATE", tmp_path / "state.json")

    today = date(2026, 3, 25)
    _write_chain_csv(tmp_path / f"chain_{today.isoformat()}.csv", {10: 50})

    sent = []
    async def _capture(msg: str) -> bool:
        sent.append(msg)
        return True
    monkeypatch.setattr(acd, "_send_telegram", _capture)

    # Pre-poison the state at the same hour we'll pin the wall clock to
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"2026-03-25:10": {"sent_at": "irrelevant"}}))

    rc = await acd.run(
        today,
        dry_run=False,
        force_alert=True,
        skip_market_hours_gate=True,
        now=_at_hour(today, 10),
    )
    assert rc == 0
    assert sent, "--force should bypass dedup state"
