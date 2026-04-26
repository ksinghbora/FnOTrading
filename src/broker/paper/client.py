"""Paper trading broker — simulates order execution without real money.

Uses the same BrokerClient interface, so strategies run identically
in paper mode vs live mode with zero code changes.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from src.broker.base import BrokerClient
from src.broker.paper.slippage import SlippageModel
from src.core.clock import now_ist
from src.core.types import OrderSide, OrderType, ProductType

logger = logging.getLogger(__name__)

# Callable returning (bid, ask) for a tradingsymbol, or (None, None) if unknown.
QuoteProvider = Callable[[str], tuple[float | None, float | None]]


# ─── Book depth support ────────────────────────────────────────────────
# GDFL tick feed only exposes top-of-book (BuyPrice/SellPrice). Kite
# market-depth payload exposes up to 5 levels per side. Strategies using
# NIFTY lot=75 frequently walk 2-3 levels on strangle wings, so a single
# best-bid/best-ask fill is 20-40 bps too optimistic on OTM legs.
#
# This module supports a richer "depth-aware" provider that returns
# ordered price/size levels. When only top-of-book is available we fall
# back to a linear-extrapolation penalty: one NSE tick (0.05) per
# additional lot above the first, labelled `book_depth_estimated`.


@dataclass
class BookLevel:
    """One level of the order book."""

    price: float
    size: int  # contracts (not lots)


@dataclass
class DepthQuote:
    """Top-N depth snapshot returned by a DepthQuoteProvider.

    bid_levels and ask_levels must be ordered from best to worst (bid
    descending, ask ascending). An empty list means "no depth available
    on this side". ``tick_size`` defaults to NSE 0.05 for NFO.
    """

    bid_levels: list[BookLevel] = field(default_factory=list)
    ask_levels: list[BookLevel] = field(default_factory=list)
    tick_size: float = 0.05

    @property
    def best_bid(self) -> float | None:
        return self.bid_levels[0].price if self.bid_levels else None

    @property
    def best_ask(self) -> float | None:
        return self.ask_levels[0].price if self.ask_levels else None


# Callable returning a DepthQuote or None. Paper broker prefers this over
# the legacy top-of-book QuoteProvider when both are set.
DepthQuoteProvider = Callable[[str], DepthQuote | None]


# NSE tick size on NFO (options segment). Used as the walk-penalty step
# when only top-of-book is available.
_NFO_TICK_SIZE = 0.05


def walk_book(
    levels: list[BookLevel],
    quantity: int,
    tick_size: float = _NFO_TICK_SIZE,
) -> tuple[float, int, list[tuple[float, int]]]:
    """Walk an ordered book side, consuming ``quantity`` contracts.

    Returns ``(vwap, filled_qty, consumed)``:
      - vwap: volume-weighted average price across consumed levels.
      - filled_qty: how many contracts were actually filled (``<= quantity``).
      - consumed: ordered list of (price, size) pairs actually taken.

    If the book exhausts before quantity is filled, the last price is
    walked one tick further to price the tail (models a rester/MM stepping
    up rather than a gap). The synthetic extra level is included in
    ``consumed`` with its imputed fill size so the caller can detect the
    walk-through (``used_synthetic = last price wasn't in input levels``).
    """
    if quantity <= 0:
        return 0.0, 0, []
    if not levels:
        return 0.0, 0, []

    remaining = quantity
    notional = 0.0
    filled = 0
    consumed: list[tuple[float, int]] = []

    for lvl in levels:
        if remaining <= 0:
            break
        take = min(remaining, max(0, int(lvl.size)))
        if take == 0:
            continue
        notional += take * float(lvl.price)
        filled += take
        remaining -= take
        consumed.append((float(lvl.price), take))

    if remaining > 0:
        # Book exhausted — impute one more level at last_price + tick.
        last_price = consumed[-1][0] if consumed else float(levels[-1].price)
        step = float(tick_size) if tick_size and tick_size > 0 else _NFO_TICK_SIZE
        synth_price = last_price + step
        notional += remaining * synth_price
        filled += remaining
        consumed.append((synth_price, remaining))
        remaining = 0

    vwap = notional / filled if filled > 0 else 0.0
    return vwap, filled, consumed


def walk_book_descending(
    levels: list[BookLevel],
    quantity: int,
    tick_size: float = _NFO_TICK_SIZE,
) -> tuple[float, int, list[tuple[float, int]]]:
    """Walk bid side (descending prices) consuming ``quantity`` contracts."""
    if quantity <= 0:
        return 0.0, 0, []
    if not levels:
        return 0.0, 0, []

    remaining = quantity
    notional = 0.0
    filled = 0
    consumed: list[tuple[float, int]] = []

    for lvl in levels:
        if remaining <= 0:
            break
        take = min(remaining, max(0, int(lvl.size)))
        if take == 0:
            continue
        notional += take * float(lvl.price)
        filled += take
        remaining -= take
        consumed.append((float(lvl.price), take))

    if remaining > 0:
        last_price = consumed[-1][0] if consumed else float(levels[-1].price)
        step = float(tick_size) if tick_size and tick_size > 0 else _NFO_TICK_SIZE
        synth_price = max(step, last_price - step)  # don't go negative
        notional += remaining * synth_price
        filled += remaining
        consumed.append((synth_price, remaining))
        remaining = 0

    vwap = notional / filled if filled > 0 else 0.0
    return vwap, filled, consumed


def estimate_top_of_book_walk(
    best_price: float,
    quantity: int,
    side: OrderSide,
    lot_size: int = 75,
    tick_size: float = _NFO_TICK_SIZE,
) -> tuple[float, list[tuple[float, int]]]:
    """Fallback when only best-bid / best-ask is available (no depth).

    Assumes ~1 lot at the best level, then steps by one tick per
    additional lot. BUY walks up (cost rises), SELL walks down.
    This is a coarse linear extrapolation; real books can step 2-5 ticks
    on OTM NIFTY wings. Mark telemetry with `book_depth_estimated`.
    """
    lot_size = max(1, int(lot_size))
    tick_size = float(tick_size) if tick_size and tick_size > 0 else _NFO_TICK_SIZE
    qty_remaining = max(0, int(quantity))
    if qty_remaining <= 0 or best_price <= 0:
        return float(best_price), []

    direction = 1.0 if side == OrderSide.BUY else -1.0
    consumed: list[tuple[float, int]] = []
    notional = 0.0
    filled = 0
    step_idx = 0
    while qty_remaining > 0:
        take = min(qty_remaining, lot_size)
        price = float(best_price) + direction * tick_size * step_idx
        if side == OrderSide.SELL:
            price = max(tick_size, price)  # never negative on sell walks
        notional += take * price
        consumed.append((price, take))
        filled += take
        qty_remaining -= take
        step_idx += 1

    vwap = notional / filled if filled > 0 else float(best_price)
    return vwap, consumed


def _infer_lot_size(tradingsymbol: str) -> int:
    """Best-effort lot-size inference from tradingsymbol. NIFTY-focused."""
    from src.core.constants import LOT_SIZES

    for underlying, ls in sorted(LOT_SIZES.items(), key=lambda kv: -len(kv[0])):
        if tradingsymbol.startswith(underlying):
            return int(ls)
    return 75  # NIFTY default


class PaperPosition:
    """Internal position tracking for paper trading."""

    def __init__(self, tradingsymbol: str, exchange: str):
        self.tradingsymbol = tradingsymbol
        self.exchange = exchange
        self.quantity = 0
        self.average_price = 0.0
        self.pnl = 0.0


class PaperBrokerClient(BrokerClient):
    """Simulated broker for paper trading and backtesting.

    - Orders are filled immediately at LTP (or specified price)
    - Positions are tracked in memory
    - No real API calls are made
    """

    MAX_ORDER_HISTORY = 5000
    MAX_TRADE_HISTORY = 5000

    def __init__(
        self,
        initial_capital: float = 1_000_000,
        slippage: SlippageModel | None = None,
        quote_provider: QuoteProvider | None = None,
        depth_provider: DepthQuoteProvider | None = None,
    ):
        self._connected = False
        self._capital = initial_capital
        self._available_margin = initial_capital
        self._orders: list[dict] = []
        self._trades: list[dict] = []
        self._positions: dict[str, PaperPosition] = {}
        self._ltp_cache: dict[str, float] = {}
        # Pass slippage=None explicitly to disable (e.g., legacy backtest tests).
        self.slippage = slippage if slippage is not None else SlippageModel()
        self._vix: float = 0.0  # Updated by data feed via set_vix()
        # Optional callable: tradingsymbol -> (bid, ask). When set and a
        # realistic (>0) bid/ask is returned, fills cross the spread
        # (BUY @ ask, SELL @ bid) instead of applying the LTP-based
        # SlippageModel. Used by the GDFL backtest path where real
        # quotes are available; synthetic path leaves it None and keeps
        # SlippageModel. See F2 in the roadmap.
        self.quote_provider: QuoteProvider | None = quote_provider
        # Optional callable: tradingsymbol -> DepthQuote. Preferred over
        # ``quote_provider`` when set. Enables book-walk fills with VWAP
        # across multiple levels. GDFL only exposes top-of-book so this
        # is primarily for Kite live feeds (market-depth payload) and
        # tests; the GDFL path continues to hit the
        # top-of-book-estimated fallback with a tick-per-lot penalty.
        self.depth_provider: DepthQuoteProvider | None = depth_provider

    async def connect(self) -> None:
        self._connected = True
        logger.info(f"Paper broker connected (capital: {self._capital:,.0f})")

    async def disconnect(self) -> None:
        self._connected = False
        logger.info("Paper broker disconnected")

    def is_connected(self) -> bool:
        return self._connected

    def set_ltp(self, tradingsymbol: str, price: float) -> None:
        """Set LTP for an instrument (called by market data feed during paper trading)."""
        self._ltp_cache[tradingsymbol] = price

    def set_vix(self, vix: float) -> None:
        """Set current VIX (called by data feed). Drives slippage regime multiplier."""
        self._vix = vix

    async def place_order(
        self,
        tradingsymbol: str,
        exchange: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType = OrderType.MARKET,
        product: ProductType = ProductType.NRML,
        price: float = 0,
        trigger_price: float = 0,
        tag: str = "",
    ) -> str:
        order_id = str(uuid.uuid4())[:8]
        ltp = price if price > 0 else self._ltp_cache.get(tradingsymbol, 0)

        if ltp <= 0:
            logger.warning(f"[PAPER] No LTP for {tradingsymbol}, cannot fill order")
            raise ValueError(f"No LTP available for {tradingsymbol}")

        now = now_ist()

        # ─── Fill model selection (F2 + book-depth walk) ───────────
        # Priority:
        #   1. depth_provider (Kite market-depth) → walk levels, VWAP.
        #   2. quote_provider (GDFL top-of-book) → spread + per-lot
        #      tick-penalty fallback for size > 1 lot.
        #   3. SlippageModel (synthetic BS backtests).
        #
        # LIMIT semantics from commit 8e1060d are preserved: a passive
        # LIMIT inside the spread still fills at the strategy's price
        # (up to the size available on that side).
        bid: float | None = None
        ask: float | None = None
        depth_quote: DepthQuote | None = None

        if self.depth_provider is not None:
            try:
                depth_quote = self.depth_provider(tradingsymbol)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    f"[PAPER] depth_provider raised for {tradingsymbol}: {exc}"
                )
                depth_quote = None
            if depth_quote is not None:
                bid = depth_quote.best_bid
                ask = depth_quote.best_ask

        if depth_quote is None and self.quote_provider is not None:
            try:
                bid, ask = self.quote_provider(tradingsymbol)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[PAPER] quote_provider raised for {tradingsymbol}: {exc}")
                bid = ask = None

        used_spread = False
        used_limit_in_band = False
        used_book_walk = False
        used_depth_estimated = False
        book_walk_slippage_pct = 0.0
        consumed_levels: list[tuple[float, int]] = []

        if bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid:
            # Real quote available. Fill model depends on order type.
            #
            # LIMIT: honour the limit price when it's inside [bid, ask]
            # (passive fill at your quote, captures the spread the
            # strategy targeted). When aggressive (BUY ≥ ask / SELL ≤
            # bid) cross at the opposing edge — walk the book if depth
            # available. When passive beyond the book (BUY < bid /
            # SELL > ask) a real broker would rest the order — backtest
            # models this as an aggressive cross with a warning so we
            # don't silently lose a leg. F1b will tighten this with a
            # reprice loop.
            #
            # MARKET (or LIMIT without a sane price): BUY lifts the
            # offer, SELL hits the bid — walk if depth available.
            if order_type == OrderType.LIMIT and price > 0:
                if side == OrderSide.BUY:
                    if price >= ask:
                        fill_price, consumed_levels, used_book_walk, used_depth_estimated = \
                            self._compute_walk_fill(
                                side, quantity, depth_quote, bid, ask, tradingsymbol,
                            )
                    elif price >= bid:
                        fill_price = float(price)
                        used_limit_in_band = True
                    else:
                        logger.warning(
                            f"[PAPER] LIMIT BUY {price:.2f} below bid {bid:.2f} "
                            f"for {tradingsymbol} — filling aggressive at ask {ask:.2f}"
                        )
                        fill_price, consumed_levels, used_book_walk, used_depth_estimated = \
                            self._compute_walk_fill(
                                side, quantity, depth_quote, bid, ask, tradingsymbol,
                            )
                else:  # SELL
                    if price <= bid:
                        fill_price, consumed_levels, used_book_walk, used_depth_estimated = \
                            self._compute_walk_fill(
                                side, quantity, depth_quote, bid, ask, tradingsymbol,
                            )
                    elif price <= ask:
                        fill_price = float(price)
                        used_limit_in_band = True
                    else:
                        logger.warning(
                            f"[PAPER] LIMIT SELL {price:.2f} above ask {ask:.2f} "
                            f"for {tradingsymbol} — filling aggressive at bid {bid:.2f}"
                        )
                        fill_price, consumed_levels, used_book_walk, used_depth_estimated = \
                            self._compute_walk_fill(
                                side, quantity, depth_quote, bid, ask, tradingsymbol,
                            )
            else:
                fill_price, consumed_levels, used_book_walk, used_depth_estimated = \
                    self._compute_walk_fill(
                        side, quantity, depth_quote, bid, ask, tradingsymbol,
                    )

            # Book-walk slippage %: difference between VWAP and best
            # opposing quote on the traded side (what you *would* have
            # paid if top-of-book had infinite size). Kept as a separate
            # attribution field so we can diff against the flat
            # SlippageModel during post-trade analysis.
            reference = float(ask) if side == OrderSide.BUY else float(bid)
            if reference > 0 and (used_book_walk or used_depth_estimated):
                book_walk_slippage_pct = (
                    (fill_price - reference) / reference * 100.0
                    if side == OrderSide.BUY
                    else (reference - fill_price) / reference * 100.0
                )

            # slip_bps reported relative to LTP so telemetry stays
            # comparable across paths.
            slip_bps = abs(fill_price - ltp) / ltp * 10_000.0 if ltp > 0 else 0.0
            used_spread = True
        elif self.slippage is not None:
            # Tiered slippage — closes paper-to-live P&L gap.
            # Slippage dimensions: liquidity (premium proxy) × time × VIX × size.
            fill_price, slip_bps = self.slippage.apply(
                ltp, side, self._vix, quantity, now.time()
            )
        else:
            fill_price, slip_bps = ltp, 0.0

        if used_depth_estimated:
            fill_source = "book_depth_estimated"
        elif used_book_walk:
            fill_source = "book_walk"
        elif used_limit_in_band:
            fill_source = "limit_in_band"
        elif used_spread:
            fill_source = "spread"
        else:
            fill_source = "slippage_model"

        # ─── Per-fill validation metadata (cost/capacity harness) ─────
        # spread_half: absolute price units (always positive). Falls back
        # to 0.5% of premium if only one side is known; 0.0 if neither.
        # book_snapshot: up to 5 levels per side, or single level for the
        # GDFL top-of-book path. None when no quote is available.
        spread_half, book_snapshot = self._build_fill_metadata(
            bid, ask, fill_price, depth_quote,
        )

        order = {
            "order_id": order_id,
            "tradingsymbol": tradingsymbol,
            "exchange": exchange,
            "transaction_type": side.value,
            "quantity": quantity,
            "order_type": order_type.value,
            "product": product.value,
            "price": price,
            "trigger_price": trigger_price,
            "average_price": fill_price,
            "filled_quantity": quantity,
            "status": "COMPLETE",
            "status_message": "Paper trade filled",
            "tag": tag,
            "order_timestamp": now.isoformat(),
            "exchange_timestamp": now.isoformat(),
            "ltp": ltp,
            "slippage_bps": slip_bps,
            "fill_source": fill_source,
            "bid": bid if used_spread else None,
            "ask": ask if used_spread else None,
            # Book-walk attribution — how much the VWAP diverged from
            # best opposing quote. Distinguishes "wide spread" cost
            # from "walked through levels" cost in post-trade analysis.
            "book_walk_slippage_pct": round(book_walk_slippage_pct, 4),
            "book_walk_levels": [
                {"price": round(p, 2), "size": int(s)} for p, s in consumed_levels
            ] if consumed_levels else [],
            # Consumed downstream by the cost-sensitivity / capacity
            # validation harness; callers that don't need them can ignore.
            "spread_half": spread_half,
            "book_snapshot": book_snapshot,
        }

        self._orders.append(order)
        if len(self._orders) > self.MAX_ORDER_HISTORY:
            self._orders = self._orders[-self.MAX_ORDER_HISTORY:]
        self._trades.append({
            "trade_id": str(uuid.uuid4())[:8],
            "order_id": order_id,
            "tradingsymbol": tradingsymbol,
            "exchange": exchange,
            "transaction_type": side.value,
            "quantity": quantity,
            "average_price": fill_price,
            "fill_timestamp": now.isoformat(),
            "spread_half": spread_half,
            "book_snapshot": book_snapshot,
        })
        if len(self._trades) > self.MAX_TRADE_HISTORY:
            self._trades = self._trades[-self.MAX_TRADE_HISTORY:]

        # Update position
        self._update_position(tradingsymbol, exchange, side, quantity, fill_price)

        if used_spread:
            src_tag = fill_source
            walk_bits = (
                f" walk_slip={book_walk_slippage_pct:.2f}% "
                f"levels={len(consumed_levels)}"
                if (used_book_walk or used_depth_estimated)
                else ""
            )
            if used_depth_estimated:
                logger.info(
                    f"[BOOK_DEPTH_ESTIMATED] {side.value} {quantity} {tradingsymbol} "
                    f"fill={fill_price:.2f} best={ask if side == OrderSide.BUY else bid:.2f} "
                    f"walk_slip={book_walk_slippage_pct:.2f}% (no depth available)"
                )
            logger.info(
                f"[PAPER] {side.value} {quantity} {tradingsymbol} @ {fill_price:.2f} "
                f"(ltp={ltp:.2f}, bid={bid:.2f}, ask={ask:.2f}, "
                f"slip={slip_bps:.0f}bps, src={src_tag},{walk_bits} order={order_id})"
            )
        else:
            logger.info(
                f"[PAPER] {side.value} {quantity} {tradingsymbol} @ {fill_price:.2f} "
                f"(ltp={ltp:.2f}, slip={slip_bps:.0f}bps, src=slippage_model, order={order_id})"
            )
        return order_id

    async def modify_order(
        self,
        order_id: str,
        quantity: int | None = None,
        price: float | None = None,
        trigger_price: float | None = None,
        order_type: OrderType | None = None,
    ) -> str:
        logger.info(f"[PAPER] Order modified: {order_id}")
        return order_id

    async def cancel_order(self, order_id: str) -> str:
        for order in self._orders:
            if order["order_id"] == order_id:
                order["status"] = "CANCELLED"
        logger.info(f"[PAPER] Order cancelled: {order_id}")
        return order_id

    async def get_order_status(self, order_id: str) -> dict:
        for order in self._orders:
            if order["order_id"] == order_id:
                return order
        return {}

    async def get_orders(self) -> list[dict]:
        return self._orders

    async def get_trades(self) -> list[dict]:
        return self._trades

    async def get_positions(self) -> dict:
        day_positions = []
        net_positions = []
        for pos in self._positions.values():
            entry = {
                "tradingsymbol": pos.tradingsymbol,
                "exchange": pos.exchange,
                "quantity": pos.quantity,
                "average_price": pos.average_price,
                "last_price": self._ltp_cache.get(pos.tradingsymbol, pos.average_price),
                "pnl": pos.pnl,
                "product": "NRML",
            }
            day_positions.append(entry)
            net_positions.append(entry)
        return {"day": day_positions, "net": net_positions}

    async def get_holdings(self) -> list[dict]:
        return []

    async def get_ltp(self, instruments: list[str]) -> dict[str, float]:
        result = {}
        for inst in instruments:
            symbol = inst.split(":")[-1] if ":" in inst else inst
            result[inst] = self._ltp_cache.get(symbol, 0)
        return result

    async def get_quote(self, instruments: list[str]) -> dict:
        result = {}
        for inst in instruments:
            symbol = inst.split(":")[-1] if ":" in inst else inst
            ltp = self._ltp_cache.get(symbol, 0)
            result[inst] = {
                "last_price": ltp,
                "ohlc": {"open": ltp, "high": ltp, "low": ltp, "close": ltp},
                "depth": {"buy": [], "sell": []},
            }
        return result

    async def get_historical_data(
        self,
        instrument_token: int,
        from_date: datetime,
        to_date: datetime,
        interval: str,
    ) -> list[dict]:
        return []

    async def get_instruments(self, exchange: str = "") -> list[dict]:
        return []

    async def get_margins(self) -> dict:
        return {
            "equity": {
                "available": {"cash": self._available_margin},
                "utilised": {"debits": self._capital - self._available_margin},
            }
        }

    async def get_order_margins(self, orders: list[dict]) -> list[dict]:
        return [{"total": 0} for _ in orders]

    def _compute_walk_fill(
        self,
        side: OrderSide,
        quantity: int,
        depth_quote: DepthQuote | None,
        bid: float,
        ask: float,
        tradingsymbol: str,
    ) -> tuple[float, list[tuple[float, int]], bool, bool]:
        """Compute a book-walking fill VWAP.

        Returns ``(fill_price, consumed_levels, used_book_walk, used_depth_estimated)``.

        Priority:
        * If ``depth_quote`` exposes >=1 level on the traded side, walk it.
        * Otherwise fall back to per-lot linear extrapolation on the best
          price (one NSE tick per extra lot). Sets ``used_depth_estimated``
          so telemetry captures the guess.
        """
        # Full-depth path
        if depth_quote is not None:
            if side == OrderSide.BUY and depth_quote.ask_levels:
                fill, filled, consumed = walk_book(
                    depth_quote.ask_levels, quantity, depth_quote.tick_size
                )
                if filled > 0:
                    return fill, consumed, True, False
            if side == OrderSide.SELL and depth_quote.bid_levels:
                fill, filled, consumed = walk_book_descending(
                    depth_quote.bid_levels, quantity, depth_quote.tick_size
                )
                if filled > 0:
                    return fill, consumed, True, False

        # Top-of-book-only fallback
        best = float(ask) if side == OrderSide.BUY else float(bid)
        lot_size = _infer_lot_size(tradingsymbol)
        fill, consumed = estimate_top_of_book_walk(
            best, quantity, side, lot_size=lot_size,
        )
        return fill, consumed, False, True

    @staticmethod
    def _build_fill_metadata(
        bid: float | None,
        ask: float | None,
        fill_price: float,
        depth_quote: DepthQuote | None,
    ) -> tuple[float, dict | None]:
        """Return (spread_half, book_snapshot) for the fill record.

        Inputs mirror what the place_order path already captured:
        ``bid``/``ask`` may be None when no quote was available,
        ``depth_quote`` is the full N-level snapshot when a depth_provider
        is wired. Output is always serialisable (no numpy/Decimal leaks).
        """
        bid_f: float | None = float(bid) if bid and bid > 0 else None
        ask_f: float | None = float(ask) if ask and ask > 0 else None

        # spread_half — absolute price units, always >= 0.
        # Real two-sided quote → exact half-spread.
        # One-sided (rare: crossed book, single-leg halts) → 0.5% of
        # premium as a conservative floor. No quote at all (broker fell
        # through to SlippageModel) → 0.0; the validation harness
        # interprets that as "no observed spread".
        if bid_f is not None and ask_f is not None and ask_f >= bid_f:
            spread_half = (ask_f - bid_f) / 2.0
        elif bid_f is not None or ask_f is not None:
            ref = fill_price if fill_price > 0 else (bid_f or ask_f or 0.0)
            spread_half = 0.005 * float(ref)
        else:
            spread_half = 0.0
        spread_half = max(0.0, float(spread_half))

        # book_snapshot — up to 5 levels per side. None when we have no
        # quote at all (broker fell through to SlippageModel / LTP).
        book_snapshot: dict | None = None
        if depth_quote is not None and (
            depth_quote.bid_levels or depth_quote.ask_levels
        ):
            book_snapshot = {
                "bids": [
                    (float(lvl.price), int(lvl.size))
                    for lvl in depth_quote.bid_levels[:5]
                ],
                "asks": [
                    (float(lvl.price), int(lvl.size))
                    for lvl in depth_quote.ask_levels[:5]
                ],
            }
        elif bid_f is not None or ask_f is not None:
            book_snapshot = {
                "bids": [(bid_f, 0)] if bid_f is not None else [],
                "asks": [(ask_f, 0)] if ask_f is not None else [],
            }

        return spread_half, book_snapshot

    def _update_position(
        self,
        tradingsymbol: str,
        exchange: str,
        side: OrderSide,
        quantity: int,
        price: float,
    ) -> None:
        if tradingsymbol not in self._positions:
            self._positions[tradingsymbol] = PaperPosition(tradingsymbol, exchange)

        pos = self._positions[tradingsymbol]
        signed_qty = quantity if side == OrderSide.BUY else -quantity

        if pos.quantity == 0:
            pos.quantity = signed_qty
            pos.average_price = price
        elif (pos.quantity > 0 and side == OrderSide.BUY) or (
            pos.quantity < 0 and side == OrderSide.SELL
        ):
            # Adding to position
            total_value = abs(pos.quantity) * pos.average_price + quantity * price
            pos.quantity += signed_qty
            if pos.quantity != 0:
                pos.average_price = total_value / abs(pos.quantity)
        else:
            # Reducing or reversing position
            close_qty = min(abs(pos.quantity), quantity)
            if pos.quantity > 0:
                pos.pnl += close_qty * (price - pos.average_price)
            else:
                pos.pnl += close_qty * (pos.average_price - price)
            pos.quantity += signed_qty
            if pos.quantity == 0:
                pos.average_price = 0
            elif abs(signed_qty) > abs(pos.quantity - signed_qty):
                pos.average_price = price
