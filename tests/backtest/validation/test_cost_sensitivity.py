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
    """n round-trips with varied exit prices — required for non-zero std.

    Apr 30 2026: each round-trip's exit lands on a distinct calendar
    day so the daily-bucketed Sharpe (sensitivity_curve's post-Phase-4
    output) has n entries rather than collapsing to one "day". Pre-fix
    the cost_sensitivity code mis-counted per-trade returns as daily;
    this fixture now models realistic multi-day trade flow.
    """
    from datetime import date as _date, timedelta as _td
    out: list[dict] = []
    base = _date(2025, 1, 1)
    for i in range(n):
        exit_price = 101.0 + (i * 0.1)  # vary to get non-zero std
        ts = (base + _td(days=i)).isoformat() + "T15:25:00+05:30"
        out.extend(
            [
                {
                    "tradingsymbol": "X",
                    "transaction_type": "BUY",
                    "quantity": 75,
                    "average_price": 100.0,
                    "spread_half": spread_half,
                    "fill_timestamp": ts,
                },
                {
                    "tradingsymbol": "X",
                    "transaction_type": "SELL",
                    "quantity": 75,
                    "average_price": exit_price,
                    "spread_half": spread_half,
                    "fill_timestamp": ts,
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


# ─── Apr 30 2026 Phase 4: daily bucketing of round-trip PnLs ──────


def test_round_trip_pairs_emits_exit_dates():
    """Phase 4 contract: ``_round_trip_pnl_pairs`` returns
    ``(pnl, exit_date)`` tuples so the caller can aggregate by day."""
    from src.backtest.validation.cost_sensitivity import _round_trip_pnl_pairs
    trades = [
        {"tradingsymbol": "X", "transaction_type": "BUY",  "quantity": 75,
         "average_price": 100.0, "fill_timestamp": "2025-01-01T09:30:00+05:30"},
        {"tradingsymbol": "X", "transaction_type": "SELL", "quantity": 75,
         "average_price": 101.0, "fill_timestamp": "2025-01-01T15:25:00+05:30"},
        {"tradingsymbol": "X", "transaction_type": "BUY",  "quantity": 75,
         "average_price": 100.0, "fill_timestamp": "2025-01-02T09:30:00+05:30"},
        {"tradingsymbol": "X", "transaction_type": "SELL", "quantity": 75,
         "average_price": 102.0, "fill_timestamp": "2025-01-02T15:25:00+05:30"},
    ]
    pairs = _round_trip_pnl_pairs(trades)
    assert len(pairs) == 2
    pnl_1, date_1 = pairs[0]
    pnl_2, date_2 = pairs[1]
    assert pnl_1 == 75.0   # 75 * (101 - 100)
    assert pnl_2 == 150.0  # 75 * (102 - 100)
    assert date_1 == "2025-01-01"
    assert date_2 == "2025-01-02"


def test_bucket_pnls_by_day_aggregates_same_day():
    """Multiple round-trips on the same day collapse to one bucket."""
    from src.backtest.validation.cost_sensitivity import _bucket_pnls_by_day
    pairs = [
        (100.0, "2025-01-01"),
        (200.0, "2025-01-01"),  # same day → bucket together
        (50.0,  "2025-01-02"),
    ]
    daily = _bucket_pnls_by_day(pairs)
    assert daily == [300.0, 50.0]  # sorted by date


def test_bucket_pnls_by_day_sorted_ascending():
    """Output order must be deterministic ascending so consumers
    treating it as a time series get stable autocorrelation behaviour."""
    from src.backtest.validation.cost_sensitivity import _bucket_pnls_by_day
    pairs = [
        (50.0,  "2025-03-15"),
        (100.0, "2025-01-01"),
        (75.0,  "2025-02-10"),
    ]
    daily = _bucket_pnls_by_day(pairs)
    assert daily == [100.0, 75.0, 50.0]  # Jan, Feb, Mar


def test_bucket_pnls_by_day_handles_empty_input():
    from src.backtest.validation.cost_sensitivity import _bucket_pnls_by_day
    assert _bucket_pnls_by_day([]) == []


def test_bucket_pnls_by_day_treats_missing_date_as_single_bucket():
    """Trades without fill_timestamp bucket under "" — won't crash but
    produces a single-entry curve which calculate_metrics handles via
    the ``returns.std() == 0`` guard."""
    from src.backtest.validation.cost_sensitivity import _bucket_pnls_by_day
    pairs = [
        (50.0, ""),
        (75.0, ""),
        (100.0, "2025-01-15"),
    ]
    daily = _bucket_pnls_by_day(pairs)
    # Empty date sorts before any real date, so ["" entries summed,
    # then 2025-01-15]
    assert daily == [125.0, 100.0]


def test_sensitivity_curve_sharpe_correctly_annualised_post_phase4():
    """Pre-Phase-4 the cost_sensitivity Sharpe was inflated by
    sqrt(252/n_trades) because each round-trip was treated as a
    distinct daily return. With trades spread over n distinct dates
    the post-fix Sharpe should be a proper annualised value
    (mean_daily / std_daily * sqrt(252)).

    This pins the new semantics — a future revert to per-trade
    bucketing would produce a Sharpe that's an order of magnitude
    larger and trip this assertion."""
    from datetime import date, timedelta
    base = date(2025, 1, 1)
    trades: list[dict] = []
    for i in range(60):  # 60 distinct days
        exit_price = 101.0 + (i * 0.05)
        ts = (base + timedelta(days=i)).isoformat() + "T15:25:00+05:30"
        trades.extend([
            {"tradingsymbol": "X", "transaction_type": "BUY",  "quantity": 75,
             "average_price": 100.0, "spread_half": 0.5, "fill_timestamp": ts},
            {"tradingsymbol": "X", "transaction_type": "SELL", "quantity": 75,
             "average_price": exit_price, "spread_half": 0.5, "fill_timestamp": ts},
        ])
    curve = sensitivity_curve(trades)
    sharpe = curve[0.0]["sharpe_ratio"]
    # Pre-Phase-4 this Sharpe was being annualised against ~60 trades
    # treated as ~60 days, with sqrt(252) inflating an already-noisy
    # number. With the fix annualising against 60 actual days the
    # value should be in a reasonable real-world range, not an
    # absurdly-large per-trade-Sharpe number.
    assert sharpe > 0  # genuine positive edge
    # A truly reasonable upper bound: even for a strategy with edge,
    # annualised daily Sharpe stays under ~50 (the world's best
    # strategies live around 3-5). Pre-fix this was ~100+.
    assert sharpe < 50, f"sharpe={sharpe} suggests Phase 4 bucketing regressed"
