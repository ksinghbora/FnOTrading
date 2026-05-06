"""Tests for the May 2 2026 Indian-market range detectors on RegimeDetector.

Adds ADX(14), Bollinger Band squeeze, and RV/IV ratio — three proven
institutional indicators calibrated for Indian NIFTY/BANKNIFTY 5-min
spot bars. Powers ``is_premium_selling_favorable()``, the unified
hard-gate IC and other premium-selling strategies opt into via
``require_premium_selling_regime=True``.

Calibration (vs US-textbook defaults):
  - ADX threshold 22 (vs US 20) — Indian intraday has more fake-trend noise
  - BB squeeze 25th-pct (vs absolute width) — adapts to per-day vol regime
  - RV/IV < 0.80 (vs US 0.85) — Indian VIX has slightly larger structural premium
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from unittest.mock import MagicMock

import pytest


def _detector():
    """Build a RegimeDetector with mocked dependencies."""
    from src.strategy.regime import RegimeDetector
    feed = MagicMock()
    aggregator = MagicMock()
    chain_builder = MagicMock()
    return RegimeDetector(feed, aggregator, chain_builder)


def _bar(o: float, h: float, l: float, c: float):
    """Construct a minimal candle with .high/.low/.close attrs as Decimals."""
    from decimal import Decimal
    b = MagicMock()
    b.open = Decimal(str(o))
    b.high = Decimal(str(h))
    b.low = Decimal(str(l))
    b.close = Decimal(str(c))
    return b


# ── ADX detector ─────────────────────────────────────────────────────


def test_adx_returns_none_with_insufficient_bars():
    """ADX(14) needs ~30 bars to seed Wilder smoothing properly."""
    d = _detector()
    d._chain_builder._instruments = {1: ("NIFTY", None, 0, None)}
    d._aggregator.get_completed_candles.return_value = [
        _bar(22500, 22510, 22490, 22500) for _ in range(5)
    ]
    # Mock _find_spot_token to return a valid token
    d._find_spot_token = MagicMock(return_value=1)
    assert d.compute_adx("NIFTY") is None


def test_adx_low_value_on_flat_range_bound_market():
    """When highs/lows are tightly clustered, ADX should be < 22 (range)."""
    d = _detector()
    d._find_spot_function = MagicMock(return_value=1)
    d._find_spot_token = MagicMock(return_value=1)
    # 35 bars, NIFTY-like spot oscillating ±5 pts around 22500
    bars = []
    for i in range(35):
        # Tiny oscillation, no trend
        center = 22500.0 + (i % 3 - 1) * 2.0  # ±2 pts
        bars.append(_bar(center - 1, center + 3, center - 3, center))
    d._aggregator.get_completed_candles.return_value = bars
    adx = d.compute_adx("NIFTY")
    assert adx is not None
    assert adx < 22.0, f"Expected ADX<22 on flat range, got {adx:.2f}"


def test_adx_high_value_on_strongly_trending_market():
    """Steady +5 pts/bar trend should produce ADX > 28."""
    d = _detector()
    d._find_spot_token = MagicMock(return_value=1)
    bars = []
    for i in range(35):
        # Bullish trend: each bar makes a higher high and higher low
        base = 22500.0 + i * 5.0
        bars.append(_bar(base, base + 4, base - 1, base + 3))
    d._aggregator.get_completed_candles.return_value = bars
    adx = d.compute_adx("NIFTY")
    assert adx is not None
    assert adx > 28.0, f"Expected ADX>28 on strong trend, got {adx:.2f}"


# ── Bollinger Band squeeze ───────────────────────────────────────────


def test_bb_squeeze_true_during_compression():
    """When recent CLOSES converge tight while history was wide, the
    20-period BB width drops into the bottom quartile → squeeze."""
    d = _detector()
    d._find_spot_token = MagicMock(return_value=1)
    bars = []
    # First 50 bars: closes oscillate ±50 pts → wide BB width
    for i in range(50):
        # cycle through 22450 / 22550 / 22500 to give std ~40 pts
        offset = [-50, 50, -25, 25][i % 4]
        center = 22500.0 + offset
        bars.append(_bar(center, center + 5, center - 5, center))
    # Last 30 bars: closes within ±2 pts → tight BB width
    for i in range(30):
        center = 22500.0 + (i % 2) * 2.0  # 22500 or 22502
        bars.append(_bar(center, center + 0.5, center - 0.5, center))
    d._aggregator.get_completed_candles.return_value = bars
    is_sqz, width = d.compute_bb_squeeze("NIFTY")
    assert is_sqz is True, f"Expected squeeze=True, got width={width}"
    assert width is not None
    assert width < 0.5  # very tight in % terms


def test_bb_squeeze_false_when_current_width_is_at_or_above_median():
    """When recent BB width sits ABOVE the bottom quartile of historical
    widths, the gate must NOT report a squeeze. Inversion of the
    'true' test: tight history + recent expansion = no squeeze now."""
    d = _detector()
    d._find_spot_token = MagicMock(return_value=1)
    bars = []
    # First 50 bars: tight ±2pt closes
    for i in range(50):
        center = 22500.0 + (i % 2) * 2.0
        bars.append(_bar(center, center + 0.5, center - 0.5, center))
    # Last 30 bars: wide ±50pt closes — expansion, not squeeze
    for i in range(30):
        offset = [-50, 50, -25, 25][i % 4]
        center = 22500.0 + offset
        bars.append(_bar(center, center + 5, center - 5, center))
    d._aggregator.get_completed_candles.return_value = bars
    is_sqz, _ = d.compute_bb_squeeze("NIFTY")
    assert is_sqz is False


def test_bb_squeeze_returns_none_with_too_few_bars():
    d = _detector()
    d._find_spot_token = MagicMock(return_value=1)
    d._aggregator.get_completed_candles.return_value = [
        _bar(22500, 22510, 22490, 22500) for _ in range(10)
    ]
    is_sqz, width = d.compute_bb_squeeze("NIFTY")
    assert is_sqz is None
    assert width is None


# ── Realized vol ─────────────────────────────────────────────────────


def test_realized_vol_returns_none_with_insufficient_closes():
    d = _detector()
    # No daily closes captured yet
    assert d.compute_realized_vol("NIFTY") is None


def _compounding_closes(base: float, n: int, daily_pct_step: float):
    """Helper: build n+1 closes where each step is +/- daily_pct_step
    of the PREVIOUS close (so the series compounds, not oscillates
    around an absolute level). Useful for matching annualised RV
    targets in tests."""
    closes = [base]
    for i in range(n):
        sign = 1.0 if i % 2 == 0 else -1.0
        closes.append(closes[-1] * (1.0 + daily_pct_step * sign))
    return closes


def test_realized_vol_computes_on_synthetic_low_vol_series():
    """Daily compounding ±0.1% steps → annualized vol ~ 1.6%."""
    from src.strategy.regime import RV_PERIOD_DAYS
    d = _detector()
    closes = _compounding_closes(22500.0, RV_PERIOD_DAYS, 0.001)  # 0.1%/day
    d._daily_closes["NIFTY"] = __import__("collections").deque(closes, maxlen=30)
    rv = d.compute_realized_vol("NIFTY")
    assert rv is not None
    # ~0.1% daily × sqrt(252) × 100 ≈ 1.6% annualized — synthetic low
    assert 1.0 < rv < 3.0, f"Expected RV ~1.6%, got {rv:.2f}"


def test_realized_vol_high_on_choppy_series():
    """Daily compounding ±2% steps → annualized vol >= 30%."""
    from src.strategy.regime import RV_PERIOD_DAYS
    d = _detector()
    closes = _compounding_closes(22500.0, RV_PERIOD_DAYS, 0.02)
    d._daily_closes["NIFTY"] = __import__("collections").deque(closes, maxlen=30)
    rv = d.compute_realized_vol("NIFTY")
    assert rv is not None
    assert rv >= 30.0, f"Expected RV>=30% on choppy series, got {rv:.2f}"


# ── RV/IV ratio + unified gate ───────────────────────────────────────


def test_rv_iv_ratio_premium_overpriced():
    """RV ~ 12% with VIX 20 → ratio ~ 0.6 → premium overpriced (favourable for IC)."""
    from src.strategy.regime import RV_PERIOD_DAYS
    d = _detector()
    # daily std for 12% annual ≈ 12 / sqrt(252) ≈ 0.756%/day
    closes = _compounding_closes(22500.0, RV_PERIOD_DAYS, 0.00756)
    d._daily_closes["NIFTY"] = __import__("collections").deque(closes, maxlen=30)
    d._get_vix = MagicMock(return_value=20.0)
    ratio = d.compute_rv_iv_ratio("NIFTY")
    assert ratio is not None
    assert 0.4 < ratio < 0.8, f"Expected ratio ~0.6, got {ratio:.2f}"


def test_unified_gate_returns_false_on_insufficient_data():
    """When detectors lack data, the gate must err on the side of 'don't trade'."""
    d = _detector()
    d._find_spot_token = MagicMock(return_value=1)
    d._aggregator.get_completed_candles.return_value = []  # no bars
    d._get_vix = MagicMock(return_value=18.0)
    ok, metrics = d.is_premium_selling_favorable("NIFTY")
    assert ok is False
    assert metrics["reason"] == "insufficient_data"


def test_unified_gate_returns_true_when_all_three_agree():
    """Range-bound spot + tight BB + low RV vs VIX → ALL three pass → gate fires True."""
    from src.strategy.regime import RV_PERIOD_DAYS
    d = _detector()
    d._find_spot_token = MagicMock(return_value=1)
    # Bars: 50 wide, 30 tight — replicates BB squeeze test setup
    bars = []
    for i in range(50):
        offset = [-50, 50, -25, 25][i % 4]
        center = 22500.0 + offset
        bars.append(_bar(center, center + 5, center - 5, center))
    for i in range(30):
        center = 22500.0 + (i % 2) * 2.0
        bars.append(_bar(center, center + 0.5, center - 0.5, center))
    d._aggregator.get_completed_candles.return_value = bars
    # RV ~12% via compounding closes; VIX 20 → ratio ~ 0.6
    closes = _compounding_closes(22500.0, RV_PERIOD_DAYS, 0.00756)
    d._daily_closes["NIFTY"] = __import__("collections").deque(closes, maxlen=30)
    d._get_vix = MagicMock(return_value=20.0)

    ok, metrics = d.is_premium_selling_favorable("NIFTY")
    assert ok is True, f"Expected gate True, got metrics={metrics}"


def test_unified_gate_blocks_when_adx_high():
    """Strong trend (high ADX) blocks entry even if BB and RV/IV are fine."""
    from src.strategy.regime import RV_PERIOD_DAYS
    d = _detector()
    d._find_spot_token = MagicMock(return_value=1)
    # Trending bars → high ADX
    bars = [_bar(22500.0 + i * 5, 22500.0 + i * 5 + 4, 22500.0 + i * 5 - 1, 22500.0 + i * 5 + 3)
            for i in range(70)]
    d._aggregator.get_completed_candles.return_value = bars
    closes = _compounding_closes(22500.0, RV_PERIOD_DAYS, 0.00756)
    d._daily_closes["NIFTY"] = __import__("collections").deque(closes, maxlen=30)
    d._get_vix = MagicMock(return_value=20.0)
    ok, _ = d.is_premium_selling_favorable("NIFTY")
    assert ok is False


def test_unified_gate_blocks_when_rv_iv_above_threshold():
    """High realized vol vs VIX (ratio > 0.80) means IV is fairly priced —
    don't sell premium."""
    from src.strategy.regime import RV_PERIOD_DAYS
    d = _detector()
    d._find_spot_token = MagicMock(return_value=1)
    # Same range-bound bar setup as the favourable-gate test
    bars = []
    for i in range(50):
        offset = [-50, 50, -25, 25][i % 4]
        center = 22500.0 + offset
        bars.append(_bar(center, center + 5, center - 5, center))
    for i in range(30):
        center = 22500.0 + (i % 2) * 2.0
        bars.append(_bar(center, center + 0.5, center - 0.5, center))
    d._aggregator.get_completed_candles.return_value = bars
    # High RV (~30%) via 1.89%/day compounding → ratio 1.5 → unfavourable
    closes = _compounding_closes(22500.0, RV_PERIOD_DAYS, 0.0189)
    d._daily_closes["NIFTY"] = __import__("collections").deque(closes, maxlen=30)
    d._get_vix = MagicMock(return_value=20.0)
    ok, metrics = d.is_premium_selling_favorable("NIFTY")
    assert ok is False
    assert metrics["rv_iv_ratio"] is not None
    assert metrics["rv_iv_ratio"] > 0.80


# ── Daily-close capture (day-rollover semantics) ────────────────────


def test_daily_close_capture_promotes_on_day_rollover():
    """First call of new day promotes prior day's running close to a stored close."""
    d = _detector()
    # Day 1: spot ticks
    d._maybe_capture_daily_close("NIFTY", datetime(2025, 5, 1, 9, 30), 22500.0)
    d._maybe_capture_daily_close("NIFTY", datetime(2025, 5, 1, 10, 0), 22550.0)
    d._maybe_capture_daily_close("NIFTY", datetime(2025, 5, 1, 15, 25), 22580.0)
    # No stored close yet (still day 1)
    assert "NIFTY" not in d._daily_closes or len(d._daily_closes["NIFTY"]) == 0
    # Day 2 first tick — yesterday's last running close promoted
    d._maybe_capture_daily_close("NIFTY", datetime(2025, 5, 2, 9, 30), 22600.0)
    assert "NIFTY" in d._daily_closes
    assert len(d._daily_closes["NIFTY"]) == 1
    assert d._daily_closes["NIFTY"][0] == 22580.0


# ── IronCondor wiring ────────────────────────────────────────────────


def test_ic_param_require_premium_selling_regime_default_false():
    """Default OFF so existing validation reports remain reproducible."""
    from src.strategy.params import IronCondorParams
    p = IronCondorParams()
    assert p.require_premium_selling_regime is False


def test_ic_param_can_be_enabled_via_override():
    """Strong-signal configs opt in via the JSON params override path."""
    from src.strategy.params import IronCondorParams
    p = IronCondorParams.model_validate({
        "require_premium_selling_regime": True,
    })
    assert p.require_premium_selling_regime is True


def test_ic_regime_only_mode_bypasses_legacy_filters():
    """When require_premium_selling_regime=True, the IC entry path
    bypasses the legacy heuristic filters (score, VIX, PCR, max-pain,
    trend) and gates SOLELY on the regime detector + expiry-day safety.

    Pin this contract with an inspection of the entry source — a future
    refactor that re-introduces a legacy filter into the regime-gate
    branch would silently regress the user-driven 'remove everything
    else' simplification."""
    import inspect
    from src.strategy.implementations import iron_condor
    src = inspect.getsource(iron_condor.IronCondorStrategy._try_entry)
    # The regime branch should appear BEFORE the legacy else-branch
    regime_idx = src.find("require_premium_selling_regime")
    else_idx = src.find("else:\n            # ─── Legacy heuristic-filter pipeline")
    assert regime_idx > 0 and else_idx > regime_idx, (
        "Expected regime-only mode to short-circuit before legacy filters; "
        "source layout suggests the bypass was lost"
    )
    # Specifically: score / VIX / PCR / max-pain / trend filter calls
    # must live INSIDE the else branch, not before it.
    score_idx = src.find("entry_score_threshold")
    vix_idx = src.find("_check_vix_filter()")
    pcr_idx = src.find("_check_pcr_filter")
    mp_idx = src.find("_check_max_pain_filter")
    trend_idx = src.find("_check_trend_filter")
    for name, idx in (
        ("score", score_idx), ("vix", vix_idx), ("pcr", pcr_idx),
        ("max-pain", mp_idx), ("trend", trend_idx),
    ):
        assert idx > else_idx, (
            f"Expected legacy {name} filter inside the else-branch (after "
            f"line {else_idx}), but found it at line {idx} (before the "
            f"regime-only short-circuit)"
        )


# ── v2 detectors: Choppiness Index + VRP ─────────────────────────────


def test_choppiness_index_returns_none_with_too_few_bars():
    """CI(14) needs at least 15 bars (period+1 closes for period TR)."""
    d = _detector()
    d._find_spot_token = MagicMock(return_value=1)
    d._aggregator.get_completed_candles.return_value = [
        _bar(22500, 22510, 22490, 22500) for _ in range(5)
    ]
    assert d.compute_choppiness_index("NIFTY") is None


def test_choppiness_index_high_on_choppy_sideways_market():
    """When sum_TR is large but max−min range is small (zig-zag), CI is high.
    Literature: CI >= 61.8 is the Fibonacci range-bound threshold (Dreiss)."""
    d = _detector()
    d._find_spot_token = MagicMock(return_value=1)
    # Build a chop: each bar has 20-pt swing but the overall window range
    # is also ~20 pts (because we keep returning to the middle). Sum_TR
    # will be large (20 × period) but range will be small (~20), giving
    # ratio ~ period and CI close to 100.
    bars = []
    for i in range(20):
        # Alternate up/down 20-pt bars all centred on 22500
        if i % 2 == 0:
            bars.append(_bar(22500, 22510, 22490, 22500))
        else:
            bars.append(_bar(22500, 22510, 22490, 22500))
    d._aggregator.get_completed_candles.return_value = bars
    ci = d.compute_choppiness_index("NIFTY")
    assert ci is not None
    assert ci >= CHOPPINESS_RANGE_THRESHOLD_VAL, f"Expected CI>=61.8 on chop, got {ci:.2f}"


def test_choppiness_index_low_on_strongly_trending_market():
    """Steady directional move: sum_TR ~= total range → CI close to 0.
    Literature: CI <= 38.2 is the Fibonacci trend threshold (Dreiss)."""
    d = _detector()
    d._find_spot_token = MagicMock(return_value=1)
    bars = []
    # +5 pts/bar trend over 20 bars → sum_TR ~ 5 × 14 = 70, range = 5×20 = 100
    # ratio = 0.7, log10(0.7)/log10(14) is negative → clamped to 0
    for i in range(20):
        base = 22500.0 + i * 5.0
        bars.append(_bar(base, base + 5, base, base + 5))
    d._aggregator.get_completed_candles.return_value = bars
    ci = d.compute_choppiness_index("NIFTY")
    assert ci is not None
    assert ci <= CHOPPINESS_TREND_THRESHOLD_VAL, f"Expected CI<=38.2 on trend, got {ci:.2f}"


def test_choppiness_index_handles_degenerate_zero_range():
    """If max_high == min_low across the window (truly flat), CI is None."""
    d = _detector()
    d._find_spot_token = MagicMock(return_value=1)
    # All bars identical: zero range → formula undefined
    bars = [_bar(22500, 22500, 22500, 22500) for _ in range(20)]
    d._aggregator.get_completed_candles.return_value = bars
    ci = d.compute_choppiness_index("NIFTY")
    assert ci is None


def test_vrp_positive_when_vix_exceeds_realized_vol():
    """VRP = VIX − RV. With RV ≈ 12 and VIX = 18, VRP ≈ +6 (favourable)."""
    from src.strategy.regime import RV_PERIOD_DAYS
    d = _detector()
    closes = _compounding_closes(22500.0, RV_PERIOD_DAYS, 0.00756)  # ~12% annualised
    d._daily_closes["NIFTY"] = __import__("collections").deque(closes, maxlen=30)
    d._get_vix = MagicMock(return_value=18.0)
    vrp = d.compute_vrp("NIFTY")
    assert vrp is not None
    # 18 − 12 = +6 with 1pt slack on either side
    assert 4.0 < vrp < 8.0, f"Expected VRP ~+6, got {vrp:.2f}"


def test_vrp_negative_when_realized_exceeds_implied():
    """VIX = 12, RV ≈ 30 → VRP ≈ −18 → unfavourable."""
    from src.strategy.regime import RV_PERIOD_DAYS
    d = _detector()
    closes = _compounding_closes(22500.0, RV_PERIOD_DAYS, 0.0189)  # ~30% annualised
    d._daily_closes["NIFTY"] = __import__("collections").deque(closes, maxlen=30)
    d._get_vix = MagicMock(return_value=12.0)
    vrp = d.compute_vrp("NIFTY")
    assert vrp is not None
    assert vrp < 0.0, f"Expected VRP < 0 when RV > VIX, got {vrp:.2f}"


def test_vrp_returns_none_on_insufficient_history():
    d = _detector()
    d._get_vix = MagicMock(return_value=18.0)
    assert d.compute_vrp("NIFTY") is None


def test_v2_gate_returns_false_on_insufficient_data():
    d = _detector()
    d._find_spot_token = MagicMock(return_value=1)
    d._aggregator.get_completed_candles.return_value = []
    d._get_vix = MagicMock(return_value=18.0)
    ok, metrics = d.is_premium_selling_favorable_v2("NIFTY")
    assert ok is False
    assert metrics["reason"] == "insufficient_data"


def test_v2_gate_passes_when_chop_and_positive_vrp():
    """CI high (sideways) AND VRP > 0 (IV > RV) → gate True."""
    from src.strategy.regime import RV_PERIOD_DAYS
    d = _detector()
    d._find_spot_token = MagicMock(return_value=1)
    # Choppy bars: tight range, high TR — see compute_choppiness_index test
    bars = [_bar(22500, 22510, 22490, 22500) for _ in range(20)]
    d._aggregator.get_completed_candles.return_value = bars
    # RV ~12% via compounding; VIX 18 → VRP +6
    closes = _compounding_closes(22500.0, RV_PERIOD_DAYS, 0.00756)
    d._daily_closes["NIFTY"] = __import__("collections").deque(closes, maxlen=30)
    d._get_vix = MagicMock(return_value=18.0)

    ok, metrics = d.is_premium_selling_favorable_v2("NIFTY")
    assert ok is True, f"Expected v2 gate True, got metrics={metrics}"


def test_v2_gate_blocks_when_trending():
    """Strong trend (low CI) blocks even if VRP is positive."""
    from src.strategy.regime import RV_PERIOD_DAYS
    d = _detector()
    d._find_spot_token = MagicMock(return_value=1)
    bars = []
    for i in range(20):
        base = 22500.0 + i * 5.0
        bars.append(_bar(base, base + 5, base, base + 5))
    d._aggregator.get_completed_candles.return_value = bars
    closes = _compounding_closes(22500.0, RV_PERIOD_DAYS, 0.00756)
    d._daily_closes["NIFTY"] = __import__("collections").deque(closes, maxlen=30)
    d._get_vix = MagicMock(return_value=18.0)
    ok, _ = d.is_premium_selling_favorable_v2("NIFTY")
    assert ok is False


def test_v2_gate_blocks_when_vrp_negative():
    """High realized vol vs IV blocks even if market is choppy."""
    from src.strategy.regime import RV_PERIOD_DAYS
    d = _detector()
    d._find_spot_token = MagicMock(return_value=1)
    bars = [_bar(22500, 22510, 22490, 22500) for _ in range(20)]
    d._aggregator.get_completed_candles.return_value = bars
    # RV ~30%, VIX 12 → VRP −18
    closes = _compounding_closes(22500.0, RV_PERIOD_DAYS, 0.0189)
    d._daily_closes["NIFTY"] = __import__("collections").deque(closes, maxlen=30)
    d._get_vix = MagicMock(return_value=12.0)
    ok, metrics = d.is_premium_selling_favorable_v2("NIFTY")
    assert ok is False
    assert metrics["vrp"] is not None and metrics["vrp"] < 0.0


def test_ic_param_v2_default_false():
    """Default OFF so existing reports remain reproducible."""
    from src.strategy.params import IronCondorParams
    p = IronCondorParams()
    assert p.require_premium_selling_regime_v2 is False


def test_ic_param_v2_can_be_enabled_via_override():
    from src.strategy.params import IronCondorParams
    p = IronCondorParams.model_validate({
        "require_premium_selling_regime_v2": True,
    })
    assert p.require_premium_selling_regime_v2 is True


# Module-level threshold constants for tests (re-imported to keep
# assertions readable; canonical source is src/strategy/regime.py).
from src.strategy.regime import (
    CHOPPINESS_RANGE_THRESHOLD as CHOPPINESS_RANGE_THRESHOLD_VAL,
    CHOPPINESS_TREND_THRESHOLD as CHOPPINESS_TREND_THRESHOLD_VAL,
)
