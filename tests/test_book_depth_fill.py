"""Tests for book-depth-aware fill model (src/broker/paper/client.py).

Validates:
  * Walking a 3-level book for a 75-contract NIFTY BUY returns the
    expected VWAP (0.167 above best ask) — matches hand-computed value.
  * Fallback when only top-of-book is available — uses per-lot tick
    penalty and tags ``fill_source = "book_depth_estimated"``.
  * LIMIT semantics from F1/F2 remain intact: a limit inside the spread
    still pays the strategy's quote.
  * Symmetric SELL walk descending through bid levels.
"""

import pytest

from src.broker.paper.client import (
    BookLevel,
    DepthQuote,
    PaperBrokerClient,
    estimate_top_of_book_walk,
    walk_book,
    walk_book_descending,
)
from src.core.types import OrderSide, OrderType, ProductType


# ─── Pure-function book walk ────────────────────────────────────────────


class TestWalkBook:
    def test_single_level_covers_quantity(self):
        levels = [BookLevel(price=100.0, size=100)]
        vwap, filled, consumed = walk_book(levels, 75)
        assert vwap == 100.0
        assert filled == 75
        assert consumed == [(100.0, 75)]

    def test_walks_across_levels(self):
        # NIFTY lot = 75. Best ask 100 has 50 contracts, next level 100.5
        # has another 50. A 75-lot BUY takes 50@100 + 25@100.5.
        levels = [
            BookLevel(price=100.0, size=50),
            BookLevel(price=100.5, size=50),
            BookLevel(price=101.0, size=100),
        ]
        vwap, filled, consumed = walk_book(levels, 75)
        expected = (50 * 100.0 + 25 * 100.5) / 75
        assert filled == 75
        assert vwap == pytest.approx(expected, abs=1e-9)
        # Hand-check: (5000 + 2512.5) / 75 = 100.16666...
        assert vwap == pytest.approx(100.166666, abs=1e-4)
        assert consumed == [(100.0, 50), (100.5, 25)]

    def test_three_level_full_book(self):
        # 150 contracts takes full levels 1+2 and 50 of level 3.
        levels = [
            BookLevel(price=100.0, size=50),
            BookLevel(price=100.5, size=50),
            BookLevel(price=101.0, size=100),
        ]
        vwap, filled, consumed = walk_book(levels, 150)
        expected = (50 * 100.0 + 50 * 100.5 + 50 * 101.0) / 150
        assert filled == 150
        assert vwap == pytest.approx(expected)
        assert len(consumed) == 3

    def test_book_exhaust_synthesizes_one_more_tick(self):
        # 200 contracts requested, book exposes only 100. We walk the
        # last known price + 1 tick for the tail.
        levels = [
            BookLevel(price=100.0, size=50),
            BookLevel(price=100.5, size=50),
        ]
        vwap, filled, consumed = walk_book(levels, 200, tick_size=0.5)
        assert filled == 200
        # Last level was 100.5, synthetic at 101.0 × 100 contracts
        synth_notional = (50 * 100.0) + (50 * 100.5) + (100 * 101.0)
        assert vwap == pytest.approx(synth_notional / 200)
        assert consumed[-1] == (101.0, 100)

    def test_zero_quantity(self):
        levels = [BookLevel(price=100.0, size=50)]
        vwap, filled, consumed = walk_book(levels, 0)
        assert filled == 0
        assert consumed == []

    def test_empty_book(self):
        vwap, filled, consumed = walk_book([], 75)
        assert filled == 0
        assert consumed == []


class TestWalkBookDescending:
    def test_sell_walks_down_bid_side(self):
        # Best bid 100, then 99.5, 99.0.
        levels = [
            BookLevel(price=100.0, size=50),
            BookLevel(price=99.5, size=50),
            BookLevel(price=99.0, size=100),
        ]
        vwap, filled, _consumed = walk_book_descending(levels, 75)
        expected = (50 * 100.0 + 25 * 99.5) / 75
        assert filled == 75
        assert vwap == pytest.approx(expected)

    def test_sell_book_exhaust_steps_down_one_tick(self):
        levels = [BookLevel(price=100.0, size=50)]
        vwap, filled, consumed = walk_book_descending(levels, 100, tick_size=0.5)
        assert filled == 100
        assert consumed[-1] == (99.5, 50)


# ─── Top-of-book fallback (no depth) ────────────────────────────────────


class TestTopOfBookFallback:
    def test_buy_penalizes_one_tick_per_extra_lot(self):
        # NIFTY lot=75. 75-contract BUY stays at best ask.
        vwap, consumed = estimate_top_of_book_walk(
            best_price=100.0, quantity=75, side=OrderSide.BUY, lot_size=75
        )
        assert vwap == pytest.approx(100.0)
        assert consumed == [(100.0, 75)]

    def test_buy_two_lots_adds_one_tick_on_second_lot(self):
        # 150 contracts = 2 lots. First lot @100.0, second @100.05.
        vwap, consumed = estimate_top_of_book_walk(
            best_price=100.0, quantity=150, side=OrderSide.BUY,
            lot_size=75, tick_size=0.05,
        )
        assert consumed == [(100.0, 75), (100.05, 75)]
        assert vwap == pytest.approx((75 * 100.0 + 75 * 100.05) / 150)

    def test_sell_steps_down(self):
        vwap, consumed = estimate_top_of_book_walk(
            best_price=100.0, quantity=150, side=OrderSide.SELL,
            lot_size=75, tick_size=0.05,
        )
        assert consumed == [(100.0, 75), (99.95, 75)]

    def test_sell_never_goes_negative(self):
        # Absurd size, tiny premium — walking should clamp to tick size.
        vwap, consumed = estimate_top_of_book_walk(
            best_price=0.10, quantity=75 * 100, side=OrderSide.SELL,
            lot_size=75, tick_size=0.05,
        )
        assert all(p > 0 for p, _ in consumed)


# ─── PaperBrokerClient integration with depth provider ────────────────


@pytest.mark.asyncio
class TestPaperBrokerDepthIntegration:
    async def _broker(self, depth=None, quotes=None):
        b = PaperBrokerClient(
            initial_capital=1_000_000,
            depth_provider=depth,
            quote_provider=quotes,
        )
        await b.connect()
        return b

    async def test_buy_walks_three_levels(self):
        # Synthetic book: 50@100, 50@100.5, 100@101. BUY 75 → VWAP ≈ 100.167.
        def provider(_symbol: str) -> DepthQuote:
            return DepthQuote(
                bid_levels=[BookLevel(99.5, 100)],
                ask_levels=[
                    BookLevel(100.0, 50),
                    BookLevel(100.5, 50),
                    BookLevel(101.0, 100),
                ],
                tick_size=0.05,
            )
        broker = await self._broker(depth=provider)
        broker.set_ltp("NIFTY25100CE", 100.0)
        oid = await broker.place_order(
            tradingsymbol="NIFTY25100CE",
            exchange="NFO",
            side=OrderSide.BUY,
            quantity=75,
            order_type=OrderType.MARKET,
            product=ProductType.NRML,
        )
        order = await broker.get_order_status(oid)
        assert order["fill_source"] == "book_walk"
        assert order["average_price"] == pytest.approx(100.16666, abs=1e-3)
        assert order["book_walk_slippage_pct"] > 0  # Walked past best ask
        assert len(order["book_walk_levels"]) == 2

    async def test_sell_walks_bid_side(self):
        def provider(_symbol: str) -> DepthQuote:
            return DepthQuote(
                bid_levels=[
                    BookLevel(99.0, 50),
                    BookLevel(98.5, 50),
                ],
                ask_levels=[BookLevel(100.0, 100)],
                tick_size=0.05,
            )
        broker = await self._broker(depth=provider)
        broker.set_ltp("X", 99.5)
        oid = await broker.place_order(
            "X", "NFO", OrderSide.SELL, 75, OrderType.MARKET, ProductType.NRML,
        )
        order = await broker.get_order_status(oid)
        expected = (50 * 99.0 + 25 * 98.5) / 75
        assert order["average_price"] == pytest.approx(expected, abs=1e-3)
        assert order["fill_source"] == "book_walk"

    async def test_fallback_when_depth_not_available(self):
        # Only top-of-book via legacy quote_provider → book_depth_estimated.
        def quotes(_symbol: str) -> tuple[float | None, float | None]:
            return 99.5, 100.0
        broker = await self._broker(quotes=quotes)
        broker.set_ltp("X", 99.75)
        oid = await broker.place_order(
            "X", "NFO", OrderSide.BUY, 150, OrderType.MARKET, ProductType.NRML,
        )
        order = await broker.get_order_status(oid)
        assert order["fill_source"] == "book_depth_estimated"
        # NIFTY default lot=75 (inferred when symbol doesn't match). The
        # test symbol "X" uses the hard-coded fallback of 75.
        # 150 contracts @ (75@100.00 + 75@100.05) / 150 = 100.025
        assert order["average_price"] == pytest.approx(100.025, abs=1e-3)
        assert order["book_walk_slippage_pct"] > 0

    async def test_limit_inside_spread_still_fills_at_price_f1f2(self):
        # The F1/F2 behaviour from commit 8e1060d must survive: a LIMIT
        # inside [bid, ask] fills at the limit price, not a walked VWAP.
        def provider(_symbol: str) -> DepthQuote:
            return DepthQuote(
                bid_levels=[BookLevel(99.5, 100)],
                ask_levels=[BookLevel(100.5, 100)],
                tick_size=0.05,
            )
        broker = await self._broker(depth=provider)
        broker.set_ltp("X", 100.0)
        oid = await broker.place_order(
            "X", "NFO", OrderSide.BUY, 75, OrderType.LIMIT, ProductType.NRML,
            price=100.0,  # Mid-market limit
        )
        order = await broker.get_order_status(oid)
        assert order["fill_source"] == "limit_in_band"
        assert order["average_price"] == pytest.approx(100.0)

    async def test_aggressive_limit_triggers_walk(self):
        # LIMIT BUY above ask is aggressive and must walk the book.
        def provider(_symbol: str) -> DepthQuote:
            return DepthQuote(
                bid_levels=[BookLevel(99.5, 100)],
                ask_levels=[
                    BookLevel(100.0, 30),
                    BookLevel(100.5, 100),
                ],
                tick_size=0.05,
            )
        broker = await self._broker(depth=provider)
        broker.set_ltp("X", 100.0)
        oid = await broker.place_order(
            "X", "NFO", OrderSide.BUY, 75, OrderType.LIMIT, ProductType.NRML,
            price=101.0,  # Aggressive
        )
        order = await broker.get_order_status(oid)
        assert order["fill_source"] == "book_walk"
        # 30@100 + 45@100.5 = (3000+4522.5)/75 = 100.3
        assert order["average_price"] == pytest.approx(100.3, abs=1e-3)
