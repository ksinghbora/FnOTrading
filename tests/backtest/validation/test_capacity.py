"""Tests for src/backtest/validation/capacity.py."""

from __future__ import annotations

import pandas as pd
import pytest

from src.backtest.validation.capacity import (
    CapacityUnavailable,
    LOT_MULTIPLES,
    simulate_capacity,
)


# ─── Helpers ─────────────────────────────────────────────────────────


def _buy_fill(
    *,
    qty: int = 75,
    price: float = 100.0,
    bids: list[tuple[float, int]] | None = None,
    asks: list[tuple[float, int]] | None = None,
    spread_half: float = 0.25,
    symbol: str = "NIFTY25APR24000CE",
) -> dict:
    return {
        "tradingsymbol": symbol,
        "transaction_type": "BUY",
        "quantity": qty,
        "average_price": price,
        "spread_half": spread_half,
        "book_snapshot": (
            {"bids": bids or [], "asks": asks or []}
            if (bids or asks)
            else None
        ),
    }


def _sell_fill(
    *,
    qty: int = 75,
    price: float = 101.0,
    bids: list[tuple[float, int]] | None = None,
    asks: list[tuple[float, int]] | None = None,
    spread_half: float = 0.25,
    symbol: str = "NIFTY25APR24000CE",
) -> dict:
    return {
        "tradingsymbol": symbol,
        "transaction_type": "SELL",
        "quantity": qty,
        "average_price": price,
        "spread_half": spread_half,
        "book_snapshot": (
            {"bids": bids or [], "asks": asks or []}
            if (bids or asks)
            else None
        ),
    }


# ─── CapacityUnavailable ─────────────────────────────────────────────


def test_capacity_unavailable_when_no_snapshot():
    """No trade has book_snapshot → unavoidable capacity failure."""
    trades = [
        {
            "tradingsymbol": "X",
            "transaction_type": "BUY",
            "quantity": 75,
            "average_price": 100.0,
            "spread_half": 0.5,
            "book_snapshot": None,
        },
        {
            "tradingsymbol": "X",
            "transaction_type": "SELL",
            "quantity": 75,
            "average_price": 101.0,
            "spread_half": 0.5,
            "book_snapshot": None,
        },
    ]
    with pytest.raises(CapacityUnavailable, match="book_snapshot"):
        simulate_capacity(trades)


def test_capacity_unavailable_for_empty_input():
    with pytest.raises(CapacityUnavailable, match="empty"):
        simulate_capacity([])


def test_capacity_unavailable_when_no_matched_pairs():
    """Only SELL fills (no BUYs to pair with) → CapacityUnavailable."""
    trades = [
        _sell_fill(
            bids=[(99.5, 100)],
            asks=[(100.5, 100)],
            qty=75,
            price=100.0,
            symbol="X",
        ),
        _sell_fill(
            bids=[(99.5, 100)],
            asks=[(100.5, 100)],
            qty=75,
            price=100.0,
            symbol="X",
        ),
    ]
    with pytest.raises(CapacityUnavailable, match="round-trip"):
        simulate_capacity(trades)


# ─── VWAP analytics ──────────────────────────────────────────────────


def test_buy_walk_300_qty_two_levels_exact_vwap():
    """Walk 300 qty across [(100.0, 60), (100.5, 200), (101.0, 100)].
       VWAP = (100*60 + 100.5*200 + 101*40) / 300 = 30140 / 300 = 100.4667.
    """
    asks = [(100.0, 60), (100.5, 200), (101.0, 100)]
    bids = [(99.8, 500), (99.5, 500), (99.0, 500)]

    # Single round trip at 75. Re-walk at 300 should hit our computed VWAP.
    trades = [
        _buy_fill(qty=75, price=100.0, bids=bids, asks=asks),
        _sell_fill(qty=75, price=99.8, bids=bids, asks=asks),
    ]
    df = simulate_capacity(trades, lot_sizes=(300,), tick_size=0.05)
    assert len(df) == 1
    # Compute expected entry price at 300. Exit uses bids for SELL.
    # BUY 300: 100*60 + 100.5*200 + 101*40 = 30140 → VWAP 100.4667
    # SELL 300 (walk bids descending): 99.8*500 enough → VWAP 99.8
    # P&L = 300 * (99.8 - 100.4667) = -200 (approx)
    expected_entry = (100.0 * 60 + 100.5 * 200 + 101.0 * 40) / 300
    expected_exit = 99.8  # entire 300 filled at best bid
    expected_pnl = 300 * (expected_exit - expected_entry)
    assert df["total_pnl"].iloc[0] == pytest.approx(round(expected_pnl, 2), abs=1e-6)


def test_sell_walk_300_qty_bids_descending():
    """Walk SELL 300 across bids [(99.8, 60), (99.5, 200), (99.0, 100)].
       VWAP = (99.8*60 + 99.5*200 + 99.0*40) / 300 = 29878 / 300 = 99.5933.
    """
    asks = [(100.2, 500), (100.5, 500)]
    bids = [(99.8, 60), (99.5, 200), (99.0, 100)]

    # SELL-first round trip (premium selling): SELL entry, BUY exit.
    trades = [
        _sell_fill(qty=75, price=99.8, bids=bids, asks=asks),
        _buy_fill(qty=75, price=100.2, bids=bids, asks=asks),
    ]
    df = simulate_capacity(trades, lot_sizes=(300,), tick_size=0.05)
    # SELL 300 entry: descending bids → VWAP (99.8*60 + 99.5*200 + 99.0*40)/300
    expected_entry = (99.8 * 60 + 99.5 * 200 + 99.0 * 40) / 300
    # BUY 300 exit: ascending asks → 100.2 * 300 / 300 = 100.2 (enough at first)
    expected_exit = 100.2
    # Short: pnl = qty * (entry - exit) — negative (exit above entry).
    expected_pnl = 300 * (expected_entry - expected_exit)
    assert df["total_pnl"].iloc[0] == pytest.approx(round(expected_pnl, 2), abs=1e-6)


def test_smaller_lot_has_smaller_slippage_bps_than_larger():
    """Walk 75 qty stays on level 1; walk 1500 qty walks 3 levels → more bps."""
    asks = [(100.0, 200), (100.5, 500), (101.0, 1000)]
    bids = [(99.5, 200), (99.0, 500), (98.5, 1000)]
    trades = [
        _buy_fill(qty=75, price=100.0, bids=bids, asks=asks),
        _sell_fill(qty=75, price=99.5, bids=bids, asks=asks),
    ]
    df = simulate_capacity(trades, lot_sizes=(75, 1500), tick_size=0.05)
    small = df[df["lot_size"] == 75].iloc[0]
    large = df[df["lot_size"] == 1500].iloc[0]
    # Small lot walks only the first level at 100.0 BUY and 99.5 SELL —
    # same as recorded, so slippage ≈ 0. Large lot walks multiple levels.
    assert small["avg_slippage_bps"] == pytest.approx(0.0, abs=1e-2)
    assert large["avg_slippage_bps"] > 1.0  # clear adverse slippage


def test_lot_size_smaller_than_quantity_improves_fills():
    """Scaling DOWN should improve BUY fills (lower cost)."""
    asks = [(100.0, 40), (100.5, 500)]  # small top level → walks at 75
    bids = [(99.5, 40), (99.0, 500)]
    trades = [
        _buy_fill(qty=75, price=100.0, bids=bids, asks=asks),
        _sell_fill(qty=75, price=99.5, bids=bids, asks=asks),
    ]
    df = simulate_capacity(trades, lot_sizes=(30, 75), tick_size=0.05)
    small = df[df["lot_size"] == 30].iloc[0]
    baseline = df[df["lot_size"] == 75].iloc[0]
    # 30 qty lands entirely on ask level 1 (100.0) and bid level 1 (99.5).
    # 75 qty overflows to level 2 on both sides — worse fills.
    assert small["avg_slippage_bps"] <= baseline["avg_slippage_bps"]


# ─── DataFrame shape ─────────────────────────────────────────────────


def test_default_output_shape():
    asks = [(100.0, 200), (100.5, 500)]
    bids = [(99.5, 200), (99.0, 500)]
    trades = [
        _buy_fill(qty=75, bids=bids, asks=asks),
        _sell_fill(qty=75, price=99.5, bids=bids, asks=asks),
    ]
    df = simulate_capacity(trades)
    assert list(df.columns) == [
        "lot_size",
        "total_pnl",
        "pnl_per_lot",
        "avg_slippage_bps",
        "num_trades_synthetic_fallback",
    ]
    assert list(df["lot_size"]) == list(LOT_MULTIPLES)


def test_fallback_counted_when_some_trades_missing_snapshot():
    """Mix of trades with and without book_snapshot — fallback count ≥ 1."""
    asks = [(100.0, 200)]
    bids = [(99.5, 200)]
    trades = [
        # Pair 1: full snapshot on both sides.
        _buy_fill(
            qty=75, price=100.0, bids=bids, asks=asks,
            symbol="AAA",
        ),
        _sell_fill(
            qty=75, price=99.5, bids=bids, asks=asks,
            symbol="AAA",
        ),
        # Pair 2: no snapshot — should trigger synthetic fallback on BOTH legs.
        {
            "tradingsymbol": "BBB",
            "transaction_type": "BUY",
            "quantity": 75,
            "average_price": 100.0,
            "spread_half": 0.5,
            "book_snapshot": None,
        },
        {
            "tradingsymbol": "BBB",
            "transaction_type": "SELL",
            "quantity": 75,
            "average_price": 101.0,
            "spread_half": 0.5,
            "book_snapshot": None,
        },
    ]
    df = simulate_capacity(trades, lot_sizes=(300,))
    # At 300 qty we expect the second pair (no snapshot) to fall back on
    # both its legs → 2 synthetic legs at that lot size.
    row = df[df["lot_size"] == 300].iloc[0]
    assert row["num_trades_synthetic_fallback"] >= 2


def test_all_snapshot_fallback_count_zero_at_small_lot():
    """When every pair has a snapshot and lot fits in level 1, no fallback."""
    asks = [(100.0, 500)]
    bids = [(99.5, 500)]
    trades = [
        _buy_fill(qty=75, price=100.0, bids=bids, asks=asks),
        _sell_fill(qty=75, price=99.5, bids=bids, asks=asks),
    ]
    df = simulate_capacity(trades, lot_sizes=(75,))
    row = df[df["lot_size"] == 75].iloc[0]
    assert row["num_trades_synthetic_fallback"] == 0


def test_total_pnl_scales_linearly_when_price_does_not_move():
    """At lot sizes entirely within level 1 depth, P&L per lot is constant."""
    asks = [(100.0, 10_000)]
    bids = [(99.5, 10_000)]
    trades = [
        _buy_fill(qty=75, price=100.0, bids=bids, asks=asks),
        _sell_fill(qty=75, price=99.5, bids=bids, asks=asks),
    ]
    df = simulate_capacity(trades, lot_sizes=(75, 300, 1500))
    # All lot sizes fit inside level 1 → P&L per contract is constant.
    per_contract_pnls = (df["total_pnl"] / df["lot_size"]).round(4).unique()
    assert len(per_contract_pnls) == 1


def test_pnl_per_lot_field_consistent():
    asks = [(100.0, 10_000)]
    bids = [(99.5, 10_000)]
    trades = [
        _buy_fill(qty=75, price=100.0, bids=bids, asks=asks),
        _sell_fill(qty=75, price=99.5, bids=bids, asks=asks),
    ]
    df = simulate_capacity(trades, lot_sizes=(150,))
    row = df.iloc[0]
    assert row["pnl_per_lot"] == pytest.approx(row["total_pnl"] / 150, abs=1e-3)


# ─── Multi-symbol pairing ────────────────────────────────────────────


def test_multi_symbol_pairs_are_attributed_independently():
    """Two symbols should pair each BUY with the correct SELL."""
    a_asks = [(100.0, 500)]
    a_bids = [(99.5, 500)]
    b_asks = [(50.0, 500)]
    b_bids = [(49.5, 500)]
    trades = [
        _buy_fill(qty=75, price=100.0, bids=a_bids, asks=a_asks, symbol="AAA"),
        _buy_fill(qty=75, price=50.0, bids=b_bids, asks=b_asks, symbol="BBB"),
        _sell_fill(qty=75, price=99.5, bids=a_bids, asks=a_asks, symbol="AAA"),
        _sell_fill(qty=75, price=49.5, bids=b_bids, asks=b_asks, symbol="BBB"),
    ]
    df = simulate_capacity(trades, lot_sizes=(75,))
    row = df.iloc[0]
    # AAA: 75 * (99.5 - 100.0) = -37.5
    # BBB: 75 * (49.5 - 50.0) = -37.5
    # Total: -75
    assert row["total_pnl"] == pytest.approx(-75.0, abs=0.01)
