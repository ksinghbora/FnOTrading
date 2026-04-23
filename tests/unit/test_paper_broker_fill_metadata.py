"""Tests for per-fill spread_half + book_snapshot metadata.

The cost-sensitivity and capacity validation harnesses consume trade-level
metadata without re-reading GDFL parquet. These assertions lock the shape
and values so the harness can rely on them.
"""

import pytest

from src.broker.paper.client import (
    BookLevel,
    DepthQuote,
    PaperBrokerClient,
)
from src.core.types import OrderSide, OrderType, ProductType


@pytest.mark.asyncio
class TestFillMetadataWithDepth:
    async def _broker(self, depth=None, quotes=None):
        b = PaperBrokerClient(
            initial_capital=1_000_000,
            depth_provider=depth,
            quote_provider=quotes,
        )
        await b.connect()
        return b

    async def test_buy_captures_spread_half_and_book_snapshot(self):
        def provider(_symbol: str) -> DepthQuote:
            return DepthQuote(
                bid_levels=[
                    BookLevel(99.5, 60),
                    BookLevel(99.0, 100),
                ],
                ask_levels=[
                    BookLevel(100.5, 60),
                    BookLevel(101.0, 100),
                ],
                tick_size=0.05,
            )
        broker = await self._broker(depth=provider)
        broker.set_ltp("X", 100.0)
        oid = await broker.place_order(
            "X", "NFO", OrderSide.BUY, 60, OrderType.MARKET, ProductType.NRML,
        )
        order = await broker.get_order_status(oid)
        # (100.5 - 99.5) / 2 = 0.5
        assert order["spread_half"] == pytest.approx(0.5, abs=1e-6)
        assert order["book_snapshot"] is not None
        assert order["book_snapshot"]["bids"] == [(99.5, 60), (99.0, 100)]
        assert order["book_snapshot"]["asks"] == [(100.5, 60), (101.0, 100)]

    async def test_sell_captures_spread_half_and_book_snapshot(self):
        def provider(_symbol: str) -> DepthQuote:
            return DepthQuote(
                bid_levels=[BookLevel(99.5, 100)],
                ask_levels=[BookLevel(100.5, 100)],
                tick_size=0.05,
            )
        broker = await self._broker(depth=provider)
        broker.set_ltp("X", 100.0)
        oid = await broker.place_order(
            "X", "NFO", OrderSide.SELL, 75, OrderType.MARKET, ProductType.NRML,
        )
        order = await broker.get_order_status(oid)
        assert order["spread_half"] == pytest.approx(0.5, abs=1e-6)
        assert order["book_snapshot"]["bids"] == [(99.5, 100)]
        assert order["book_snapshot"]["asks"] == [(100.5, 100)]
        # Trade record (used by backtest result["trades"]) also carries
        # the metadata so the validation harness gets it without the
        # broker's ``_orders`` list.
        trades = await broker.get_trades()
        assert trades[-1]["spread_half"] == pytest.approx(0.5, abs=1e-6)
        assert trades[-1]["book_snapshot"] is not None

    async def test_depth_capped_at_five_levels_per_side(self):
        def provider(_symbol: str) -> DepthQuote:
            return DepthQuote(
                bid_levels=[BookLevel(99 - 0.1 * i, 50) for i in range(8)],
                ask_levels=[BookLevel(100 + 0.1 * i, 50) for i in range(8)],
                tick_size=0.05,
            )
        broker = await self._broker(depth=provider)
        broker.set_ltp("X", 99.5)
        oid = await broker.place_order(
            "X", "NFO", OrderSide.BUY, 50, OrderType.MARKET, ProductType.NRML,
        )
        order = await broker.get_order_status(oid)
        assert len(order["book_snapshot"]["bids"]) == 5
        assert len(order["book_snapshot"]["asks"]) == 5


@pytest.mark.asyncio
class TestFillMetadataTopOfBookOnly:
    """When only a legacy QuoteProvider is set (GDFL path with no depth),
    the snapshot collapses to a single-level list per side. Size unknown
    → recorded as 0 so the shape is consistent.
    """

    async def _broker(self, quotes):
        b = PaperBrokerClient(
            initial_capital=1_000_000,
            quote_provider=quotes,
        )
        await b.connect()
        return b

    async def test_spread_half_and_single_level_book(self):
        def quotes(_symbol: str) -> tuple[float | None, float | None]:
            return 99.5, 100.5
        broker = await self._broker(quotes=quotes)
        broker.set_ltp("X", 100.0)
        oid = await broker.place_order(
            "X", "NFO", OrderSide.BUY, 75, OrderType.MARKET, ProductType.NRML,
        )
        order = await broker.get_order_status(oid)
        assert order["spread_half"] == pytest.approx(0.5, abs=1e-6)
        snap = order["book_snapshot"]
        assert snap is not None
        assert snap["bids"] == [(99.5, 0)]
        assert snap["asks"] == [(100.5, 0)]


@pytest.mark.asyncio
class TestFillMetadataNoQuote:
    """When no provider returns a quote, the broker falls back to the
    SlippageModel / LTP path. Metadata defaults must not throw.
    """

    async def test_no_quote_defaults_to_zero_and_none(self):
        broker = PaperBrokerClient(initial_capital=1_000_000)
        await broker.connect()
        broker.set_ltp("X", 100.0)
        # No quote_provider, no depth_provider — SlippageModel path.
        oid = await broker.place_order(
            "X", "NFO", OrderSide.BUY, 75, OrderType.MARKET, ProductType.NRML,
        )
        order = await broker.get_order_status(oid)
        assert order["spread_half"] == 0.0
        assert order["book_snapshot"] is None
        # Trade record mirrors the order record.
        trades = await broker.get_trades()
        assert trades[-1]["spread_half"] == 0.0
        assert trades[-1]["book_snapshot"] is None
