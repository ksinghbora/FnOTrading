"""Tests for the tiered slippage model in the paper broker.

Validates the four slippage dimensions (liquidity, time-of-day, VIX, size) and
their integration in `PaperBrokerClient.place_order`. Slippage is meant to close
the paper-to-live P&L gap measured during live integration testing.
"""

from datetime import time

import pytest

from src.broker.paper.client import PaperBrokerClient
from src.broker.paper.slippage import (
    PREMIUM_TIERS,
    SlippageModel,
    _size_multiplier,
    _time_multiplier,
    _vix_multiplier,
)
from src.core.types import OrderSide, OrderType, ProductType


# ─── Liquidity tier (premium proxy) ─────────────────────────────────────


class TestLiquidityTiers:
    def setup_method(self):
        self.model = SlippageModel()

    def test_atm_premium_gets_tightest_spread(self):
        # premium >= 100 = ATM/ITM → 5 bps base
        assert self.model._liquidity_bps(150.0) == 5

    def test_near_otm_gets_moderate_spread(self):
        # 30 <= premium < 100 → 20 bps
        assert self.model._liquidity_bps(50.0) == 20

    def test_moderate_otm(self):
        assert self.model._liquidity_bps(15.0) == 50

    def test_far_otm_gets_wide_spread(self):
        assert self.model._liquidity_bps(5.0) == 100

    def test_deep_otm_gets_widest_spread(self):
        assert self.model._liquidity_bps(1.0) == 200

    def test_zero_premium_uses_widest_tier(self):
        assert self.model._liquidity_bps(0.0) == 200

    def test_tiers_monotonically_widen_as_premium_drops(self):
        # Sanity: bps should never decrease as premium falls
        bps_seq = [self.model._liquidity_bps(p) for p in (200, 50, 15, 5, 1)]
        assert bps_seq == sorted(bps_seq)


# ─── Time-of-day multiplier ─────────────────────────────────────────────


class TestTimeMultiplier:
    def test_morning_calm_below_one(self):
        # MMs warming up — slippage smaller than baseline
        assert _time_multiplier(time(9, 30)) == 0.8
        assert _time_multiplier(time(10, 59)) == 0.8

    def test_midday_baseline(self):
        assert _time_multiplier(time(11, 0)) == 1.0
        assert _time_multiplier(time(13, 59)) == 1.0

    def test_afternoon_widens(self):
        assert _time_multiplier(time(14, 0)) == 1.3
        assert _time_multiplier(time(14, 59)) == 1.3

    def test_closing_rush_worst(self):
        assert _time_multiplier(time(15, 0)) == 1.8
        assert _time_multiplier(time(15, 25)) == 1.8


# ─── VIX regime multiplier ──────────────────────────────────────────────


class TestVixMultiplier:
    def test_unknown_vix_assumes_normal(self):
        assert _vix_multiplier(0.0) == 1.0

    def test_normal_band_baseline(self):
        # NORMAL band: <16
        assert _vix_multiplier(14.5) == 1.0
        assert _vix_multiplier(15.99) == 1.0

    def test_high_band_widens(self):
        # HIGH band: 16-22
        assert _vix_multiplier(16.0) == 1.3
        assert _vix_multiplier(21.99) == 1.3

    def test_extreme_band_worst(self):
        # EXTREME: >=22
        assert _vix_multiplier(22.0) == 1.8
        assert _vix_multiplier(35.0) == 1.8


# ─── Order size multiplier ──────────────────────────────────────────────


class TestSizeMultiplier:
    def test_one_lot_baseline(self):
        # NIFTY lot = 75
        assert _size_multiplier(75) == 1.0

    def test_few_lots_minor_impact(self):
        assert _size_multiplier(150) == 1.1     # 2 lots
        assert _size_multiplier(225) == 1.1     # 3 lots

    def test_medium_lots(self):
        assert _size_multiplier(300) == 1.25    # 4 lots
        assert _size_multiplier(375) == 1.25    # 5 lots

    def test_large_orders_walk_book(self):
        assert _size_multiplier(450) == 1.5     # 6 lots
        assert _size_multiplier(1500) == 1.5    # 20 lots


# ─── End-to-end slippage application ────────────────────────────────────


class TestSlippageApply:
    def setup_method(self):
        self.model = SlippageModel()
        self.midday = time(12, 0)

    def test_buy_pays_more_than_ltp(self):
        slipped, _bps = self.model.apply(
            fill_price=100.0, side=OrderSide.BUY, vix=14.0, quantity=75, now=self.midday
        )
        assert slipped > 100.0

    def test_sell_receives_less_than_ltp(self):
        slipped, _bps = self.model.apply(
            fill_price=100.0, side=OrderSide.SELL, vix=14.0, quantity=75, now=self.midday
        )
        assert slipped < 100.0

    def test_atm_buy_at_normal_vix_loses_5bps(self):
        # 100 * 5/10000 = 0.05 → fill at 100.05
        slipped, bps = self.model.apply(
            fill_price=100.0, side=OrderSide.BUY, vix=14.0, quantity=75, now=self.midday
        )
        assert bps == pytest.approx(5.0)
        assert slipped == pytest.approx(100.05)

    def test_far_otm_sell_at_extreme_vix_in_close_compounds(self):
        # premium=5 (100 bps) × 15:00 (1.8x) × VIX 25 (1.8x) × 1 lot (1.0x) = 324 bps
        # 5.0 * 0.0324 = 0.162 → fill at ~4.838
        _slipped, bps = self.model.apply(
            fill_price=5.0, side=OrderSide.SELL, vix=25.0, quantity=75, now=time(15, 0)
        )
        assert bps == pytest.approx(100 * 1.8 * 1.8 * 1.0)

    def test_min_option_price_floor_for_sell(self):
        # Cheap option, terrible conditions — must not go below min tick (0.05)
        slipped, _bps = self.model.apply(
            fill_price=0.10, side=OrderSide.SELL, vix=30.0, quantity=2000, now=time(15, 25)
        )
        assert slipped >= 0.05

    def test_zero_fill_price_returns_zero_bps(self):
        _slipped, bps = self.model.apply(
            fill_price=0.0, side=OrderSide.BUY, vix=14.0, quantity=75, now=self.midday
        )
        assert bps == 0.0


# ─── Integration with PaperBrokerClient ─────────────────────────────────


@pytest.mark.asyncio
class TestPaperBrokerSlippageIntegration:
    async def _make_broker(self, slippage=...):
        kwargs = {"initial_capital": 1_000_000}
        if slippage is not ...:
            kwargs["slippage"] = slippage
        broker = PaperBrokerClient(**kwargs)
        await broker.connect()
        return broker

    async def test_default_broker_has_slippage_enabled(self):
        broker = await self._make_broker()
        assert broker.slippage is not None
        assert isinstance(broker.slippage, SlippageModel)

    async def test_can_disable_slippage_with_explicit_none_via_legacy_path(self):
        # Backwards compat: tests that need exact-LTP fills can monkey-patch
        broker = await self._make_broker()
        broker.slippage = None  # opt-out path for legacy backtests
        broker.set_ltp("NIFTY26APR24500CE", 100.0)
        oid = await broker.place_order(
            tradingsymbol="NIFTY26APR24500CE",
            exchange="NFO",
            side=OrderSide.BUY,
            quantity=75,
            order_type=OrderType.MARKET,
            product=ProductType.NRML,
        )
        order = await broker.get_order_status(oid)
        assert order["average_price"] == 100.0
        assert order["slippage_bps"] == 0.0

    async def test_buy_fills_above_ltp(self):
        broker = await self._make_broker()
        broker.set_vix(14.0)  # Normal regime
        broker.set_ltp("NIFTY26APR24500CE", 100.0)
        oid = await broker.place_order(
            tradingsymbol="NIFTY26APR24500CE",
            exchange="NFO",
            side=OrderSide.BUY,
            quantity=75,
        )
        order = await broker.get_order_status(oid)
        assert order["average_price"] > order["ltp"]
        assert order["ltp"] == 100.0
        assert order["slippage_bps"] > 0

    async def test_sell_fills_below_ltp(self):
        broker = await self._make_broker()
        broker.set_vix(14.0)
        broker.set_ltp("NIFTY26APR24500CE", 100.0)
        oid = await broker.place_order(
            tradingsymbol="NIFTY26APR24500CE",
            exchange="NFO",
            side=OrderSide.SELL,
            quantity=75,
        )
        order = await broker.get_order_status(oid)
        assert order["average_price"] < order["ltp"]

    async def test_extreme_vix_widens_slippage(self):
        normal = await self._make_broker()
        normal.set_vix(14.0)
        normal.set_ltp("X", 50.0)
        n_oid = await normal.place_order("X", "NFO", OrderSide.BUY, 75)
        n_order = await normal.get_order_status(n_oid)

        extreme = await self._make_broker()
        extreme.set_vix(25.0)
        extreme.set_ltp("X", 50.0)
        e_oid = await extreme.place_order("X", "NFO", OrderSide.BUY, 75)
        e_order = await extreme.get_order_status(e_oid)

        assert e_order["slippage_bps"] > n_order["slippage_bps"]

    async def test_set_vix_updates_regime(self):
        broker = await self._make_broker()
        assert broker._vix == 0.0
        broker.set_vix(18.5)
        assert broker._vix == 18.5

    async def test_premium_tiers_are_well_formed(self):
        # Sanity: tiers are sorted by min_premium descending
        mins = [t[0] for t in PREMIUM_TIERS]
        assert mins == sorted(mins, reverse=True)
