"""Central Risk Manager — gate through which every order must pass."""

import logging
from decimal import Decimal

from src.core.constants import LOT_SIZES
from src.core.events import EventBus
from src.core.exceptions import CircuitBreakerActiveError, KillSwitchActiveError, RiskLimitBreachError
from src.core.models import OrderRequest
from src.core.types import OrderSide
from src.portfolio.manager import PortfolioManager
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.greeks_risk import GreeksRiskMonitor
from src.risk.kill_switch import KillSwitch
from src.risk.limits import RiskLimits

logger = logging.getLogger(__name__)


class RiskManager:
    """Central risk gate — every order request passes through here.

    Aggregates checks from:
    - RiskLimits (position/loss limits)
    - CircuitBreaker (system-level halt)
    - KillSwitch (emergency stop)
    - GreeksRiskMonitor (portfolio Greeks)
    """

    def __init__(
        self,
        limits: RiskLimits,
        circuit_breaker: CircuitBreaker,
        kill_switch: KillSwitch,
        greeks_monitor: GreeksRiskMonitor,
        portfolio: PortfolioManager,
        event_bus: EventBus,
    ):
        self._limits = limits
        self._circuit_breaker = circuit_breaker
        self._kill_switch = kill_switch
        self._greeks_monitor = greeks_monitor
        self._portfolio = portfolio
        self._event_bus = event_bus
        self._order_manager = None  # Injected after creation

    def set_order_manager(self, order_manager) -> None:
        """Inject order manager for open order count checks."""
        self._order_manager = order_manager

    @staticmethod
    def _get_lot_size(tradingsymbol: str) -> int:
        """Get lot size for a tradingsymbol, matching longest underlying first."""
        for underlying, ls in sorted(LOT_SIZES.items(), key=lambda x: -len(x[0])):
            if tradingsymbol.startswith(underlying):
                return ls
        return 1

    @staticmethod
    def _count_total_lots(positions) -> int:
        """Convert position quantities to lots and sum."""
        total = 0
        for pos in positions:
            ls = RiskManager._get_lot_size(pos.tradingsymbol)
            total += abs(pos.quantity) // ls
        return total

    def _is_risk_reducing(self, order: OrderRequest) -> bool:
        """Check if order reduces an existing position (exit/close).

        A BUY closing a short position or a SELL closing a long position
        is risk-reducing only if the order quantity does not exceed the
        position size (prevents position reversal via "exit" orders).
        """
        positions = self._portfolio.get_open_positions(order.strategy_id)
        for pos in positions:
            if pos.instrument_token == order.instrument_token:
                if order.order_side == OrderSide.BUY and pos.quantity < 0:
                    return order.quantity <= abs(pos.quantity)
                if order.order_side == OrderSide.SELL and pos.quantity > 0:
                    return order.quantity <= pos.quantity
        return False

    def validate_order(self, order: OrderRequest) -> None:
        """Validate an order against all risk checks.

        Risk-reducing orders (exits/closes) are always allowed through
        loss limit checks — you must be able to close losing positions.

        Raises:
            KillSwitchActiveError: Kill switch is active (blocks even exits).
            CircuitBreakerActiveError: Circuit breaker is tripped.
            RiskLimitBreachError: A risk limit is breached.
        """
        # Kill switch blocks EVERYTHING (emergency only)
        if self._kill_switch.is_activated:
            raise KillSwitchActiveError("Kill switch is active. All trading halted.")

        # Check circuit breaker — but allow risk-reducing orders through
        risk_reducing = self._is_risk_reducing(order)
        if self._circuit_breaker.is_active and not risk_reducing:
            raise CircuitBreakerActiveError(
                f"Circuit breaker is {self._circuit_breaker.state.value}. "
                f"Reason: {self._circuit_breaker.trigger_reason}"
            )

        # Get current state
        day_pnl = self._portfolio.get_day_pnl()
        strategy_pnl = self._portfolio.get_pnl(order.strategy_id).net
        open_positions = self._portfolio.get_open_positions()
        total_lots = self._count_total_lots(open_positions)

        open_order_count = (
            len(self._order_manager.get_open_orders()) if self._order_manager else 0
        )

        # Check limits — pass risk_reducing flag so exits bypass loss limits
        self._limits.validate_order(
            order=order,
            current_day_pnl=day_pnl,
            strategy_pnl=strategy_pnl,
            total_lots=total_lots,
            open_order_count=open_order_count,
            is_risk_reducing=risk_reducing,
        )

        # Check Greeks risk — block position-increasing orders on breach
        breaches = self._greeks_monitor.check_limits()
        if breaches and not risk_reducing:
            raise RiskLimitBreachError(
                f"Greeks limits breached: {'; '.join(breaches)}"
            )

        # Update circuit breaker with current P&L
        self._circuit_breaker.check_pnl(day_pnl)

        if risk_reducing:
            logger.info(
                f"Risk-reducing order allowed: {order.order_side.value} "
                f"{order.quantity} {order.tradingsymbol} [{order.strategy_id}]"
            )

    def update_greeks(self) -> None:
        """Recalculate portfolio Greeks from current positions."""
        positions = self._portfolio.get_open_positions()
        self._greeks_monitor.update(positions)

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._circuit_breaker

    @property
    def kill_switch(self) -> KillSwitch:
        return self._kill_switch

    @property
    def limits(self) -> RiskLimits:
        return self._limits
