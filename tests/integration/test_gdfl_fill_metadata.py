"""End-to-end smoke test: GDFL backtest emits spread_half + book_snapshot.

Runs a tiny 3-day NIFTY backtest in-process and asserts the per-fill
metadata flows through ``BacktestEngine.run`` into ``result["trades"]``
for the cost-sensitivity / capacity validation harness.

Skips gracefully when the GDFL parquet corpus is absent (fresh checkouts,
CI without the recorded data).
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

GDFL_DIR = Path("data/gdfl_snapshots")


def _has_gdfl_data() -> bool:
    if not GDFL_DIR.exists():
        return False
    return any(GDFL_DIR.glob("gdfl_nifty_*.parquet"))


pytestmark = pytest.mark.skipif(
    not _has_gdfl_data(),
    reason="GDFL parquet corpus missing — populate data/gdfl_snapshots/ to run.",
)


def _run(engine, strategy: str, source, start: date, days: int) -> dict:
    return asyncio.run(
        engine.run(
            strategy_name=strategy,
            strategy_params={"underlying": "NIFTY", "quantity_lots": 1},
            num_days=days,
            start_date=start,
            initial_capital=1_000_000,
            market_source=source,
        )
    )


def test_gdfl_backtest_trades_carry_fill_metadata():
    # Import lazily so the skip path doesn't pay for heavy optional deps.
    from src.backtest.engine import BacktestEngine
    from src.backtest.gdfl_market_source import GDFLMarketSource
    from src.market_data.simulator import NIFTY_SPOT_TOKEN

    source = GDFLMarketSource(GDFL_DIR, "NIFTY", NIFTY_SPOT_TOKEN)
    avail = source.available_days()
    if not avail:
        pytest.skip("No GDFL days parsed from parquet filenames.")

    # Strategies vary in entry cadence; scan a few start windows so the
    # test doesn't false-skip just because Sep-24 is a sleepy VIX floor.
    candidates = [
        (date(2024, 11, 1), "short_strangle"),
        (date(2024, 11, 1), "short_straddle"),
        (date(2024, 11, 1), "iron_condor"),
    ]
    engine = BacktestEngine()
    result = None
    trades: list = []
    for start, strategy in candidates:
        if start < avail[0] or start > avail[-1]:
            continue
        result = _run(engine, strategy, source, start, days=3)
        trades = result.get("trades") or []
        if trades:
            break
    if not trades:
        pytest.skip("No trades in any sampled window — regime gates likely blocking.")

    assert result is not None
    assert "trades" in result, "engine result must expose trades list"

    enriched = [
        t for t in trades
        if t.get("spread_half", 0.0) > 0 and t.get("book_snapshot") is not None
    ]
    assert enriched, (
        "At least one trade should carry non-zero spread_half and a "
        f"book_snapshot (saw {len(trades)} trades, "
        f"{sum(1 for t in trades if t.get('spread_half', 0) > 0)} with spread, "
        f"{sum(1 for t in trades if t.get('book_snapshot') is not None)} with snapshot)"
    )

    # Shape check: book_snapshot is a dict with bids/asks lists of
    # (price, size) tuples.
    snap = enriched[0]["book_snapshot"]
    assert set(snap.keys()) == {"bids", "asks"}
    assert isinstance(snap["bids"], list)
    assert isinstance(snap["asks"], list)
    for side_levels in (snap["bids"], snap["asks"]):
        for lvl in side_levels:
            assert len(lvl) == 2
            assert isinstance(lvl[0], (int, float))
            assert isinstance(lvl[1], int)
