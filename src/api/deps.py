"""Dependency injection container for FastAPI routes.

Holds references to core system components so routes can access them
through FastAPI's Depends() mechanism.
"""

from typing import Any

from src.config import Settings
from src.core.events import EventBus
from src.oms.manager import OrderManager
from src.portfolio.manager import PortfolioManager
from src.risk.manager import RiskManager
from src.strategy.runner import StrategyRunner


class Container:
    """Simple dependency container holding references to system components.

    Set during application startup; accessed by routes via get_container().
    """

    def __init__(self) -> None:
        self.settings: Settings | None = None
        self.event_bus: EventBus | None = None
        self.order_manager: OrderManager | None = None
        self.portfolio_manager: PortfolioManager | None = None
        self.risk_manager: RiskManager | None = None
        self.strategy_runner: StrategyRunner | None = None


# Module-level singleton
_container = Container()


def get_container() -> Container:
    """Return the global dependency container."""
    return _container


def init_container(
    settings: Settings,
    event_bus: EventBus,
    order_manager: OrderManager,
    portfolio_manager: PortfolioManager,
    risk_manager: RiskManager,
    strategy_runner: StrategyRunner,
) -> Container:
    """Initialize the dependency container with system components.

    Called once during application startup.
    """
    _container.settings = settings
    _container.event_bus = event_bus
    _container.order_manager = order_manager
    _container.portfolio_manager = portfolio_manager
    _container.risk_manager = risk_manager
    _container.strategy_runner = strategy_runner
    return _container


def _require(component, name: str):
    """Raise RuntimeError if component is None. Safe under python -O."""
    if component is None:
        raise RuntimeError(f"{name} not initialized. Call init_container() first.")
    return component


def get_settings() -> Settings:
    return _require(_container.settings, "Settings")


def get_event_bus() -> EventBus:
    return _require(_container.event_bus, "EventBus")


def get_order_manager() -> OrderManager:
    return _require(_container.order_manager, "OrderManager")


def get_portfolio_manager() -> PortfolioManager:
    return _require(_container.portfolio_manager, "PortfolioManager")


def get_risk_manager() -> RiskManager:
    return _require(_container.risk_manager, "RiskManager")


def get_strategy_runner() -> StrategyRunner:
    return _require(_container.strategy_runner, "StrategyRunner")
