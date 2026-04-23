"""Signal type definitions for strategy communication.

F1 (Apr 23 2026): leg builders now default to LIMIT orders priced at mid-of-book.
The old default of `order_type=MARKET, price=0` caused every live order to be
routed as MARKET, which on thin OTM weekly options crosses the full bid-ask
spread (expert-estimated 30-60 bps/leg). `make_leg` still accepts an explicit
`order_type=OrderType.MARKET` for narrow emergency-close / kill-switch paths,
but strategies should use `make_option_leg()` which computes mid from a quote
and returns LIMIT — or returns None when the quote is missing (the caller
must skip that entry rather than fall back to MARKET).

F1b (deferred): timeout/reprice loop if a LIMIT doesn't fill within N seconds
is a follow-up; today this iteration only places the initial LIMIT-at-mid.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

from src.core.models import Signal, SignalLeg
from src.core.types import OrderSide, OrderType, SignalType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.core.models import OptionData

logger = logging.getLogger(__name__)


def entry_signal(
    strategy_id: str,
    legs: list[SignalLeg],
    reason: str = "",
) -> Signal:
    """Create an ENTRY signal."""
    return Signal(
        strategy_id=strategy_id,
        signal_type=SignalType.ENTRY,
        legs=legs,
        reason=reason,
    )


def exit_signal(
    strategy_id: str,
    legs: list[SignalLeg],
    reason: str = "",
) -> Signal:
    """Create an EXIT signal."""
    return Signal(
        strategy_id=strategy_id,
        signal_type=SignalType.EXIT,
        legs=legs,
        reason=reason,
    )


def adjust_signal(
    strategy_id: str,
    legs: list[SignalLeg],
    reason: str = "",
) -> Signal:
    """Create an ADJUST signal."""
    return Signal(
        strategy_id=strategy_id,
        signal_type=SignalType.ADJUST,
        legs=legs,
        reason=reason,
    )


# ─── Quote-aware pricing helpers ─────────────────────────────────────


def mid_from_quote(
    bid: Decimal | float | int | None,
    ask: Decimal | float | int | None,
    tick_size: Decimal = Decimal("0.05"),
) -> Decimal | None:
    """Return (bid + ask) / 2, rounded to `tick_size`, or None if either side is missing.

    Returns None when bid or ask is missing/zero, or when bid > ask (crossed book).
    The caller decides whether to skip the leg or fall back (for F1 the answer
    is always "skip"; MARKET fallback is reserved for emergency-close paths).
    """
    if bid is None or ask is None:
        return None
    try:
        b = Decimal(str(bid))
        a = Decimal(str(ask))
    except Exception:
        return None
    if b <= 0 or a <= 0:
        return None
    if b > a:
        # Crossed book — likely stale one side. Refuse to price.
        return None
    raw = (b + a) / Decimal("2")
    if tick_size and tick_size > 0:
        # Round to the nearest tick_size (NSE options: 0.05).
        steps = (raw / tick_size).quantize(Decimal("1"))
        return steps * tick_size
    return raw


def mid_from_option(
    opt: OptionData | None,
    tick_size: Decimal = Decimal("0.05"),
) -> Decimal | None:
    """Compute mid from an OptionData (or duck-typed) object with bid_price/ask_price.

    Returns None when the object is missing or either quote side is absent. Does
    NOT fall back to LTP — an LTP fallback would re-introduce the stale-tick
    problem that existed before F1, and for F1 we want a hard "skip if no
    quote" policy. Use `resolve_option_price` in portfolio_pricing.py for the
    LTP-then-mid cascade used by recorded-chain replay paths.
    """
    if opt is None:
        return None
    bid = getattr(opt, "bid_price", None)
    ask = getattr(opt, "ask_price", None)
    return mid_from_quote(bid, ask, tick_size=tick_size)


def make_leg(
    tradingsymbol: str,
    instrument_token: int,
    side: OrderSide,
    quantity: int,
    order_type: OrderType = OrderType.LIMIT,
    price: float | Decimal = 0,
) -> SignalLeg:
    """Helper to create a signal leg.

    F1 contract: the default is now LIMIT. Callers must pass `price > 0`
    when using the default order_type or the OMS validator will reject
    the order. For emergency-close / kill-switch paths that intentionally
    want to cross the book, pass `order_type=OrderType.MARKET` explicitly
    (price is ignored for MARKET orders).
    """
    return SignalLeg(
        tradingsymbol=tradingsymbol,
        instrument_token=instrument_token,
        order_side=side,
        quantity=quantity,
        order_type=order_type,
        price=Decimal(str(price)),
    )


def make_option_leg(
    tradingsymbol: str,
    instrument_token: int,
    side: OrderSide,
    quantity: int,
    *,
    opt: OptionData | None = None,
    bid: Decimal | float | int | None = None,
    ask: Decimal | float | int | None = None,
    tick_size: Decimal = Decimal("0.05"),
    strategy_id: str = "",
) -> SignalLeg | None:
    """Build a LIMIT-at-mid SignalLeg for an options trade.

    Pass either `opt` (an OptionData-like with `.bid_price` / `.ask_price`) or
    the raw `bid` / `ask` values. Returns None when pricing is unavailable —
    the caller MUST skip the entire trade, NOT fall back to MARKET. A
    strategy that can't price its wings shouldn't enter that trade.

    Contract note — no hidden defaults. If you want a MARKET order
    (emergency close), use `make_leg(..., order_type=OrderType.MARKET)`
    directly and pass a reason via your calling log line. This helper is
    deliberately narrow: LIMIT at mid, or no order.
    """
    if opt is not None:
        mid = mid_from_option(opt, tick_size=tick_size)
    else:
        mid = mid_from_quote(bid, ask, tick_size=tick_size)

    if mid is None or mid <= 0:
        # Structured message so downstream grep/dashboards pick it up.
        logger.info(
            "[FILL] bid/ask missing for %s — skipping leg "
            "(strategy=%s side=%s qty=%d)",
            tradingsymbol, strategy_id, side.value, quantity,
        )
        return None

    return SignalLeg(
        tradingsymbol=tradingsymbol,
        instrument_token=instrument_token,
        order_side=side,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        price=mid,
    )
