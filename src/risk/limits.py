"""Risk limits — position and loss limits enforcement."""

import logging
from decimal import Decimal

from src.core.constants import LOT_SIZES
from src.core.exceptions import RiskLimitBreachError
from src.core.models import OrderRequest

logger = logging.getLogger(__name__)


class RiskLimits:
    """Enforces position limits, loss limits, and exposure limits."""

    def __init__(
        self,
        max_day_loss: Decimal = Decimal("15000"),
        max_strategy_loss: Decimal = Decimal("5000"),
        max_total_lots: int = 50,
        max_open_orders: int = 20,
    ):
        self.max_day_loss = max_day_loss
        self.max_strategy_loss = max_strategy_loss
        self.max_total_lots = max_total_lots
        self.max_open_orders = max_open_orders

    def check_day_loss(self, current_day_pnl: Decimal) -> None:
        """Check if day loss limit has been breached."""
        if current_day_pnl < -self.max_day_loss:
            raise RiskLimitBreachError(
                f"Day loss limit breached: PnL={current_day_pnl} "
                f"exceeds limit={-self.max_day_loss}"
            )

    def check_strategy_loss(self, strategy_id: str, strategy_pnl: Decimal) -> None:
        """Check if per-strategy loss limit has been breached."""
        if strategy_pnl < -self.max_strategy_loss:
            raise RiskLimitBreachError(
                f"Strategy {strategy_id} loss limit breached: PnL={strategy_pnl} "
                f"exceeds limit={-self.max_strategy_loss}"
            )

    def check_position_limit(self, total_lots: int) -> None:
        """Check if total lot limit has been breached."""
        if total_lots > self.max_total_lots:
            raise RiskLimitBreachError(
                f"Total lots limit breached: {total_lots} > {self.max_total_lots}"
            )

    def check_open_orders(self, open_order_count: int) -> None:
        """Check if max open orders limit has been reached."""
        if open_order_count >= self.max_open_orders:
            raise RiskLimitBreachError(
                f"Max open orders reached: {open_order_count} >= {self.max_open_orders}"
            )

    def _get_lot_size(self, tradingsymbol: str) -> int:
        """Get lot size for a tradingsymbol, matching longest underlying first."""
        for underlying, ls in sorted(LOT_SIZES.items(), key=lambda x: -len(x[0])):
            if tradingsymbol.startswith(underlying):
                return ls
        return 1

    def validate_order(
        self,
        order: OrderRequest,
        current_day_pnl: Decimal,
        strategy_pnl: Decimal,
        total_lots: int,
        open_order_count: int,
        is_risk_reducing: bool = False,
    ) -> None:
        """Run all limit checks for a new order.

        Risk-reducing orders (exits/closes) bypass loss limits and position
        limits — you must always be able to close a losing position.
        """
        if not is_risk_reducing:
            # Only block new/risk-increasing orders on loss limits
            self.check_day_loss(current_day_pnl)
            self.check_strategy_loss(order.strategy_id, strategy_pnl)

            # Position limit only applies to risk-increasing orders
            lot_size = self._get_lot_size(order.tradingsymbol)
            new_lots = total_lots + (order.quantity // lot_size)
            self.check_position_limit(new_lots)

        self.check_open_orders(open_order_count)
