"""Paper trading broker — simulates order execution without real money.

Uses the same BrokerClient interface, so strategies run identically
in paper mode vs live mode with zero code changes.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from src.broker.base import BrokerClient
from src.broker.paper.slippage import SlippageModel
from src.core.clock import now_ist
from src.core.types import OrderSide, OrderType, ProductType

logger = logging.getLogger(__name__)

# Callable returning (bid, ask) for a tradingsymbol, or (None, None) if unknown.
QuoteProvider = Callable[[str], tuple[float | None, float | None]]


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

        # ─── Fill model selection (F2) ──────────────────────────────
        # Prefer crossing the real spread when we have one (GDFL path).
        # BUY crosses ask, SELL crosses bid. Falls back to the tiered
        # LTP-based SlippageModel for the synthetic-BS path where
        # bid/ask are effectively LTP.
        bid: float | None = None
        ask: float | None = None
        if self.quote_provider is not None:
            try:
                bid, ask = self.quote_provider(tradingsymbol)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[PAPER] quote_provider raised for {tradingsymbol}: {exc}")
                bid = ask = None

        used_spread = False
        used_limit_in_band = False
        if bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid:
            # Real quote available. Fill model depends on order type.
            #
            # LIMIT: honour the limit price when it's inside [bid, ask]
            # (passive fill at your quote, captures the spread the
            # strategy targeted). When aggressive (BUY ≥ ask / SELL ≤
            # bid) cross at the opposing edge. When passive beyond the
            # book (BUY < bid / SELL > ask) a real broker would rest
            # the order — backtest models this as an aggressive cross
            # with a warning so we don't silently lose a leg. F1b will
            # tighten this with a reprice loop.
            #
            # MARKET (or LIMIT without a sane price): BUY lifts the
            # offer, SELL hits the bid.
            if order_type == OrderType.LIMIT and price > 0:
                if side == OrderSide.BUY:
                    if price >= ask:
                        fill_price = float(ask)
                    elif price >= bid:
                        fill_price = float(price)
                        used_limit_in_band = True
                    else:
                        logger.warning(
                            f"[PAPER] LIMIT BUY {price:.2f} below bid {bid:.2f} "
                            f"for {tradingsymbol} — filling aggressive at ask {ask:.2f}"
                        )
                        fill_price = float(ask)
                else:  # SELL
                    if price <= bid:
                        fill_price = float(bid)
                    elif price <= ask:
                        fill_price = float(price)
                        used_limit_in_band = True
                    else:
                        logger.warning(
                            f"[PAPER] LIMIT SELL {price:.2f} above ask {ask:.2f} "
                            f"for {tradingsymbol} — filling aggressive at bid {bid:.2f}"
                        )
                        fill_price = float(bid)
            else:
                if side == OrderSide.BUY:
                    fill_price = float(ask)
                else:
                    fill_price = float(bid)
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
            "fill_source": (
                "limit_in_band" if used_limit_in_band
                else "spread" if used_spread
                else "slippage_model"
            ),
            "bid": bid if used_spread else None,
            "ask": ask if used_spread else None,
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
        })
        if len(self._trades) > self.MAX_TRADE_HISTORY:
            self._trades = self._trades[-self.MAX_TRADE_HISTORY:]

        # Update position
        self._update_position(tradingsymbol, exchange, side, quantity, fill_price)

        if used_spread:
            src_tag = "limit_in_band" if used_limit_in_band else "spread"
            logger.info(
                f"[PAPER] {side.value} {quantity} {tradingsymbol} @ {fill_price:.2f} "
                f"(ltp={ltp:.2f}, bid={bid:.2f}, ask={ask:.2f}, "
                f"slip={slip_bps:.0f}bps, src={src_tag}, order={order_id})"
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
