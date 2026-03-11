"""Zerodha Kite Connect broker client implementation."""

import asyncio
import functools
import logging
from datetime import datetime

from kiteconnect import KiteConnect

from src.broker.base import BrokerClient
from src.core.exceptions import (
    BrokerAuthError,
    BrokerConnectionError,
    BrokerOrderError,
    BrokerRateLimitError,
)
from src.core.types import OrderSide, OrderType, ProductType

logger = logging.getLogger(__name__)

# Timeout for individual Kite API calls (seconds)
API_TIMEOUT = 10


class ZerodhaClient(BrokerClient):
    """Kite Connect API wrapper implementing BrokerClient interface."""

    def __init__(self, api_key: str, access_token: str = ""):
        self._api_key = api_key
        self._access_token = access_token
        self._kite: KiteConnect | None = None
        self._connected = False

    async def _run_sync(self, func, *args, **kwargs):
        """Run a synchronous Kite API call in a thread with timeout."""
        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, functools.partial(func, *args, **kwargs)),
                timeout=API_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise BrokerConnectionError(
                f"Kite API call {func.__name__} timed out after {API_TIMEOUT}s"
            )
        except Exception as e:
            error_msg = str(e)
            # Detect token expiry
            if "TokenException" in type(e).__name__ or "Invalid access token" in error_msg:
                self._connected = False
                raise BrokerAuthError(
                    f"Access token expired or invalid: {error_msg}. "
                    f"Re-login required."
                ) from e
            raise

    @property
    def kite(self) -> KiteConnect:
        if self._kite is None:
            raise BrokerConnectionError("Not connected to Zerodha. Call connect() first.")
        return self._kite

    async def connect(self) -> None:
        try:
            self._kite = KiteConnect(api_key=self._api_key)
            if self._access_token:
                self._kite.set_access_token(self._access_token)
            # Verify connection by fetching profile (async to avoid blocking event loop)
            await self._run_sync(self._kite.profile)
            self._connected = True
            logger.info("Connected to Zerodha Kite")
        except (BrokerAuthError, BrokerConnectionError):
            self._connected = False
            raise
        except Exception as e:
            self._connected = False
            raise BrokerAuthError(f"Failed to connect to Zerodha: {e}") from e

    async def disconnect(self) -> None:
        if self._kite:
            try:
                await self._run_sync(self._kite.invalidate_access_token, self._access_token)
            except Exception:
                pass  # Best-effort token invalidation
        self._connected = False
        self._kite = None
        logger.info("Disconnected from Zerodha")

    def is_connected(self) -> bool:
        return self._connected

    def set_access_token(self, access_token: str) -> None:
        self._access_token = access_token
        if self._kite:
            self._kite.set_access_token(access_token)

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
        try:
            params: dict = {
                "tradingsymbol": tradingsymbol,
                "exchange": exchange,
                "transaction_type": side.value,
                "quantity": quantity,
                "order_type": order_type.value,
                "product": product.value,
                "variety": "regular",
            }
            if price > 0:
                params["price"] = price
            if trigger_price > 0:
                params["trigger_price"] = trigger_price
            if tag:
                params["tag"] = tag[:20]  # Kite limits tag to 20 chars

            order_id = await self._run_sync(self.kite.place_order, **params)
            logger.info(f"Order placed: {order_id} | {side.value} {quantity} {tradingsymbol}")
            return str(order_id)
        except (BrokerAuthError, BrokerConnectionError):
            raise
        except Exception as e:
            error_msg = str(e)
            if "Too many requests" in error_msg:
                raise BrokerRateLimitError(error_msg) from e
            raise BrokerOrderError(f"Order placement failed: {error_msg}") from e

    async def modify_order(
        self,
        order_id: str,
        quantity: int | None = None,
        price: float | None = None,
        trigger_price: float | None = None,
        order_type: OrderType | None = None,
    ) -> str:
        try:
            params: dict = {"order_id": order_id, "variety": "regular"}
            if quantity is not None:
                params["quantity"] = quantity
            if price is not None:
                params["price"] = price
            if trigger_price is not None:
                params["trigger_price"] = trigger_price
            if order_type is not None:
                params["order_type"] = order_type.value

            result = await self._run_sync(self.kite.modify_order, **params)
            logger.info(f"Order modified: {order_id}")
            return str(result)
        except (BrokerAuthError, BrokerConnectionError):
            raise
        except Exception as e:
            raise BrokerOrderError(f"Order modification failed: {e}") from e

    async def cancel_order(self, order_id: str) -> str:
        try:
            result = await self._run_sync(self.kite.cancel_order, order_id=order_id, variety="regular")
            logger.info(f"Order cancelled: {order_id}")
            return str(result)
        except (BrokerAuthError, BrokerConnectionError):
            raise
        except Exception as e:
            raise BrokerOrderError(f"Order cancellation failed: {e}") from e

    async def get_order_status(self, order_id: str) -> dict:
        try:
            order_history = await self._run_sync(self.kite.order_history, order_id=order_id)
            return order_history[-1] if order_history else {}
        except (BrokerAuthError, BrokerConnectionError):
            raise
        except Exception as e:
            raise BrokerOrderError(f"Failed to get order status: {e}") from e

    async def get_orders(self) -> list[dict]:
        try:
            return await self._run_sync(self.kite.orders) or []
        except (BrokerAuthError, BrokerConnectionError):
            raise
        except Exception as e:
            raise BrokerOrderError(f"Failed to get orders: {e}") from e

    async def get_trades(self) -> list[dict]:
        try:
            return await self._run_sync(self.kite.trades) or []
        except (BrokerAuthError, BrokerConnectionError):
            raise
        except Exception as e:
            raise BrokerOrderError(f"Failed to get trades: {e}") from e

    async def get_positions(self) -> dict:
        try:
            return await self._run_sync(self.kite.positions)
        except (BrokerAuthError, BrokerConnectionError):
            raise
        except Exception as e:
            raise BrokerConnectionError(f"Failed to get positions: {e}") from e

    async def get_holdings(self) -> list[dict]:
        try:
            return await self._run_sync(self.kite.holdings) or []
        except (BrokerAuthError, BrokerConnectionError):
            raise
        except Exception as e:
            raise BrokerConnectionError(f"Failed to get holdings: {e}") from e

    async def get_ltp(self, instruments: list[str]) -> dict[str, float]:
        try:
            data = await self._run_sync(self.kite.ltp, instruments)
            return {k: v["last_price"] for k, v in data.items()}
        except (BrokerAuthError, BrokerConnectionError):
            raise
        except Exception as e:
            raise BrokerConnectionError(f"Failed to get LTP: {e}") from e

    async def get_quote(self, instruments: list[str]) -> dict:
        try:
            return await self._run_sync(self.kite.quote, instruments)
        except (BrokerAuthError, BrokerConnectionError):
            raise
        except Exception as e:
            raise BrokerConnectionError(f"Failed to get quote: {e}") from e

    async def get_historical_data(
        self,
        instrument_token: int,
        from_date: datetime,
        to_date: datetime,
        interval: str,
    ) -> list[dict]:
        try:
            return await self._run_sync(
                self.kite.historical_data,
                instrument_token=instrument_token,
                from_date=from_date,
                to_date=to_date,
                interval=interval,
                oi=True,
            )
        except (BrokerAuthError, BrokerConnectionError):
            raise
        except Exception as e:
            raise BrokerConnectionError(f"Failed to get historical data: {e}") from e

    async def get_instruments(self, exchange: str = "") -> list[dict]:
        try:
            if exchange:
                return await self._run_sync(self.kite.instruments, exchange=exchange)
            return await self._run_sync(self.kite.instruments)
        except (BrokerAuthError, BrokerConnectionError):
            raise
        except Exception as e:
            raise BrokerConnectionError(f"Failed to get instruments: {e}") from e

    async def get_margins(self) -> dict:
        try:
            return await self._run_sync(self.kite.margins)
        except (BrokerAuthError, BrokerConnectionError):
            raise
        except Exception as e:
            raise BrokerConnectionError(f"Failed to get margins: {e}") from e

    async def get_order_margins(self, orders: list[dict]) -> list[dict]:
        try:
            return await self._run_sync(self.kite.order_margins, orders)
        except (BrokerAuthError, BrokerConnectionError):
            raise
        except Exception as e:
            raise BrokerConnectionError(f"Failed to get order margins: {e}") from e
