"""Fill-cost sensitivity curve for backtest validation.

Re-prices every trade at a widened/narrowed spread and re-runs metrics.
The gate is Sharpe > 0 at +50% spread: a strategy that collapses when
the spread widens half a turn is fragile and should not be deployed.

Depends on Phase 2A metadata (``spread_half`` per trade record). Trades
missing that field fall back to 50 bps of the fill price as a coarse
spread estimate — flagged in warnings so operators notice.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from src.backtest.metrics import calculate_metrics

logger = logging.getLogger(__name__)

# Default spread-shift grid. Units: fraction of spread_half.
# - -1.0: fill crosses inside the mid (optimistic — rest captures edge)
# -  0.0: fill at original recorded price (identity check)
# - +0.5: the key gate — half a turn wider than observed
# - +1.0: full extra turn (scenario for a stressed market)
DEFAULT_SHIFTS: tuple[float, ...] = (-1.0, -0.5, -0.25, 0.0, 0.25, 0.5, 1.0)

# Fallback spread_half when the trade record predates Phase 2A metadata
# and carries no ``spread_half``. 0.5% of average_price = 50 bps each side
# — matches the "one-sided quote" branch in PaperBrokerClient.
_FALLBACK_SPREAD_HALF_PCT = 0.005


def _normalise_side(raw: Any) -> str:
    """Return ``BUY`` or ``SELL`` (upper-case) for a trade's side field.

    Tolerates upper/lower/mixed case. Returns empty string if the field
    is missing or unparseable — callers treat that as "skip".
    """
    if raw is None:
        return ""
    return str(raw).strip().upper()


def _spread_half_for(trade: dict) -> float | None:
    """Resolve ``spread_half`` for a trade or fall back to 50bps of fill.

    Returns None iff neither ``spread_half`` nor ``average_price`` is
    available — the caller must drop this trade from the shifted set.
    """
    raw = trade.get("spread_half")
    if raw is not None:
        try:
            val = float(raw)
            if val >= 0:
                return val
        except (TypeError, ValueError):
            pass

    avg = trade.get("average_price")
    try:
        avg_f = float(avg) if avg is not None else 0.0
    except (TypeError, ValueError):
        avg_f = 0.0

    if avg_f <= 0:
        return None

    return _FALLBACK_SPREAD_HALF_PCT * avg_f


def shift_fill_pnl(trade: dict, spread_shift_pct: float) -> dict:
    """Return a new trade dict with ``average_price`` shifted by the spread.

    Sign convention — ``spread_shift_pct > 0`` = wider spread, hurts the
    trader:
      * BUY fills move UP (pay more)
      * SELL fills move DOWN (receive less)
    For ``spread_shift_pct < 0`` the fill moves inside the mid (beneficial).

    Input ``trade`` is never mutated. If the trade has neither
    ``spread_half`` nor ``average_price`` the original dict is returned
    unchanged and a warning is emitted (the caller decides whether to
    drop it from the list).
    """
    side = _normalise_side(trade.get("transaction_type"))
    if side not in ("BUY", "SELL"):
        logger.warning(
            "cost_sensitivity: unknown transaction_type=%r, leaving trade untouched",
            trade.get("transaction_type"),
        )
        return dict(trade)

    spread_half = _spread_half_for(trade)
    if spread_half is None:
        logger.warning(
            "cost_sensitivity: trade has no spread_half and no average_price, skipping "
            "(tradingsymbol=%r, order_id=%r)",
            trade.get("tradingsymbol"),
            trade.get("order_id"),
        )
        return dict(trade)

    avg = trade.get("average_price")
    try:
        avg_f = float(avg) if avg is not None else 0.0
    except (TypeError, ValueError):
        avg_f = 0.0

    direction = 1.0 if side == "BUY" else -1.0
    new_price = avg_f + direction * spread_half * float(spread_shift_pct)

    # SELL fills must not cross zero — if the shift is so deeply negative
    # that it would price a receipt below zero, clamp to a tick to keep
    # downstream P&L math sane (matches PaperBrokerClient's sell-walk clamp).
    if side == "SELL" and new_price <= 0:
        new_price = max(0.05, avg_f * 0.01)

    out = dict(trade)
    out["average_price"] = float(new_price)
    return out


def sensitivity_curve(
    trades: list[dict],
    shifts: Sequence[float] = DEFAULT_SHIFTS,
    initial_capital: float = 1_000_000.0,
) -> dict[float, dict]:
    """Compute ``metrics_dict`` at each spread shift.

    For each ``shift`` we:
      1. Re-price every trade via ``shift_fill_pnl``.
      2. Reconstruct a daily P&L curve from the shifted trades (one bucket
         per fill-pair P&L, since the trade list is fills not rounds).
      3. Pass the synthetic curve + shifted trades to ``calculate_metrics``.

    Trades with neither ``spread_half`` nor ``average_price`` are skipped
    (they'd produce nonsense P&L). The call is stable across shifts: if
    the input is degenerate, every shift returns the empty-metrics dict.
    """
    results: dict[float, dict] = {}
    for shift in shifts:
        shifted: list[dict] = []
        for trade in trades:
            sh = shift_fill_pnl(trade, shift)
            # shift_fill_pnl returns the original dict untouched when it
            # can't resolve spread_half/avg — detect that and drop.
            if trade.get("spread_half") is None and trade.get("average_price") in (None, 0, 0.0):
                # Both missing → drop.
                continue
            shifted.append(sh)

        pnl_curve = _round_trip_pnl_curve(shifted)
        metrics = calculate_metrics(pnl_curve, shifted, initial_capital)
        results[float(shift)] = metrics

    return results


def sensitivity_accepted(
    curve: dict[float, dict], threshold_shift: float = 0.5
) -> bool:
    """Return True iff Sharpe at ``threshold_shift`` is strictly > 0.

    Looks up the closest key to ``threshold_shift`` (floating-point keys
    from the curve dict) — if the caller built the curve with the default
    shifts, the +0.5 entry is an exact match.
    """
    if not curve:
        return False
    # Tolerant key lookup: map of rounded→original so +0.5 ≡ +0.5000000001.
    target = round(float(threshold_shift), 6)
    best_key = None
    best_diff = float("inf")
    for k in curve:
        diff = abs(round(float(k), 6) - target)
        if diff < best_diff:
            best_diff = diff
            best_key = k
    if best_key is None:
        return False
    metrics = curve[best_key]
    sharpe = metrics.get("sharpe_ratio", 0.0)
    # sharpe can be str "inf" from calculate_metrics — guard against it.
    try:
        return float(sharpe) > 0.0
    except (TypeError, ValueError):
        return False


def _round_trip_pnl_curve(trades: list[dict]) -> list[float]:
    """Pair fills into round trips and emit per-trade P&L.

    Mirrors the ``_compute_trade_pnls`` helper in ``src.backtest.metrics``
    but keeps its own copy so cost_sensitivity doesn't depend on a private
    function. Returns a list of float P&Ls — the length matches
    ``calculate_metrics`` downstream which treats each element as a
    daily-bucket value (fine for Sharpe comparison; the curve's shape
    between shifts is what we're measuring, not absolute values).
    """
    pnls: list[float] = []
    positions: dict[str, list[dict]] = {}

    for trade in trades:
        symbol = trade.get("tradingsymbol", "")
        side = _normalise_side(trade.get("transaction_type"))
        price = float(trade.get("average_price", 0) or 0)
        qty = int(trade.get("quantity", 0) or 0)

        if qty <= 0 or price <= 0 or side not in ("BUY", "SELL"):
            continue

        positions.setdefault(symbol, [])
        opposite = "SELL" if side == "BUY" else "BUY"

        remaining = qty
        for pos in positions[symbol]:
            if remaining <= 0:
                break
            if pos["side"] == opposite and pos["qty"] > 0:
                close_qty = min(remaining, pos["qty"])
                if opposite == "BUY":
                    pnl = close_qty * (price - pos["price"])
                else:
                    pnl = close_qty * (pos["price"] - price)
                pnls.append(float(pnl))
                pos["qty"] -= close_qty
                remaining -= close_qty

        if remaining > 0:
            positions[symbol].append({"qty": remaining, "price": price, "side": side})

    return pnls
