"""Tests for trade charges calculation."""

import pytest
from decimal import Decimal

from src.core.types import OrderSide
from src.portfolio.charges import calculate_charges, estimate_round_trip_charges


class TestCharges:
    """Test charges calculation accuracy."""

    def test_option_sell_has_stt(self):
        charges = calculate_charges(
            price=Decimal("150"), quantity=75, side=OrderSide.SELL, instrument_type="CE"
        )
        assert charges.stt > 0
        assert charges.brokerage > 0
        assert charges.total > 0

    def test_option_buy_no_stt(self):
        charges = calculate_charges(
            price=Decimal("150"), quantity=75, side=OrderSide.BUY, instrument_type="CE"
        )
        assert charges.stt == 0  # No STT on buy side for options

    def test_option_buy_has_stamp_duty(self):
        charges = calculate_charges(
            price=Decimal("150"), quantity=75, side=OrderSide.BUY, instrument_type="CE"
        )
        assert charges.stamp_duty > 0

    def test_option_sell_no_stamp_duty(self):
        charges = calculate_charges(
            price=Decimal("150"), quantity=75, side=OrderSide.SELL, instrument_type="CE"
        )
        assert charges.stamp_duty == 0

    def test_brokerage_capped_at_20(self):
        # High turnover should cap brokerage at Rs 20
        charges = calculate_charges(
            price=Decimal("1000"), quantity=750, side=OrderSide.SELL, instrument_type="CE"
        )
        assert charges.brokerage <= Decimal("20")

    def test_gst_on_brokerage_and_txn(self):
        charges = calculate_charges(
            price=Decimal("150"), quantity=75, side=OrderSide.SELL, instrument_type="CE"
        )
        assert charges.gst > 0

    def test_futures_charges(self):
        charges = calculate_charges(
            price=Decimal("22000"), quantity=75, side=OrderSide.SELL, instrument_type="FUT"
        )
        assert charges.stt > 0
        assert charges.total > 0

    def test_round_trip_charges(self):
        charges = estimate_round_trip_charges(
            price=Decimal("150"), quantity=75, instrument_type="CE"
        )
        assert charges.total > 0
        # Round trip should have both buy and sell charges
        assert charges.stt > 0  # From sell side
        assert charges.stamp_duty > 0  # From buy side

    def test_total_is_sum_of_parts(self):
        charges = calculate_charges(
            price=Decimal("150"), quantity=75, side=OrderSide.SELL, instrument_type="CE"
        )
        expected = (
            charges.brokerage + charges.stt + charges.transaction_charges
            + charges.sebi_charges + charges.gst + charges.stamp_duty
        )
        assert abs(charges.total - expected) < Decimal("0.02")
