"""Tests for src/backtest/validation/cost_sensitivity.py."""

from __future__ import annotations

import logging

import pytest

from src.backtest.validation.cost_sensitivity import (
    DEFAULT_SHIFTS,
    sensitivity_accepted,
    sensitivity_curve,
    shift_fill_pnl,
)


# ─── Helpers ─────────────────────────────────────────────────────────


def _trade(**overrides) -> dict:
    base = {
        "tradingsymbol": "NIFTY25C24000CE",
        "transaction_type": "BUY",
        "quantity": 75,
        "average_price": 100.0,
        "spread_half": 0.5,
    }
    base.update(overrides)
    return base


# ─── shift_fill_pnl ──────────────────────────────────────────────────


def test_shift_zero_returns_same_price():
    trade = _trade(average_price=100.0, spread_half=0.5, transaction_type="BUY")
    shifted = shift_fill_pnl(trade, 0.0)
    assert shifted["average_price"] == pytest.approx(100.0, abs=1e-9)


def test_shift_does_not_mutate_input():
    trade = _trade(average_price=100.0, spread_half=0.5, transaction_type="BUY")
    _ = shift_fill_pnl(trade, 0.5)
    # Original trade untouched.
    assert trade["average_price"] == 100.0
    assert trade["spread_half"] == 0.5


def test_shift_buy_positive_raises_price():
    trade = _trade(average_price=100.0, spread_half=0.5, transaction_type="BUY")
    shifted = shift_fill_pnl(trade, 0.5)
    # 100 + 1.0 * 0.5 * 0.5 = 100.25
    assert shifted["average_price"] == pytest.approx(100.25, abs=1e-9)
    assert shifted["average_price"] > trade["average_price"]


def test_shift_sell_positive_lowers_price():
    trade = _trade(average_price=100.0, spread_half=0.5, transaction_type="SELL")
    shifted = shift_fill_pnl(trade, 0.5)
    # 100 + (-1.0) * 0.5 * 0.5 = 99.75
    assert shifted["average_price"] == pytest.approx(99.75, abs=1e-9)
    assert shifted["average_price"] < trade["average_price"]


def test_shift_buy_negative_lowers_price_beneficial():
    trade = _trade(average_price=100.0, spread_half=0.5, transaction_type="BUY")
    shifted = shift_fill_pnl(trade, -0.5)
    # 100 + 1.0 * 0.5 * (-0.5) = 99.75
    assert shifted["average_price"] == pytest.approx(99.75, abs=1e-9)


def test_shift_lower_case_side_is_accepted():
    trade = _trade(transaction_type="buy", average_price=100.0, spread_half=0.5)
    shifted = shift_fill_pnl(trade, 0.5)
    assert shifted["average_price"] == pytest.approx(100.25, abs=1e-9)


def test_shift_unknown_side_warns_and_returns_copy(caplog):
    trade = _trade(transaction_type="WHAT", average_price=100.0, spread_half=0.5)
    with caplog.at_level(logging.WARNING, logger="src.backtest.validation.cost_sensitivity"):
        shifted = shift_fill_pnl(trade, 0.5)
    assert shifted is not trade
    assert shifted["average_price"] == 100.0
    assert any("unknown transaction_type" in r.message for r in caplog.records)


def test_shift_missing_spread_half_uses_fallback():
    """Trades predating Phase 2A have no spread_half. Use 50 bps of avg."""
    trade = {
        "tradingsymbol": "NIFTY25C24000CE",
        "transaction_type": "BUY",
        "quantity": 75,
        "average_price": 200.0,
        # no spread_half
    }
    shifted = shift_fill_pnl(trade, 1.0)
    # fallback spread_half = 0.005 * 200 = 1.0; shift 1.0 → +1.0
    assert shifted["average_price"] == pytest.approx(201.0, abs=1e-9)


def test_shift_missing_everything_warns_and_returns_copy(caplog):
    trade = {
        "tradingsymbol": "NIFTY25C24000CE",
        "transaction_type": "BUY",
        "quantity": 75,
        # no spread_half, no average_price
    }
    with caplog.at_level(logging.WARNING, logger="src.backtest.validation.cost_sensitivity"):
        shifted = shift_fill_pnl(trade, 0.5)
    assert shifted is not trade
    assert any("no spread_half" in r.message for r in caplog.records)


def test_shift_sell_clamps_non_negative():
    """A huge negative shift on a SELL must not drive fill price below 0."""
    trade = _trade(transaction_type="SELL", average_price=0.25, spread_half=10.0)
    shifted = shift_fill_pnl(trade, -5.0)
    # naive: 0.25 - (-1.0) * 10.0 * (-5.0) = 0.25 - 50 = -49.75. Clamped.
    assert shifted["average_price"] > 0


def test_hand_computed_curve_five_trades():
    """Hand-compute shifted prices for 5 trades at +50% and verify."""
    trades = [
        _trade(transaction_type="BUY", average_price=100.0, spread_half=0.5),
        _trade(transaction_type="SELL", average_price=100.0, spread_half=0.5),
        _trade(transaction_type="BUY", average_price=200.0, spread_half=1.0),
        _trade(transaction_type="SELL", average_price=200.0, spread_half=1.0),
        _trade(transaction_type="BUY", average_price=50.0, spread_half=0.25),
    ]
    expected_at_half = [100.25, 99.75, 200.5, 199.5, 50.125]
    for trade, exp in zip(trades, expected_at_half):
        shifted = shift_fill_pnl(trade, 0.5)
        assert shifted["average_price"] == pytest.approx(exp, abs=1e-9)


# ─── sensitivity_curve ────────────────────────────────────────────────


def _round_trip_pair(buy_price: float, sell_price: float, spread_half: float) -> list[dict]:
    """Make a single BUY/SELL pair — the minimum unit of a completed trade."""
    return [
        {
            "tradingsymbol": "X",
            "transaction_type": "BUY",
            "quantity": 75,
            "average_price": buy_price,
            "spread_half": spread_half,
        },
        {
            "tradingsymbol": "X",
            "transaction_type": "SELL",
            "quantity": 75,
            "average_price": sell_price,
            "spread_half": spread_half,
        },
    ]


def test_sensitivity_curve_returns_all_default_shifts():
    trades = _round_trip_pair(100.0, 101.0, 0.5) * 5  # 5 winning pairs
    curve = sensitivity_curve(trades)
    assert set(float(k) for k in curve.keys()) == set(float(s) for s in DEFAULT_SHIFTS)


def test_sensitivity_curve_degrades_as_spread_widens():
    """Wider spread (shift > 0) should hurt P&L on a profitable strategy."""
    # 10 BUY→SELL round trips each making +1 point.
    trades = _round_trip_pair(100.0, 101.0, 0.5) * 10
    curve = sensitivity_curve(trades)
    pnl_zero = curve[0.0]["total_pnl"]
    pnl_half = curve[0.5]["total_pnl"]
    pnl_one = curve[1.0]["total_pnl"]
    assert pnl_zero > pnl_half > pnl_one


def test_sensitivity_curve_improves_as_spread_narrows():
    """Negative shift (more favorable fills) should improve P&L."""
    trades = _round_trip_pair(100.0, 101.0, 0.5) * 10
    curve = sensitivity_curve(trades)
    pnl_zero = curve[0.0]["total_pnl"]
    pnl_neg_half = curve[-0.5]["total_pnl"]
    assert pnl_neg_half > pnl_zero


# ─── sensitivity_accepted ────────────────────────────────────────────


def _varied_winning_trades(n: int = 20, spread_half: float = 0.5) -> list[dict]:
    """n round-trips with varied exit prices — required for non-zero std."""
    out: list[dict] = []
    for i in range(n):
        exit_price = 101.0 + (i * 0.1)  # vary to get non-zero std
        out.extend(
            [
                {
                    "tradingsymbol": "X",
                    "transaction_type": "BUY",
                    "quantity": 75,
                    "average_price": 100.0,
                    "spread_half": spread_half,
                },
                {
                    "tradingsymbol": "X",
                    "transaction_type": "SELL",
                    "quantity": 75,
                    "average_price": exit_price,
                    "spread_half": spread_half,
                },
            ]
        )
    return out


def test_accepted_true_when_sharpe_positive_at_plus_half():
    # Varied winning trades → positive mean, non-zero std → Sharpe > 0.
    trades = _varied_winning_trades(n=30, spread_half=0.5)
    curve = sensitivity_curve(trades)
    # Sanity: +0.5 shift is small vs the gross edge so Sharpe stays > 0.
    assert curve[0.5]["sharpe_ratio"] > 0
    assert sensitivity_accepted(curve, threshold_shift=0.5) is True


def test_accepted_false_when_sharpe_zero_or_negative_at_plus_half():
    """Shift wipes out the edge entirely — gate should reject.

    Round trip gross = sell_price - buy_price. Per-leg cost at +0.5 shift
    is spread_half * 0.5. Round-trip cost = 2 * spread_half * 0.5 = spread_half.
    Pick parameters where cost > gross edge so P&L flips decisively.
    """
    # Edge = 0.2 per round trip (before shift). spread_half = 1.0 per leg.
    # At +0.5 shift: cost per round trip = 2*1.0*0.5 = 1.0 > 0.2 edge → loss.
    trades = []
    for i in range(30):
        exit_price = 100.2 + (i % 3) * 0.05  # small variation
        trades.extend(
            [
                {
                    "tradingsymbol": "X",
                    "transaction_type": "BUY",
                    "quantity": 75,
                    "average_price": 100.0,
                    "spread_half": 1.0,
                },
                {
                    "tradingsymbol": "X",
                    "transaction_type": "SELL",
                    "quantity": 75,
                    "average_price": exit_price,
                    "spread_half": 1.0,
                },
            ]
        )
    curve = sensitivity_curve(trades)
    assert curve[0.5]["sharpe_ratio"] <= 0
    assert sensitivity_accepted(curve, threshold_shift=0.5) is False


def test_accepted_tolerant_to_float_keys():
    """Pass a shift near but not exactly 0.5 — should still find the bucket."""
    trades = _varied_winning_trades(n=30, spread_half=0.5)
    curve = sensitivity_curve(trades)
    # The curve's actual key is 0.5 — test we accept a slightly different one.
    assert sensitivity_accepted(curve, threshold_shift=0.5000000001) is True


def test_accepted_empty_curve_is_false():
    assert sensitivity_accepted({}) is False


# ─── Integration ─────────────────────────────────────────────────────


def test_missing_spread_half_uses_fallback_in_curve():
    """Curve should still compute when trades have no spread_half."""
    trades = [
        {
            "tradingsymbol": "X",
            "transaction_type": "BUY",
            "quantity": 75,
            "average_price": 100.0,
        },
        {
            "tradingsymbol": "X",
            "transaction_type": "SELL",
            "quantity": 75,
            "average_price": 101.0,
        },
    ] * 5
    curve = sensitivity_curve(trades)
    assert 0.5 in curve
    # No crash — verify metrics shape is intact.
    assert "sharpe_ratio" in curve[0.5]
