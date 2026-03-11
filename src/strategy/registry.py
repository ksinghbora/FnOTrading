"""Strategy registry — decorator-based registration and factory."""

import logging
from typing import Type

from src.strategy.base import BaseStrategy
from src.strategy.params import BaseStrategyParams

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, Type[BaseStrategy]] = {}
_PARAMS_REGISTRY: dict[str, Type[BaseStrategyParams]] = {}


def register_strategy(
    name: str,
    params_class: Type[BaseStrategyParams] = BaseStrategyParams,
):
    """Decorator to register a strategy class.

    Usage:
        @register_strategy("short_straddle", ShortStraddleParams)
        class ShortStraddleStrategy(BaseStrategy):
            ...
    """

    def decorator(cls: Type[BaseStrategy]) -> Type[BaseStrategy]:
        _REGISTRY[name] = cls
        _PARAMS_REGISTRY[name] = params_class
        logger.debug(f"Registered strategy: {name} -> {cls.__name__}")
        return cls

    return decorator


def create_strategy(
    name: str,
    strategy_id: str,
    params: dict | None = None,
) -> BaseStrategy:
    """Create a strategy instance from the registry.

    Args:
        name: Registered strategy name (e.g., 'short_straddle').
        strategy_id: Unique instance ID.
        params: Strategy parameters dict.

    Returns:
        Instantiated strategy.
    """
    if name not in _REGISTRY:
        from src.core.exceptions import StrategyNotFoundError
        raise StrategyNotFoundError(f"Strategy '{name}' not found. Available: {list_strategies()}")

    strategy_cls = _REGISTRY[name]
    params_cls = _PARAMS_REGISTRY[name]

    parsed_params = params_cls(**(params or {}))
    return strategy_cls(strategy_id=strategy_id, params=parsed_params)


def list_strategies() -> list[str]:
    """List all registered strategy names."""
    return list(_REGISTRY.keys())


def get_params_schema(name: str) -> dict:
    """Get the JSON schema for a strategy's parameters."""
    if name not in _PARAMS_REGISTRY:
        return {}
    return _PARAMS_REGISTRY[name].model_json_schema()
