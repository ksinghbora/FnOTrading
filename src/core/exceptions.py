"""Custom exception hierarchy for the F&O Trading System."""


class FnOTradingError(Exception):
    """Base exception for all trading system errors."""


# Broker errors
class BrokerError(FnOTradingError):
    """Base exception for broker-related errors."""


class BrokerConnectionError(BrokerError):
    """Failed to connect to broker API."""


class BrokerAuthError(BrokerError):
    """Authentication failed with broker."""


class BrokerOrderError(BrokerError):
    """Error placing/modifying/cancelling an order."""


class BrokerRateLimitError(BrokerError):
    """Broker API rate limit exceeded."""


# Order errors
class OrderError(FnOTradingError):
    """Base exception for order-related errors."""


class OrderValidationError(OrderError):
    """Order failed pre-flight validation."""


class DuplicateOrderError(OrderError):
    """Duplicate order detected within dedup window."""


class InsufficientMarginError(OrderError):
    """Insufficient margin for the order."""


# Risk errors
class RiskError(FnOTradingError):
    """Base exception for risk-related errors."""


class RiskLimitBreachError(RiskError):
    """A risk limit has been breached."""


class CircuitBreakerActiveError(RiskError):
    """Circuit breaker is active, trading halted."""


class KillSwitchActiveError(RiskError):
    """Kill switch has been activated."""


# Market data errors
class MarketDataError(FnOTradingError):
    """Base exception for market data errors."""


class TickerConnectionError(MarketDataError):
    """WebSocket ticker connection failed."""


class InstrumentNotFoundError(MarketDataError):
    """Instrument not found in master data."""


# Strategy errors
class StrategyError(FnOTradingError):
    """Base exception for strategy-related errors."""


class StrategyNotFoundError(StrategyError):
    """Strategy not found in registry."""


class StrategyConfigError(StrategyError):
    """Invalid strategy configuration."""


# Data errors
class DataError(FnOTradingError):
    """Base exception for data-related errors."""


class ReconciliationError(DataError):
    """Position reconciliation mismatch detected."""
