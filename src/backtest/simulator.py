"""Fill simulation for backtesting — models slippage and partial fills."""

import random
from decimal import Decimal

from src.core.types import OrderSide, OrderType


class FillSimulator:
    """Simulates order fills with configurable slippage models."""

    def __init__(
        self,
        slippage_model: str = "fixed",  # 'fixed', 'proportional', 'volatility'
        fixed_slippage_pct: float = 0.05,
        spread_ticks: int = 1,
        tick_size: float = 0.05,
    ):
        self.slippage_model = slippage_model
        self.fixed_slippage_pct = fixed_slippage_pct
        self.spread_ticks = spread_ticks
        self.tick_size = tick_size

    def simulate_fill(
        self,
        price: float,
        side: OrderSide,
        order_type: OrderType = OrderType.MARKET,
        volatility: float = 0.0,
    ) -> float:
        """Simulate a fill price with slippage.

        Args:
            price: Theoretical price (LTP or limit price).
            side: BUY or SELL.
            order_type: MARKET or LIMIT.
            volatility: Current IV (for volatility-based slippage).

        Returns:
            Simulated fill price.
        """
        if order_type == OrderType.LIMIT:
            return price  # Limit orders fill at their price

        slippage = self._calculate_slippage(price, volatility)

        if side == OrderSide.BUY:
            return price + slippage  # Buy higher
        else:
            return price - slippage  # Sell lower

    def _calculate_slippage(self, price: float, volatility: float) -> float:
        if self.slippage_model == "fixed":
            return price * self.fixed_slippage_pct / 100

        elif self.slippage_model == "proportional":
            return self.spread_ticks * self.tick_size

        elif self.slippage_model == "volatility":
            base = price * self.fixed_slippage_pct / 100
            vol_factor = 1 + (volatility * 2)  # Higher IV = more slippage
            return base * vol_factor

        return 0
