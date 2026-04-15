"""Paper trading broker — simulates order execution without real money.

Uses the same BrokerClient interface, so strategies run identically
in paper mode vs live mode with zero code changes.
"""

import logging
import uuid
from datetime import datetime
from decimal import Decimal

from src.broker.base import BrokerClient
from src.core.types import OrderSide, OrderType, ProductType

logger = logging.getLogger(__name__)


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

    def __init__(self, initial_capital: float = 1_000_000):
        self._connected = False
        self._capital = initial_capital
        self._available_margin = initial_capital
        self._orders: list[dict] = []
        self._trades: list[dict] = []
        self._positions: dict[str, PaperPosition] = {}
        self._ltp_cache: dict[str, float] = {}

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
        fill_price = price if price > 0 else self._ltp_cache.get(tradingsymbol, 0)

        if fill_price <= 0:
            logger.warning(f"[PAPER] No LTP for {tradingsymbol}, cannot fill order")
            raise ValueError(f"No LTP available for {tradingsymbol}")

        # No artificial slippage — fill at exact LTP
        # Strategy decisions and fills use the same price source (chain builder via tick feed)

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
            "order_timestamp": datetime.now().isoformat(),
            "exchange_timestamp": datetime.now().isoformat(),
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
            "fill_timestamp": datetime.now().isoformat(),
        })
        if len(self._trades) > self.MAX_TRADE_HISTORY:
            self._trades = self._trades[-self.MAX_TRADE_HISTORY:]

        # Update position
        self._update_position(tradingsymbol, exchange, side, quantity, fill_price)

        logger.info(
            f"[PAPER] {side.value} {quantity} {tradingsymbol} @ {fill_price:.2f} "
            f"(order: {order_id})"
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
