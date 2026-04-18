"""Price-resolution and wing-clamping helpers for the portfolio strategy.

Two execution bugs surfaced in the 23-day chain replay (Apr 2026):

  1. Trend leg's debit non-positive: the ITM leg's LTP comes back as 0
     in the recorded chain when that strike didn't trade in a given minute,
     producing a negative `debit = buy_ltp - sell_ltp` that aborts entry.
     Real exchanges quote bid/ask continuously even when the last trade is
     stale, so the fix is to fall back to mid (bid+ask)/2.

  2. Iron condor wing strikes outside chain: with `ic_wing_width_strikes=8`
     (8 × 50pts = 400pts), wings often land below the lowest strike in the
     recorded chain (BANKNIFTY 51000 PE wing on a 21500 NIFTY short, etc.).
     The fix is to clamp the wing inward until we find a strike that's
     actually in the chain — better a narrower-than-target IC than no IC.

Both helpers are pure functions of their inputs so they can be unit-tested
without spinning up a full strategy.
"""

from __future__ import annotations

import logging
from decimal import Decimal

logger = logging.getLogger(__name__)


def resolve_option_price(opt, side: str) -> Decimal | None:
    """Resolve a fillable price for an option leg using LTP → mid → quote fallback.

    Args:
        opt: An OptionData (or duck-typed object) with `ltp`, `bid_price`, `ask_price`.
        side: "BUY" or "SELL". Determines which side of the book we cross when
              only one quote side is populated. For BUY we'd pay ask; for SELL
              we'd hit bid. When both bid and ask are populated, mid is used
              regardless of side (best estimate of fair value).

    Resolution order:
      1. LTP if > 0
      2. mid = (bid + ask) / 2 if both > 0
      3. ask if BUY-side and ask > 0
      4. bid if SELL-side and bid > 0
      5. None (caller decides whether to block)

    Why LTP wins over mid when both exist: LTP is a real transaction; mid
    is a market-maker quote that may be wider during illiquid moments.
    LTP-when-fresh is closer to what we'd actually fill at.
    """
    if opt is None:
        return None

    ltp = Decimal(str(opt.ltp)) if opt.ltp else Decimal("0")
    bid = Decimal(str(opt.bid_price)) if opt.bid_price else Decimal("0")
    ask = Decimal(str(opt.ask_price)) if opt.ask_price else Decimal("0")

    if ltp > 0:
        return ltp
    if bid > 0 and ask > 0 and bid < ask:
        return (bid + ask) / Decimal("2")
    if side == "BUY" and ask > 0:
        return ask
    if side == "SELL" and bid > 0:
        return bid
    return None


def find_available_wing_strike(
    chain,
    base_strike: float,
    desired_offset_pts: int,
    direction: int,
    opt_attr: str,
    strike_step: int = 50,
) -> tuple[object | None, int]:
    """Walk inward from `base_strike + direction*desired_offset_pts` to find a
    strike that exists in `chain.strikes` AND has the required option leg with
    a positive LTP or quote.

    Args:
        chain: OptionChain
        base_strike: Short strike (CE or PE) to size the wing relative to
        desired_offset_pts: Target wing distance in points (e.g. 8 strikes × 50)
        direction: +1 to walk above (CE wing), -1 to walk below (PE wing)
        opt_attr: "ce" or "pe" — which leg the wing must have
        strike_step: Strike increment in points (50 for NIFTY, 100 for BANKNIFTY)

    Returns:
        (entry, actual_offset_pts) — the chain entry chosen and the actual
        wing distance in points. If no valid strike exists between base_strike
        and the desired wing, returns (None, 0).

    Walking inward (vs outward) preserves the "defined risk" property: a
    narrower wing means smaller max loss, never larger. We never pick a
    wing closer than 1 strike from the short — that would degenerate to a
    bull/bear spread, not an iron condor.
    """
    if desired_offset_pts < strike_step:
        return None, 0

    # Build a strike → entry map for O(1) lookup
    by_strike = {float(e.strike): e for e in chain.strikes}

    # Walk from desired offset inward, in strike_step increments
    for offset in range(desired_offset_pts, strike_step - 1, -strike_step):
        candidate_strike = base_strike + direction * offset
        entry = by_strike.get(candidate_strike)
        if entry is None:
            continue
        opt = getattr(entry, opt_attr, None)
        if opt is None:
            continue
        # Accept if the leg has any pricing signal (LTP or quote)
        ltp = float(opt.ltp) if opt.ltp else 0
        bid = float(opt.bid_price) if opt.bid_price else 0
        ask = float(opt.ask_price) if opt.ask_price else 0
        if ltp > 0 or (bid > 0 and ask > 0):
            return entry, offset

    return None, 0
