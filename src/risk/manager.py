"""Central Risk Manager — gate through which every order must pass."""

import logging
from decimal import Decimal

from src.core.constants import LOT_SIZES
from src.core.events import EventBus
from src.core.exceptions import CircuitBreakerActiveError, KillSwitchActiveError, RiskLimitBreachError
from src.core.models import OrderRequest
from src.core.structured_logger import get_structured_logger
from src.core.types import OrderSide
from src.portfolio.manager import PortfolioManager
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.greeks_risk import GreeksRiskMonitor
from src.risk.kill_switch import KillSwitch
from src.risk.limits import RiskLimits
from src.risk.portfolio_budget import PortfolioGammaBudget

logger = logging.getLogger(__name__)


class RiskManager:
    """Central risk gate — every order request passes through here.

    Aggregates checks from:
    - RiskLimits (position/loss limits)
    - CircuitBreaker (system-level halt)
    - KillSwitch (emergency stop)
    - GreeksRiskMonitor (portfolio Greeks)
    - PortfolioGammaBudget (aggregate gamma × 1%-spot PnL)
    """

    def __init__(
        self,
        limits: RiskLimits,
        circuit_breaker: CircuitBreaker,
        kill_switch: KillSwitch,
        greeks_monitor: GreeksRiskMonitor,
        portfolio: PortfolioManager,
        event_bus: EventBus,
        gamma_budget: PortfolioGammaBudget | None = None,
        chain_builder=None,
    ):
        self._limits = limits
        self._circuit_breaker = circuit_breaker
        self._kill_switch = kill_switch
        self._greeks_monitor = greeks_monitor
        self._portfolio = portfolio
        self._event_bus = event_bus
        self._order_manager = None  # Injected after creation
        self._gamma_budget = gamma_budget
        self._chain_builder = chain_builder

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

        # Check portfolio gamma × 1%-spot budget
        # (Skips on exits so we can always flatten convexity.)
        if self._gamma_budget is not None and not risk_reducing:
            exposure = self._gamma_budget.current_exposure(
                open_positions, self._chain_builder,
            )
            additional = self._gamma_budget.estimate_incremental_dollar_gamma(
                order, self._chain_builder,
            )
            ok, reason = self._gamma_budget.can_open(additional, exposure)
            if not ok:
                logger.warning(
                    f"[GAMMA_BUDGET_BREACH] strategy={order.strategy_id} "
                    f"symbol={order.tradingsymbol} qty={order.quantity} "
                    f"{reason}"
                )
                raise RiskLimitBreachError(f"Gamma budget breach: {reason}")

        # Update circuit breaker with current P&L
        self._circuit_breaker.check_pnl(day_pnl)

        # Log risk proximity after every successful validation
        self._log_risk_proximity(day_pnl, strategy_pnl, total_lots, open_order_count, order.strategy_id)

        if risk_reducing:
            logger.info(
                f"Risk-reducing order allowed: {order.order_side.value} "
                f"{order.quantity} {order.tradingsymbol} [{order.strategy_id}]"
            )

    def _log_risk_proximity(
        self,
        day_pnl: Decimal,
        strategy_pnl: Decimal,
        total_lots: int,
        open_orders: int,
        strategy_id: str = "",
    ) -> None:
        """Log how close we are to each risk limit."""
        limits = self._limits
        day_pct = abs(float(day_pnl) / float(limits.max_day_loss) * 100) if limits.max_day_loss else 0
        strat_pct = abs(float(strategy_pnl) / float(limits.max_strategy_loss) * 100) if limits.max_strategy_loss else 0
        lots_pct = total_lots / limits.max_total_lots * 100 if limits.max_total_lots else 0
        orders_pct = open_orders / limits.max_open_orders * 100 if limits.max_open_orders else 0

        level = "WARNING" if max(day_pct, strat_pct, lots_pct, orders_pct) > 70 else "INFO"

        msg = (
            f"[RISK_PROXIMITY] day_pnl={float(day_pnl):+,.0f}/{float(-limits.max_day_loss):,.0f} ({day_pct:.0f}%) "
            f"strategy_pnl={float(strategy_pnl):+,.0f}/{float(-limits.max_strategy_loss):,.0f} ({strat_pct:.0f}%) "
            f"lots={total_lots}/{limits.max_total_lots} ({lots_pct:.0f}%) "
            f"orders={open_orders}/{limits.max_open_orders} ({orders_pct:.0f}%)"
        )

        if level == "WARNING":
            logger.warning(msg)
        else:
            logger.info(msg)

        slog = get_structured_logger()
        slog.log(
            "RISK_PROXIMITY",
            strategy_id=strategy_id,
            day_pnl=float(day_pnl),
            day_pnl_limit=float(-limits.max_day_loss),
            day_pnl_pct=round(day_pct, 1),
            strategy_pnl=float(strategy_pnl),
            strategy_pnl_limit=float(-limits.max_strategy_loss),
            strategy_pnl_pct=round(strat_pct, 1),
            total_lots=total_lots,
            max_lots=limits.max_total_lots,
            lots_pct=round(lots_pct, 1),
            open_orders=open_orders,
            max_orders=limits.max_open_orders,
            orders_pct=round(orders_pct, 1),
        )

    def get_risk_snapshot(self) -> dict:
        """Get current risk limit utilization for API/dashboard."""
        day_pnl = self._portfolio.get_day_pnl()
        positions = self._portfolio.get_open_positions()
        total_lots = self._count_total_lots(positions)
        open_orders = len(self._order_manager.get_open_orders()) if self._order_manager else 0
        limits = self._limits

        return {
            "day_pnl": float(day_pnl),
            "day_pnl_limit": float(-limits.max_day_loss),
            "day_pnl_pct": round(abs(float(day_pnl) / float(limits.max_day_loss) * 100) if limits.max_day_loss else 0, 1),
            "total_lots": total_lots,
            "max_lots": limits.max_total_lots,
            "lots_pct": round(total_lots / limits.max_total_lots * 100 if limits.max_total_lots else 0, 1),
            "open_orders": open_orders,
            "max_orders": limits.max_open_orders,
            "circuit_breaker": self._circuit_breaker.state.value,
            "kill_switch": self._kill_switch.is_activated,
        }

    def update_greeks(self) -> None:
        """Recalculate portfolio Greeks from current positions."""
        positions = self._portfolio.get_open_positions()
        self._greeks_monitor.update(positions)

    def monitor_gamma_budget(self) -> dict:
        """Periodic monitor hook — returns snapshot and triggers emergency.

        Call every ~60s from the operational summary loop.
        Returns a dict useful for logging / API; additionally logs a
        structured `[GAMMA_BUDGET]` line. When utilization blows through
        the config's emergency threshold, schedules a kill-switch flatten
        of the largest-gamma position.
        """
        if self._gamma_budget is None:
            return {}

        positions = self._portfolio.get_open_positions()
        exposure = self._gamma_budget.current_exposure(
            positions, self._chain_builder,
        )

        emergency = exposure.utilization >= self._gamma_budget.config.emergency_utilization
        level = "CRITICAL" if emergency else (
            "WARNING" if exposure.utilization > 0.8 else "INFO"
        )
        msg = (
            f"[GAMMA_BUDGET] util={exposure.utilization:.2f} "
            f"dollar_gamma_1pct={exposure.dollar_gamma_1pct:,.0f} "
            f"budget={exposure.budget:,.0f} "
            f"total_gamma={exposure.total_gamma:+.4f} "
            f"spot={exposure.spot:.2f} "
            f"positions={len(exposure.per_position)}"
        )
        if level == "CRITICAL":
            logger.critical(msg)
        elif level == "WARNING":
            logger.warning(msg)
        else:
            logger.info(msg)

        snapshot = {
            "total_gamma": exposure.total_gamma,
            "dollar_gamma_1pct": exposure.dollar_gamma_1pct,
            "budget": exposure.budget,
            "utilization": exposure.utilization,
            "spot": exposure.spot,
            "breaches": list(exposure.breaches),
            "emergency_triggered": False,
        }

        if emergency:
            target = self._gamma_budget.largest_gamma_position(
                positions, self._chain_builder,
            )
            if target is not None:
                snapshot["emergency_triggered"] = True
                snapshot["flatten_symbol"] = target.tradingsymbol
                logger.critical(
                    f"[GAMMA_BUDGET] emergency flatten scheduled: "
                    f"symbol={target.tradingsymbol} qty={target.quantity} "
                    f"util={exposure.utilization:.2f}"
                )
                import asyncio
                try:
                    asyncio.get_running_loop()
                    asyncio.create_task(
                        self._kill_switch.activate(
                            reason=(
                                f"Gamma budget breach util={exposure.utilization:.2f} "
                                f"(target={target.tradingsymbol})"
                            )
                        )
                    )
                except RuntimeError:
                    # No running loop — caller is in a sync context (tests).
                    pass
        return snapshot

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._circuit_breaker

    @property
    def kill_switch(self) -> KillSwitch:
        return self._kill_switch

    @property
    def limits(self) -> RiskLimits:
        return self._limits

    @property
    def gamma_budget(self) -> PortfolioGammaBudget | None:
        return self._gamma_budget
