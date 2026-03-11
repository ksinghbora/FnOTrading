"""Abstract broker client interface.

All broker implementations (Zerodha, paper trading) implement this interface.
Strategies never interact with a specific broker — they go through this abstraction.
"""

from abc import ABC, abstractmethod
from datetime import date, datetime

from src.core.models import Instrument, Order, Position, Trade
from src.core.types import OrderSide, OrderType, ProductType


class BrokerClient(ABC):
    """Abstract interface for broker operations."""

    # ─── Authentication ──────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection and authenticate with broker."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from broker."""

    @abstractmethod
    def is_connected(self) -> bool:
        """Check if broker connection is active."""

    # ─── Orders ──────────────────────────────────────────────────────

    @abstractmethod
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
        """Place an order. Returns broker order ID."""

    @abstractmethod
    async def modify_order(
        self,
        order_id: str,
        quantity: int | None = None,
        price: float | None = None,
        trigger_price: float | None = None,
        order_type: OrderType | None = None,
    ) -> str:
        """Modify an existing order. Returns broker order ID."""

    @abstractmethod
    async def cancel_order(self, order_id: str) -> str:
        """Cancel an open order. Returns broker order ID."""

    @abstractmethod
    async def get_order_status(self, order_id: str) -> dict:
        """Get current status of an order."""

    @abstractmethod
    async def get_orders(self) -> list[dict]:
        """Get all orders for the day."""

    @abstractmethod
    async def get_trades(self) -> list[dict]:
        """Get all trades for the day."""

    # ─── Positions ───────────────────────────────────────────────────

    @abstractmethod
    async def get_positions(self) -> dict:
        """Get current positions (day and net)."""

    @abstractmethod
    async def get_holdings(self) -> list[dict]:
        """Get equity holdings."""

    # ─── Market Data ─────────────────────────────────────────────────

    @abstractmethod
    async def get_ltp(self, instruments: list[str]) -> dict[str, float]:
        """Get last traded price for instruments.
        instruments: list of 'exchange:tradingsymbol' strings.
        """

    @abstractmethod
    async def get_quote(self, instruments: list[str]) -> dict:
        """Get full quote for instruments."""

    @abstractmethod
    async def get_historical_data(
        self,
        instrument_token: int,
        from_date: datetime,
        to_date: datetime,
        interval: str,
    ) -> list[dict]:
        """Get historical OHLC data."""

    # ─── Instruments ─────────────────────────────────────────────────

    @abstractmethod
    async def get_instruments(self, exchange: str = "") -> list[dict]:
        """Download instrument master data."""

    # ─── Margins ─────────────────────────────────────────────────────

    @abstractmethod
    async def get_margins(self) -> dict:
        """Get account margins."""

    @abstractmethod
    async def get_order_margins(self, orders: list[dict]) -> list[dict]:
        """Get margin requirements for orders before placing."""
