"""Tests for trade charges calculation."""

import pytest
from decimal import Decimal

from src.core.types import OrderSide
from src.portfolio.charges import (
    calculate_charges,
    calculate_expiry_exercise_stt,
    estimate_round_trip_charges,
)


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


class TestExpiryExerciseCharges:
    """Test expiry-day auto-exercise charges (ITM options held to settlement)."""

    def test_expiry_exercise_uses_higher_stt_rate(self):
        # Apr 25 2026 audit: sell-side STT was raised to 0.10% on Oct 1
        # 2024 per Union Budget 2024; exercise stayed at 0.125% until
        # Apr 1 2026 when Union Budget 2026 raised it to 0.15%. The
        # exercise:sell ratio is no longer a fixed 2:1 — assert against
        # the configured rates rather than the legacy ratio.
        from src.core.constants import CHARGES
        intrinsic = Decimal("100")
        qty = 75
        exercise = calculate_charges(
            price=intrinsic, quantity=qty, side=OrderSide.SELL,
            instrument_type="CE", is_expiry_exercise=True,
        )
        normal_sell = calculate_charges(
            price=intrinsic, quantity=qty, side=OrderSide.SELL, instrument_type="CE",
        )
        expected_exercise_stt = (
            intrinsic * qty * CHARGES["stt"]["options_exercise_pct"] / 100
        )
        expected_sell_stt = (
            intrinsic * qty * CHARGES["stt"]["options_sell_pct"] / 100
        )
        assert exercise.stt.quantize(Decimal("0.01")) == expected_exercise_stt.quantize(Decimal("0.01"))
        assert normal_sell.stt.quantize(Decimal("0.01")) == expected_sell_stt.quantize(Decimal("0.01"))
        # The whole point of the rule still holds: exercise > normal sell.
        assert exercise.stt > normal_sell.stt

    def test_expiry_exercise_no_brokerage(self):
        charges = calculate_charges(
            price=Decimal("100"), quantity=75, side=OrderSide.SELL,
            instrument_type="CE", is_expiry_exercise=True,
        )
        assert charges.brokerage == Decimal("0")

    def test_expiry_exercise_no_transaction_charges(self):
        charges = calculate_charges(
            price=Decimal("100"), quantity=75, side=OrderSide.SELL,
            instrument_type="CE", is_expiry_exercise=True,
        )
        assert charges.transaction_charges == Decimal("0")

    def test_expiry_exercise_no_stamp_duty(self):
        # No buy-side leg in exercise — stamp duty must be zero on both sides
        sell_exercise = calculate_charges(
            price=Decimal("100"), quantity=75, side=OrderSide.SELL,
            instrument_type="CE", is_expiry_exercise=True,
        )
        buy_assignment = calculate_charges(
            price=Decimal("100"), quantity=75, side=OrderSide.BUY,
            instrument_type="CE", is_expiry_exercise=True,
        )
        assert sell_exercise.stamp_duty == Decimal("0")
        assert buy_assignment.stamp_duty == Decimal("0")

    def test_expiry_exercise_keeps_sebi_and_gst(self):
        # SEBI still applies on exercise turnover; GST on (brokerage+txn+sebi)
        charges = calculate_charges(
            price=Decimal("100"), quantity=75, side=OrderSide.SELL,
            instrument_type="CE", is_expiry_exercise=True,
        )
        assert charges.sebi_charges > 0
        # GST should be 18% of just SEBI (since brokerage and txn are zero)
        expected_gst = charges.sebi_charges * Decimal("18") / 100
        assert abs(charges.gst - expected_gst) < Decimal("0.02")

    def test_short_holder_assignment_pays_no_stt(self):
        # Short-side (BUY to close on assignment) — no STT
        charges = calculate_charges(
            price=Decimal("100"), quantity=75, side=OrderSide.BUY,
            instrument_type="CE", is_expiry_exercise=True,
        )
        assert charges.stt == Decimal("0")

    def test_otm_expiry_exercise_zero_charges(self):
        # OTM at expiry (intrinsic=0) — no STT, no SEBI, no anything
        charges = calculate_charges(
            price=Decimal("0"), quantity=75, side=OrderSide.SELL,
            instrument_type="CE", is_expiry_exercise=True,
        )
        assert charges.stt == Decimal("0")
        assert charges.sebi_charges == Decimal("0")
        assert charges.total == Decimal("0")

    def test_round_trip_held_to_expiry_itm(self):
        # Buy at premium, exit via auto-exercise at intrinsic
        charges = estimate_round_trip_charges(
            price=Decimal("150"), quantity=75, instrument_type="CE",
            held_to_expiry_itm=True, intrinsic_at_expiry=Decimal("100"),
        )
        # Buy leg: stamp duty + brokerage; Exercise leg: higher STT, no brokerage
        assert charges.stamp_duty > 0   # from buy leg
        assert charges.brokerage > 0    # from buy leg only
        assert charges.stt > 0          # from exercise leg

    def test_round_trip_squareoff_vs_exercise_stt(self):
        # Holding to expiry (ITM) should incur higher STT than squaring off at same price
        squareoff = estimate_round_trip_charges(
            price=Decimal("100"), quantity=75, instrument_type="CE",
        )
        exercised = estimate_round_trip_charges(
            price=Decimal("100"), quantity=75, instrument_type="CE",
            held_to_expiry_itm=True, intrinsic_at_expiry=Decimal("100"),
        )
        assert exercised.stt > squareoff.stt


class TestExpiryExerciseSttHelper:
    """Tests for the standalone calculate_expiry_exercise_stt helper."""

    def test_long_holder_pays_stt_on_intrinsic(self):
        stt = calculate_expiry_exercise_stt(
            intrinsic_per_unit=Decimal("100"), quantity=75, side=OrderSide.SELL,
        )
        # 100 * 75 * 0.125% = 9.375 → rounds to 9.38
        assert stt == Decimal("9.38")

    def test_short_holder_pays_no_stt(self):
        stt = calculate_expiry_exercise_stt(
            intrinsic_per_unit=Decimal("100"), quantity=75, side=OrderSide.BUY,
        )
        assert stt == Decimal("0")

    def test_otm_expiry_zero_stt(self):
        stt = calculate_expiry_exercise_stt(
            intrinsic_per_unit=Decimal("0"), quantity=75, side=OrderSide.SELL,
        )
        assert stt == Decimal("0")
