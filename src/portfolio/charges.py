"""Trade charges calculator for Indian markets.

Calculates: STT, brokerage, transaction charges, SEBI, GST, stamp duty.

Includes expiry-day exercise STT (0.125% on intrinsic value) for ITM options
held to expiry rather than squared off — see `is_expiry_exercise` flag.
"""

from decimal import ROUND_HALF_UP, Decimal

from src.core.constants import CHARGES
from src.core.models import TradeCharges
from src.core.types import OrderSide


def calculate_charges(
    price: Decimal,
    quantity: int,
    side: OrderSide,
    instrument_type: str,  # 'FUT', 'CE', 'PE'
    is_expiry_exercise: bool = False,
) -> TradeCharges:
    """Calculate all applicable charges for a trade.

    Args:
        price: Fill price per unit. For `is_expiry_exercise=True`, this MUST
            be the intrinsic value per unit (max(0, spot-strike) for CE,
            max(0, strike-spot) for PE). For OTM options (intrinsic=0), pass 0.
        quantity: Number of units traded.
        side: BUY or SELL. For exercise, the position-closing side is SELL
            (long position auto-exercised) or BUY (short position assigned).
        instrument_type: 'FUT' for futures, 'CE'/'PE' for options.
        is_expiry_exercise: True when the option is being settled by the
            exchange at expiry (ITM auto-exercise) rather than squared off.
            Triggers higher STT rate (0.125% vs 0.0625%) on intrinsic value
            and skips stamp duty (no buy-side leg in exercise).

    Returns:
        TradeCharges with full breakdown.
    """
    turnover = price * quantity
    is_option = instrument_type in ("CE", "PE")
    is_buy = side == OrderSide.BUY

    # ─── Brokerage ───────────────────────────────────────────────
    # Exercise has no brokerage (exchange-settled, not broker-routed)
    if is_expiry_exercise:
        brokerage = Decimal("0")
    else:
        brokerage_pct = CHARGES["brokerage"]["percentage"] * turnover / 100
        brokerage = min(brokerage_pct, CHARGES["brokerage"]["per_order_cap"])

    # ─── STT ─────────────────────────────────────────────────────
    stt = Decimal("0")
    if is_option:
        if is_expiry_exercise:
            # Higher rate on intrinsic value for ITM auto-exercise.
            # Applies to long-holder side; short-holder pays no STT on assignment.
            if not is_buy:
                stt = turnover * CHARGES["stt"]["options_exercise_pct"] / 100
        elif not is_buy:
            # Regular STT on sell side premium (square-off path)
            stt = turnover * CHARGES["stt"]["options_sell_pct"] / 100
    else:
        if not is_buy:  # STT on sell side for futures
            stt = turnover * CHARGES["stt"]["futures_sell_pct"] / 100

    # ─── Transaction Charges ─────────────────────────────────────
    # Exercise has no transaction charges (no order matched on exchange)
    if is_expiry_exercise:
        txn = Decimal("0")
    elif is_option:
        txn = turnover * CHARGES["transaction_charges"]["options_pct"] / 100
    else:
        txn = turnover * CHARGES["transaction_charges"]["futures_pct"] / 100

    # ─── SEBI Charges ────────────────────────────────────────────
    # SEBI charges still apply to exercise turnover
    sebi = turnover * CHARGES["sebi_charges_pct"] / 100

    # ─── GST ─────────────────────────────────────────────────────
    gst = (brokerage + txn + sebi) * CHARGES["gst_pct"] / 100

    # ─── Stamp Duty (buy side only, NOT on exercise) ─────────────
    stamp = Decimal("0")
    if is_buy and not is_expiry_exercise:
        if is_option:
            stamp = turnover * CHARGES["stamp_duty"]["options_buy_pct"] / 100
        else:
            stamp = turnover * CHARGES["stamp_duty"]["futures_buy_pct"] / 100

    total = brokerage + stt + txn + sebi + gst + stamp

    return TradeCharges(
        brokerage=_round2(brokerage),
        stt=_round2(stt),
        transaction_charges=_round2(txn),
        sebi_charges=_round2(sebi),
        gst=_round2(gst),
        stamp_duty=_round2(stamp),
        total=_round2(total),
    )


def estimate_round_trip_charges(
    price: Decimal,
    quantity: int,
    instrument_type: str,
    held_to_expiry_itm: bool = False,
    intrinsic_at_expiry: Decimal | None = None,
) -> TradeCharges:
    """Estimate total charges for a round-trip (buy + sell or buy + exercise).

    Args:
        price: Entry/exit price per unit (premium).
        quantity: Number of units.
        instrument_type: 'FUT', 'CE', or 'PE'.
        held_to_expiry_itm: If True, exit leg is modeled as auto-exercise at
            expiry instead of a square-off sell. Use for "what-if I don't
            close this ITM position by 3:25 PM" scenarios.
        intrinsic_at_expiry: Required when `held_to_expiry_itm=True` —
            intrinsic value per unit at expiry. If omitted, defaults to
            `price` (assumes premium ≈ intrinsic at expiry, valid for ITM).
    """
    buy_charges = calculate_charges(price, quantity, OrderSide.BUY, instrument_type)
    if held_to_expiry_itm:
        exit_price = intrinsic_at_expiry if intrinsic_at_expiry is not None else price
        sell_charges = calculate_charges(
            exit_price, quantity, OrderSide.SELL, instrument_type,
            is_expiry_exercise=True,
        )
    else:
        sell_charges = calculate_charges(price, quantity, OrderSide.SELL, instrument_type)

    return TradeCharges(
        brokerage=buy_charges.brokerage + sell_charges.brokerage,
        stt=buy_charges.stt + sell_charges.stt,
        transaction_charges=buy_charges.transaction_charges + sell_charges.transaction_charges,
        sebi_charges=buy_charges.sebi_charges + sell_charges.sebi_charges,
        gst=buy_charges.gst + sell_charges.gst,
        stamp_duty=buy_charges.stamp_duty + sell_charges.stamp_duty,
        total=buy_charges.total + sell_charges.total,
    )


def calculate_expiry_exercise_stt(
    intrinsic_per_unit: Decimal,
    quantity: int,
    side: OrderSide = OrderSide.SELL,
) -> Decimal:
    """Calculate STT alone for an ITM option held to expiry exercise.

    Convenience helper for OMS / PnL code that wants to apply just the
    expiry-day STT delta without recomputing all charge components.

    Args:
        intrinsic_per_unit: max(0, spot-strike) for CE, max(0, strike-spot) for PE.
        quantity: Number of units (lot_size × lots).
        side: SELL for the long-holder (auto-exercise inflow), BUY for the
            short-holder (no STT on assignment — returns 0).

    Returns:
        STT amount in rupees.
    """
    if side == OrderSide.BUY or intrinsic_per_unit <= 0:
        return Decimal("0")
    turnover = intrinsic_per_unit * quantity
    return _round2(turnover * CHARGES["stt"]["options_exercise_pct"] / 100)


def _round2(val: Decimal) -> Decimal:
    return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
