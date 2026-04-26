"""Capacity curve via book-depth walk.

Re-walks each trade's recorded book snapshot at several lot-size multiples
and reports how P&L and average slippage (in bps) scale with size. The
curve tells the operator: "at what capital tier does execution start to
eat the edge?"

Depends on Phase 2A ``book_snapshot`` per trade record. Trades without
that field cannot be rewound — they fall back to the
``estimate_top_of_book_walk`` linear-extrapolation model and are flagged
as synthetic. If NO trade carries a snapshot, the caller's capacity
analysis can't run at all: we raise ``CapacityUnavailable``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import pandas as pd

from src.broker.paper.client import (
    BookLevel,
    estimate_top_of_book_walk,
    walk_book,
    walk_book_descending,
)
from src.core.types import OrderSide

logger = logging.getLogger(__name__)

# NIFTY lot is 75 contracts. Each multiple represents 1 / 2 / 4 / 10 / 20
# lots — spans the realistic range of an options-selling book from a
# single-lot retail trader through a mid-capital prop shop.
LOT_MULTIPLES: tuple[int, ...] = (75, 150, 300, 750, 1500)


class CapacityUnavailable(RuntimeError):
    """Raised when no trade in the input carries a ``book_snapshot``.

    Capacity analysis requires at least one real book observation. If the
    backtest predates Phase 2A or ran on the synthetic slippage path
    only, the harness can't meaningfully rewalk — abort loudly rather
    than silently return an all-synthetic curve.
    """


def _book_levels(snapshot_side: list) -> list[BookLevel]:
    """Turn a ``book_snapshot["bids"|"asks"]`` list into BookLevel objects.

    Tolerates entries shaped as (price, size) tuples or 2-element lists.
    Drops entries with non-positive price (a single-sided synthetic
    record from the GDFL path has size=0, which is fine to keep — qty
    shortfall is handled by ``walk_book`` imputing an extra level).
    """
    out: list[BookLevel] = []
    for lvl in snapshot_side or []:
        try:
            price = float(lvl[0])
            size = int(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price <= 0:
            continue
        out.append(BookLevel(price=price, size=size))
    return out


def _infer_lot_size(tradingsymbol: str) -> int:
    """Best-effort lot-size inference. Mirrors the private helper in
    ``src.broker.paper.client`` but kept local so capacity.py doesn't
    reach into the broker's internals.
    """
    # Minimal hard-coded map — matches src/core/constants.LOT_SIZES for
    # the three underlyings the validation harness cares about.
    prefixes = (("BANKNIFTY", 30), ("FINNIFTY", 40), ("NIFTY", 75))
    for pref, ls in prefixes:
        if tradingsymbol.startswith(pref):
            return ls
    return 75


def _match_round_trips(trades: list[dict]) -> list[tuple[dict, dict]]:
    """Pair fills into (entry, exit) round-trip tuples by tradingsymbol.

    Walks the trade list in arrival order, FIFO-matching opposite-side
    fills. Unmatched fills (dangling entries from prior sessions, partial
    hedges, etc.) are dropped — the caller only needs pairs for P&L
    attribution. Quantity mismatches are handled by splitting the larger
    fill into per-pair slices with the matched quantity.

    Returns a list of (entry, exit) trade dicts where each dict carries a
    ``quantity`` field equal to the matched slice.
    """
    pairs: list[tuple[dict, dict]] = []
    # Open lots per symbol: list of {"trade": dict, "remaining": int, "side": str}
    open_pos: dict[str, list[dict]] = {}

    for trade in trades:
        symbol = trade.get("tradingsymbol", "")
        side = str(trade.get("transaction_type", "") or "").upper()
        qty = int(trade.get("quantity", 0) or 0)
        if qty <= 0 or side not in ("BUY", "SELL"):
            continue

        book = open_pos.setdefault(symbol, [])
        opposite = "SELL" if side == "BUY" else "BUY"

        remaining = qty
        i = 0
        while remaining > 0 and i < len(book):
            pos = book[i]
            if pos["side"] == opposite and pos["remaining"] > 0:
                take = min(remaining, pos["remaining"])
                entry_slice = dict(pos["trade"])
                exit_slice = dict(trade)
                entry_slice["quantity"] = take
                exit_slice["quantity"] = take
                if pos["side"] == "BUY":
                    pairs.append((entry_slice, exit_slice))
                else:
                    # Short-first (premium selling): pair is (SELL-entry, BUY-exit).
                    pairs.append((entry_slice, exit_slice))
                pos["remaining"] -= take
                remaining -= take
                if pos["remaining"] == 0:
                    book.pop(i)
                    continue
            i += 1

        if remaining > 0:
            book.append(
                {"trade": trade, "remaining": remaining, "side": side}
            )

    return pairs


def _walk_for_fill(
    trade: dict,
    new_qty: int,
    tick_size: float,
) -> tuple[float, bool]:
    """Re-walk the book for this trade at a new quantity.

    Returns ``(new_price, used_synthetic)``. ``used_synthetic`` is True iff
    we had to fall back to ``estimate_top_of_book_walk`` (no book_snapshot
    on the record — e.g. the synthetic-slippage backtest path).
    """
    side_raw = str(trade.get("transaction_type", "") or "").upper()
    side = OrderSide.BUY if side_raw == "BUY" else OrderSide.SELL
    snap = trade.get("book_snapshot")
    avg_price = float(trade.get("average_price", 0) or 0)
    spread_half = float(trade.get("spread_half") or 0)

    if snap:
        if side == OrderSide.BUY:
            levels = _book_levels(snap.get("asks", []))
            if levels:
                vwap, filled, _ = walk_book(levels, new_qty, tick_size)
                if filled > 0:
                    return vwap, False
        else:
            levels = _book_levels(snap.get("bids", []))
            if levels:
                vwap, filled, _ = walk_book_descending(levels, new_qty, tick_size)
                if filled > 0:
                    return vwap, False

    # Fall back to top-of-book linear extrapolation. Best price is
    # approximated as fill + (side-adjusted) half-spread.
    if side == OrderSide.BUY:
        best = avg_price - spread_half if spread_half > 0 else avg_price
    else:
        best = avg_price + spread_half if spread_half > 0 else avg_price
    if best <= 0:
        best = avg_price if avg_price > 0 else 0.05

    lot_size = _infer_lot_size(str(trade.get("tradingsymbol", "") or ""))
    vwap, _ = estimate_top_of_book_walk(
        best, new_qty, side, lot_size=lot_size, tick_size=tick_size,
    )
    return vwap, True


def simulate_capacity(
    trades: list[dict],
    lot_sizes: Sequence[int] = LOT_MULTIPLES,
    tick_size: float = 0.05,
) -> pd.DataFrame:
    """Re-walk the book at each lot size and report aggregate stats.

    For every (lot_size, round-trip) we:
      1. Compute a scale factor ``lot_size / original_qty``.
      2. Re-walk both entry and exit at the scaled quantity.
      3. Attribute P&L with the scaled fill prices.

    Returns a DataFrame:
        lot_size | total_pnl | pnl_per_lot | avg_slippage_bps | num_trades_synthetic_fallback

    ``avg_slippage_bps`` is the mean over all leg fills of
    ``(new_price - old_price) / old_price * 10_000``, signed by side
    (positive = adverse slippage). ``num_trades_synthetic_fallback`` is
    the count of LEG fills that fell back to the synthetic top-of-book
    walk at that lot size.
    """
    if not trades:
        raise CapacityUnavailable(
            "simulate_capacity: trades list is empty — nothing to walk."
        )

    has_snapshot = any(t.get("book_snapshot") for t in trades)
    if not has_snapshot:
        raise CapacityUnavailable(
            "simulate_capacity: no trade in the input carries a book_snapshot — "
            "capacity analysis requires Phase 2A fill metadata. Re-run the "
            "backtest on the depth-aware path (GDFL top-of-book or Kite "
            "market-depth feed)."
        )

    pairs = _match_round_trips(trades)
    if not pairs:
        raise CapacityUnavailable(
            "simulate_capacity: no round-trip pairs could be matched from the "
            "supplied fills. The capacity curve attributes P&L via entry/exit "
            "pairs; unmatched half-fills cannot be scored."
        )

    rows: list[dict] = []
    for lot_size in lot_sizes:
        total_pnl = 0.0
        slippage_bps_sum = 0.0
        leg_count = 0
        synthetic_leg_count = 0
        paired_contracts = 0

        for entry, exit_ in pairs:
            entry_qty = int(entry.get("quantity", 0) or 0)
            if entry_qty <= 0:
                continue
            # Scale factor — may be fractional; we round to a multiple of
            # the lot size since option books only transact integer lots.
            scale = lot_size / entry_qty if entry_qty else 1.0
            new_qty = max(1, int(round(entry_qty * scale)))

            entry_price, entry_syn = _walk_for_fill(entry, new_qty, tick_size)
            exit_price, exit_syn = _walk_for_fill(exit_, new_qty, tick_size)

            old_entry = float(entry.get("average_price", 0) or 0)
            old_exit = float(exit_.get("average_price", 0) or 0)

            entry_side = str(entry.get("transaction_type", "") or "").upper()

            # Round-trip P&L — sign convention matches the metrics module.
            if entry_side == "BUY":
                pnl = new_qty * (exit_price - entry_price)
            else:
                # SELL-first (premium selling): collect entry, pay at exit.
                pnl = new_qty * (entry_price - exit_price)
            total_pnl += pnl
            paired_contracts += new_qty

            # Slippage bps per leg, signed by side — positive = adverse.
            for old, new, side in (
                (old_entry, entry_price, entry_side),
                (old_exit, exit_price, str(exit_.get("transaction_type", "") or "").upper()),
            ):
                if old > 0:
                    raw = (new - old) / old * 10_000.0
                    # BUY: adverse = price UP. SELL: adverse = price DOWN.
                    signed = raw if side == "BUY" else -raw
                    slippage_bps_sum += signed
                    leg_count += 1

            if entry_syn:
                synthetic_leg_count += 1
            if exit_syn:
                synthetic_leg_count += 1

        pnl_per_lot = total_pnl / lot_size if lot_size else 0.0
        avg_slip = slippage_bps_sum / leg_count if leg_count else 0.0

        rows.append(
            {
                "lot_size": int(lot_size),
                "total_pnl": round(total_pnl, 2),
                "pnl_per_lot": round(pnl_per_lot, 4),
                "avg_slippage_bps": round(avg_slip, 2),
                "num_trades_synthetic_fallback": int(synthetic_leg_count),
            }
        )

    return pd.DataFrame(
        rows,
        columns=[
            "lot_size",
            "total_pnl",
            "pnl_per_lot",
            "avg_slippage_bps",
            "num_trades_synthetic_fallback",
        ],
    )
