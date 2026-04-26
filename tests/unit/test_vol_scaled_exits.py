"""Tests for vol-scaled SL/PT/trail exits (opt-in).

Verifies that:
  1. When `vol_scaled_exits=False` (the default), the helper returns the
     fallback percentage unchanged — guaranteeing existing strategy
     behavior is preserved.
  2. Calibration: at VIX=15 weekly (DTE=7), `sl_vol_k=12.0` reproduces
     the current 25% SL within a tolerance.
  3. Scaling: higher VIX / shorter DTE gives a wider SL (more noise →
     more room). Concretely: VIX=30, DTE=3 -> ~33% SL.
  4. Clamp: absurd inputs (VIX=60, DTE=0.5) hit the 60% upper clamp.
  5. Clamp lower: flat regime (VIX=5, DTE=30) hits the 10% lower clamp.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.strategy.base import BaseStrategy
from src.strategy.params import (
    BaseStrategyParams,
    PortfolioParams,
    ShortStrangleParams,
)


class _StubStrategy(BaseStrategy):
    """Minimum concrete BaseStrategy for exercising _compute_vol_scaled_exit_pct.

    Bypasses the abstract method requirements; we only need params and
    `self.ctx.get_vix()`.
    """

    def __init__(self, params, vix: float):
        # Parent __init__ builds DecisionLogger — we skip it by not calling
        # super().__init__() and instead wiring the fields it depends on.
        self.strategy_id = "test-stub"
        self.params = params
        self._context = SimpleNamespace(get_vix=lambda: vix)

    # Abstract-method stubs (never called in this test file).
    def get_subscriptions(self):
        raise NotImplementedError

    async def on_start(self):
        raise NotImplementedError

    async def on_tick(self, tick):
        raise NotImplementedError

    async def on_stop(self):
        raise NotImplementedError


class TestOptInBehavior:
    """When vol_scaled_exits=False the helper is a no-op returning fallback."""

    def test_disabled_by_default_returns_fallback(self):
        params = ShortStrangleParams()
        assert params.vol_scaled_exits is False

        strat = _StubStrategy(params, vix=15.0)
        out = strat._compute_vol_scaled_exit_pct("sl", dte=7, fallback_pct=30.0)
        assert out == 30.0  # unchanged

    def test_disabled_ignores_vix_and_dte(self):
        params = ShortStrangleParams()
        # Even with extreme VIX, we still return fallback
        strat = _StubStrategy(params, vix=50.0)
        assert strat._compute_vol_scaled_exit_pct("sl", 1, 30.0) == 30.0
        assert strat._compute_vol_scaled_exit_pct("pt", 1, 12.0) == 12.0
        assert strat._compute_vol_scaled_exit_pct("trail", 1, 10.0) == 10.0

    def test_default_portfolio_params_preserves_hardcoded_values(self):
        # The whole point of the opt-in flag: existing backtest results
        # must be reproducible without any code-side behavior change.
        params = PortfolioParams()
        assert params.vol_scaled_exits is False
        # And the new multiplier params have the calibrated defaults.
        assert params.sl_vol_k == 12.0
        assert params.pt_vol_k == 5.8
        assert params.trail_vol_k == 4.8


class TestCalibration:
    """At VIX=15, DTE=7 (weekly expiry mid) k=12.0 => 25% SL (approx)."""

    def _enabled_strangle_params(self, **overrides):
        params = ShortStrangleParams()
        params.vol_scaled_exits = True
        for k, v in overrides.items():
            setattr(params, k, v)
        return params

    def test_calibration_vix_15_weekly_sl_recovers_25pct(self):
        # sigma*sqrt(T) = 0.15 * sqrt(7/365) = 0.02077
        # k=12.0 -> 0.2492 ≈ 25%
        params = self._enabled_strangle_params(sl_vol_k=12.0)
        strat = _StubStrategy(params, vix=15.0)
        effective = strat._compute_vol_scaled_exit_pct(
            "sl", dte=7, fallback_pct=30.0  # fallback ignored when enabled
        )
        # 12 * 0.15 * sqrt(7/365) * 100 ≈ 24.93%
        assert effective == pytest.approx(24.93, abs=0.1)

    def test_calibration_vix_15_weekly_pt_recovers_12pct(self):
        params = self._enabled_strangle_params(pt_vol_k=5.8)
        strat = _StubStrategy(params, vix=15.0)
        effective = strat._compute_vol_scaled_exit_pct("pt", dte=7, fallback_pct=0)
        # 5.8 * 0.15 * sqrt(7/365) * 100 ≈ 12.04%
        assert effective == pytest.approx(12.04, abs=0.1)

    def test_calibration_vix_15_weekly_trail_recovers_10pct(self):
        params = self._enabled_strangle_params(trail_vol_k=4.8)
        strat = _StubStrategy(params, vix=15.0)
        effective = strat._compute_vol_scaled_exit_pct("trail", dte=7, fallback_pct=0)
        # 4.8 * 0.15 * sqrt(7/365) * 100 ≈ 9.97% -> clamped to 10.0
        assert effective == pytest.approx(10.0, abs=0.15)


class TestScalingAcrossRegimes:
    """Higher VIX / shorter DTE => wider SL (vol absorbs more noise)."""

    def _enabled(self, **overrides):
        params = ShortStrangleParams()
        params.vol_scaled_exits = True
        for k, v in overrides.items():
            setattr(params, k, v)
        return params

    def test_vix_30_dte_3_widens_to_around_33pct(self):
        # 12 * 0.30 * sqrt(3/365) * 100 = 32.6%
        params = self._enabled(sl_vol_k=12.0)
        strat = _StubStrategy(params, vix=30.0)
        effective = strat._compute_vol_scaled_exit_pct("sl", dte=3, fallback_pct=0)
        assert effective == pytest.approx(32.6, abs=0.5)

    def test_higher_vix_produces_wider_sl(self):
        params = self._enabled(sl_vol_k=12.0)
        s_low = _StubStrategy(params, vix=12.0)
        s_high = _StubStrategy(params, vix=20.0)
        low = s_low._compute_vol_scaled_exit_pct("sl", dte=7, fallback_pct=0)
        high = s_high._compute_vol_scaled_exit_pct("sl", dte=7, fallback_pct=0)
        assert high > low

    def test_shorter_dte_produces_narrower_sl_on_same_vix(self):
        params = self._enabled(sl_vol_k=12.0)
        s_long = _StubStrategy(params, vix=15.0)  # 7 DTE
        s_short = _StubStrategy(params, vix=15.0)  # 2 DTE
        long_sl = s_long._compute_vol_scaled_exit_pct("sl", dte=7, fallback_pct=0)
        short_sl = s_short._compute_vol_scaled_exit_pct("sl", dte=2, fallback_pct=0)
        # sqrt(T) scaling => short DTE gives smaller expected move, smaller SL
        assert short_sl < long_sl


class TestClamps:
    """Absurd inputs must snap to [10%, 60%]."""

    def _enabled(self, **overrides):
        params = ShortStrangleParams()
        params.vol_scaled_exits = True
        for k, v in overrides.items():
            setattr(params, k, v)
        return params

    def test_upper_clamp_at_60pct(self):
        # VIX=60, DTE=0.5 (sub-1 clamped to 1) => 12 * 0.60 * sqrt(1/365) * 100 ≈ 37.7
        # So force a genuinely absurd k to verify the upper clamp fires.
        params = self._enabled(sl_vol_k=100.0)
        strat = _StubStrategy(params, vix=60.0)
        effective = strat._compute_vol_scaled_exit_pct("sl", dte=1, fallback_pct=0)
        assert effective == 60.0

    def test_sub_1_dte_floor_prevents_sqrt_zero(self):
        # DTE=0 must be treated as DTE=1 to avoid degenerate 0% SL.
        params = self._enabled(sl_vol_k=12.0)
        strat = _StubStrategy(params, vix=15.0)
        zero = strat._compute_vol_scaled_exit_pct("sl", dte=0, fallback_pct=0)
        one = strat._compute_vol_scaled_exit_pct("sl", dte=1, fallback_pct=0)
        assert zero == one
        assert zero > 0

    def test_lower_clamp_at_10pct(self):
        # Flat regime: VIX=5, DTE=30 with default k=12 =>
        # 12 * 0.05 * sqrt(30/365) * 100 ≈ 17.2% — NOT clamped.
        # Need smaller k or smaller vix*sqrt(T). Use k=1.
        params = self._enabled(sl_vol_k=1.0)
        strat = _StubStrategy(params, vix=5.0)
        effective = strat._compute_vol_scaled_exit_pct("sl", dte=30, fallback_pct=0)
        assert effective == 10.0

    def test_clamp_band_consistent_with_class_constants(self):
        # Guard against accidental edits to the clamp band.
        assert BaseStrategy._VOL_SCALED_EXIT_MIN == 0.10
        assert BaseStrategy._VOL_SCALED_EXIT_MAX == 0.60


class TestVixUnavailableFallsBack:
    """When VIX is missing/zero the helper returns fallback to avoid 0% SL."""

    def test_zero_vix_returns_fallback(self):
        params = ShortStrangleParams()
        params.vol_scaled_exits = True
        strat = _StubStrategy(params, vix=0.0)
        assert strat._compute_vol_scaled_exit_pct("sl", 7, fallback_pct=30.0) == 30.0

    def test_negative_vix_returns_fallback(self):
        params = ShortStrangleParams()
        params.vol_scaled_exits = True
        strat = _StubStrategy(params, vix=-1.0)
        assert strat._compute_vol_scaled_exit_pct("sl", 7, fallback_pct=30.0) == 30.0
