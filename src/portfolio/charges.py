"""Trade charges calculator for Indian markets.

Calculates: STT, brokerage, transaction charges, SEBI, GST, stamp duty.
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
) -> TradeCharges:
    """Calculate all applicable charges for a trade.

    Args:
        price: Fill price per unit.
        quantity: Number of units traded.
        side: BUY or SELL.
        instrument_type: 'FUT' for futures, 'CE'/'PE' for options.

    Returns:
        TradeCharges with full breakdown.
    """
    turnover = price * quantity
    is_option = instrument_type in ("CE", "PE")
    is_buy = side == OrderSide.BUY

    # ─── Brokerage ───────────────────────────────────────────────
    brokerage_pct = CHARGES["brokerage"]["percentage"] * turnover / 100
    brokerage = min(brokerage_pct, CHARGES["brokerage"]["per_order_cap"])

    # ─── STT ─────────────────────────────────────────────────────
    stt = Decimal("0")
    if is_option:
        if not is_buy:  # STT on sell side for options
            stt = turnover * CHARGES["stt"]["options_sell_pct"] / 100
    else:
        if not is_buy:  # STT on sell side for futures
            stt = turnover * CHARGES["stt"]["futures_sell_pct"] / 100

    # ─── Transaction Charges ─────────────────────────────────────
    if is_option:
        txn = turnover * CHARGES["transaction_charges"]["options_pct"] / 100
    else:
        txn = turnover * CHARGES["transaction_charges"]["futures_pct"] / 100

    # ─── SEBI Charges ────────────────────────────────────────────
    sebi = turnover * CHARGES["sebi_charges_pct"] / 100

    # ─── GST ─────────────────────────────────────────────────────
    gst = (brokerage + txn + sebi) * CHARGES["gst_pct"] / 100

    # ─── Stamp Duty (buy side only) ──────────────────────────────
    stamp = Decimal("0")
    if is_buy:
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
) -> TradeCharges:
    """Estimate total charges for a round-trip (buy + sell)."""
    buy_charges = calculate_charges(price, quantity, OrderSide.BUY, instrument_type)
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


def _round2(val: Decimal) -> Decimal:
    return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
