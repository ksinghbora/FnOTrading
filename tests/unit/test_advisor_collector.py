"""Tests for the AI advisor's data collector (post-JSONL rewrite).

What we're protecting (DATA_RELIABILITY_PLAN §8.2):
  1. JSONL preferred over legacy text — when both exist, JSONL wins.
  2. Date filter on ``ts`` prefix — yesterday's records don't bleed into
     today's totals when the JSONL hasn't been rotated.
  3. ATTRIBUTION records split correctly by ``leg`` (PREMIUM vs TREND).
  4. DAY_SUMMARY uses the LAST record (reset_day_state may run twice).
  5. Malformed lines never crash the parse — they're skipped silently.
  6. Legacy text-log parser still works for replaying historical days.

Why this file exists: the collector was rewritten on Apr 17 to read
JSONL with typed tags instead of regex'ing a free-text log. Before that
rewrite the collector had ZERO test coverage, which made it the riskiest
module in the advisor pipeline. These tests lock the new contract.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src.advisor.collector import (
    DEFAULT_JSONL_PATH,
    _attributions_from_jsonl,
    _day_summary_from_jsonl,
    _entries_from_jsonl,
    _iter_jsonl,
    _records_with_tag,
    collect_today_data,
    parse_attribution_lines,
)
from src.config import Settings
from src.utils.log_tags import Tag


# ── Helpers ────────────────────────────────────────────────────────


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _attr_record(
    leg: str,
    *,
    ts: str = "2026-04-17T15:25:00.000Z",
    delta_pnl: float = 100.0,
    gamma_pnl: float = -20.0,
    theta_pnl: float = 800.0,
    vega_pnl: float = 50.0,
    residual: float = 5.0,
    dominant: str = "THETA",
) -> dict:
    return {
        "ts": ts,
        "level": "INFO",
        "process": "trader",
        "module": "src.strategy.implementations.portfolio_strategy",
        "msg": "exit attribution",
        "tag": "ATTRIBUTION",
        "strategy": "portfolio_1",
        "leg": leg,
        "mode": "STRANGLE",
        "actual_pnl": delta_pnl + gamma_pnl + theta_pnl + vega_pnl + residual,
        "delta_pnl": delta_pnl,
        "gamma_pnl": gamma_pnl,
        "theta_pnl": theta_pnl,
        "vega_pnl": vega_pnl,
        "residual": residual,
        "dominant": dominant,
        "dominant_pct": 75.0,
    }


def _entry_record(leg: str, mode: str = "STRANGLE", direction: str = "") -> dict:
    return {
        "ts": "2026-04-17T09:30:00.000Z",
        "level": "INFO",
        "module": "src.strategy.implementations.portfolio_strategy",
        "msg": "entry",
        "tag": "ENTRY_QUALITY",
        "strategy": "portfolio_1",
        "leg": leg,
        "mode": mode,
        "direction": direction,
        "net_delta": 1.5,
        "net_gamma": 0.001,
        "net_theta": 800.0,
        "net_vega": -120.0,
        "spot": 22500.0,
        "vix": 14.5,
    }


def _day_summary_record(prem_pnl: float = 800, trend_pnl: float = 200, n_p: int = 1, n_t: int = 1) -> dict:
    return {
        "ts": "2026-04-17T15:30:00.000Z",
        "level": "INFO",
        "module": "src.strategy.implementations.portfolio_strategy",
        "msg": "day summary",
        "tag": "DAY_SUMMARY",
        "strategy": "portfolio_1",
        "total_pnl": prem_pnl + trend_pnl,
        "premium_pnl": prem_pnl,
        "premium_trades": n_p,
        "trend_pnl": trend_pnl,
        "trend_trades": n_t,
    }


# ── _iter_jsonl ────────────────────────────────────────────────────


def test_iter_jsonl_filters_by_date_prefix(tmp_path: Path):
    """Only records whose ts starts with target_date prefix come through."""
    path = tmp_path / "trader.jsonl"
    _write_jsonl(path, [
        _attr_record("PREMIUM", ts="2026-04-16T10:00:00.000Z"),  # yesterday
        _attr_record("PREMIUM", ts="2026-04-17T10:00:00.000Z"),  # today
        _attr_record("PREMIUM", ts="2026-04-18T10:00:00.000Z"),  # tomorrow
    ])

    records = _iter_jsonl(path, date(2026, 4, 17))
    assert len(records) == 1
    assert records[0]["ts"].startswith("2026-04-17")


def test_iter_jsonl_skips_malformed_lines(tmp_path: Path):
    """A garbage line in the middle must not abort the parse."""
    path = tmp_path / "trader.jsonl"
    path.write_text(
        json.dumps(_attr_record("PREMIUM")) + "\n"
        "{not valid json\n"
        + json.dumps(_attr_record("TREND")) + "\n"
    )
    records = _iter_jsonl(path, date(2026, 4, 17))
    assert len(records) == 2, "garbage line should be skipped, not abort"


def test_iter_jsonl_handles_missing_file(tmp_path: Path):
    """Missing file returns empty list — collector falls back to legacy."""
    records = _iter_jsonl(tmp_path / "does_not_exist.jsonl", date(2026, 4, 17))
    assert records == []


# ── Helpers operating on records ───────────────────────────────────


def test_records_with_tag_filter():
    records = [
        _attr_record("PREMIUM"),
        _entry_record("PREMIUM"),
        _attr_record("TREND"),
    ]
    assert len(_records_with_tag(records, Tag.ATTRIBUTION)) == 2
    assert len(_records_with_tag(records, Tag.ENTRY_QUALITY)) == 1
    assert len(_records_with_tag(records, Tag.MONITOR)) == 0


def test_attributions_from_jsonl_field_mapping():
    """Each *_pnl field maps to the matching LegAttribution attribute."""
    records = [_attr_record("PREMIUM", delta_pnl=111, gamma_pnl=222, theta_pnl=333, vega_pnl=444, residual=55, dominant="VEGA")]
    attrs = _attributions_from_jsonl(records)
    assert len(attrs) == 1
    a = attrs[0]
    assert a.delta_pnl == 111
    assert a.gamma_pnl == 222
    assert a.theta_pnl == 333
    assert a.vega_pnl == 444
    assert a.residual == 55
    assert a.dominant == "VEGA"


def test_attributions_skip_records_with_bad_types():
    """A record with a non-numeric *_pnl must be dropped, not crash."""
    records = [
        _attr_record("PREMIUM", delta_pnl=100),
        {**_attr_record("PREMIUM"), "delta_pnl": "not-a-number"},
        _attr_record("PREMIUM", delta_pnl=200),
    ]
    attrs = _attributions_from_jsonl(records)
    # The bad record gets dropped; the two good ones survive
    assert len(attrs) == 2
    assert attrs[0].delta_pnl == 100
    assert attrs[1].delta_pnl == 200


def test_day_summary_uses_last_record():
    """If reset_day_state emits twice (rare), use the LATEST values."""
    records = [
        _day_summary_record(prem_pnl=500, trend_pnl=100, n_p=1, n_t=1),
        _day_summary_record(prem_pnl=900, trend_pnl=300, n_p=2, n_t=2),
    ]
    prem, trend, n_p, n_t = _day_summary_from_jsonl(records)
    assert (prem, trend, n_p, n_t) == (900, 300, 2, 2)


def test_day_summary_zero_when_missing():
    """No DAY_SUMMARY record → all zeros, not a crash."""
    assert _day_summary_from_jsonl([_entry_record("PREMIUM")]) == (0.0, 0.0, 0, 0)


def test_entries_use_direction_field_for_trend():
    """TREND entries carry direction (UP/DOWN), not mode."""
    records = [_entry_record("TREND", direction="UP", mode="")]
    entries = _entries_from_jsonl(records)
    assert len(entries) == 1
    assert entries[0]["direction"] == "UP"
    assert entries[0]["leg"] == "TREND"


# ── End-to-end: collect_today_data ──────────────────────────────────


@pytest.mark.asyncio
async def test_collect_today_data_jsonl_path(tmp_path: Path):
    """Full pipeline: JSONL records → TodayData with split attribution."""
    jsonl_path = tmp_path / "trader.jsonl"
    _write_jsonl(jsonl_path, [
        _entry_record("PREMIUM", mode="STRANGLE"),
        _entry_record("TREND", mode="", direction="UP"),
        _attr_record("PREMIUM", delta_pnl=100, theta_pnl=600),
        _attr_record("TREND", delta_pnl=400, theta_pnl=-50),
        _day_summary_record(prem_pnl=700, trend_pnl=350, n_p=1, n_t=1),
    ])

    settings = Settings(strategies="[]")
    data = await collect_today_data(
        target_date=date(2026, 4, 17),
        settings=settings,
        jsonl_path=jsonl_path,
    )

    assert data.date == date(2026, 4, 17)
    assert data.total_pnl == 1050  # 700 + 350
    assert data.premium_leg.pnl == 700
    assert data.premium_leg.mode == "strangle"
    assert data.premium_leg.trades == 1
    assert len(data.premium_leg.attributions) == 1
    assert data.premium_leg.attributions[0].delta_pnl == 100

    assert data.trend_leg.pnl == 350
    assert data.trend_leg.direction == "UP"
    assert data.trend_leg.trades == 1
    assert len(data.trend_leg.attributions) == 1
    assert data.trend_leg.attributions[0].delta_pnl == 400

    assert data.trades_count == 2


@pytest.mark.asyncio
async def test_collect_today_data_jsonl_wins_over_legacy(tmp_path: Path):
    """When BOTH sources exist for the date, JSONL records take precedence."""
    jsonl_path = tmp_path / "trader.jsonl"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    # JSONL says premium pnl = 999
    _write_jsonl(jsonl_path, [_day_summary_record(prem_pnl=999, trend_pnl=0)])
    # Legacy log says premium pnl = 111 (we should NOT see this)
    (log_dir / "trader-20260417.log").write_text(
        "2026-04-17 15:30 INFO [DAY_SUMMARY] strategy=portfolio_1 "
        "total_pnl=+111 premium=+111(+100%) trades=1 trend=+0(+0%) trades=0\n"
    )

    data = await collect_today_data(
        target_date=date(2026, 4, 17),
        log_dir=log_dir,
        settings=Settings(strategies="[]"),
        jsonl_path=jsonl_path,
    )
    assert data.premium_leg.pnl == 999, "JSONL must win when both exist"


@pytest.mark.asyncio
async def test_collect_today_data_falls_back_to_legacy(tmp_path: Path):
    """When JSONL has no records for the date, fall back to legacy text parser.

    This is the path that lets us replay historical days that predate the
    JSONL sink — without it we'd lose ability to run the advisor on
    pre-Apr-17 data.
    """
    jsonl_path = tmp_path / "trader.jsonl"  # never created — exists() False
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "trader-20260417.log").write_text(
        "2026-04-17 15:30 INFO [DAY_SUMMARY] strategy=portfolio_1 "
        "total_pnl=+1234 premium=+800(+65%) trades=2 trend=+434(+35%) trades=1\n"
    )

    data = await collect_today_data(
        target_date=date(2026, 4, 17),
        log_dir=log_dir,
        settings=Settings(strategies="[]"),
        jsonl_path=jsonl_path,
    )
    assert data.premium_leg.pnl == 800
    assert data.trend_leg.pnl == 434
    assert data.premium_leg.trades == 2


@pytest.mark.asyncio
async def test_collect_today_data_no_sources(tmp_path: Path):
    """Empty everything → empty TodayData with sensible defaults, no crash."""
    data = await collect_today_data(
        target_date=date(2026, 4, 17),
        settings=Settings(strategies="[]"),
        jsonl_path=tmp_path / "missing.jsonl",
    )
    assert data.total_pnl == 0
    assert data.premium_leg.pnl == 0
    assert data.trend_leg.pnl == 0
    assert data.premium_leg.attributions == []
    assert data.trend_leg.attributions == []


@pytest.mark.asyncio
async def test_collect_today_data_yesterdays_records_filtered_out(tmp_path: Path):
    """A JSONL with both yesterday and today must only count today.

    Without the date filter the rolling JSONL file would let yesterday's
    P&L double-count into today's advisory — silent and devastating.
    """
    jsonl_path = tmp_path / "trader.jsonl"
    _write_jsonl(jsonl_path, [
        _day_summary_record(prem_pnl=5000, trend_pnl=5000) | {"ts": "2026-04-16T15:30:00.000Z"},
        _day_summary_record(prem_pnl=100, trend_pnl=50)   | {"ts": "2026-04-17T15:30:00.000Z"},
    ])

    data = await collect_today_data(
        target_date=date(2026, 4, 17),
        settings=Settings(strategies="[]"),
        jsonl_path=jsonl_path,
    )
    assert data.total_pnl == 150, "yesterday's P&L must NOT bleed into today's totals"


# ── Legacy parser sanity (kept thin — full coverage isn't the point) ──


def test_legacy_attribution_parser_still_works():
    lines = [
        "2026-04-17 15:30 INFO [ATTRIBUTION] strategy=portfolio_1 leg=PREMIUM mode=STRANGLE "
        "pnl=+800 spot_chg=+10 vix_chg=+0.5 held=5.0hrs delta=+100 gamma=-20 theta=+700 "
        "vega=+50 residual=-30 dominant=THETA(85%)"
    ]
    attrs = parse_attribution_lines(lines)
    assert len(attrs) == 1
    assert attrs[0].dominant == "THETA"
    assert attrs[0].delta_pnl == 100


def test_default_jsonl_path_constant():
    """If this constant ever moves, scripts/morning_advisor.py likely needs to follow."""
    assert DEFAULT_JSONL_PATH == Path("data/logs/trader.jsonl")
