"""Core enumerations and type definitions for the F&O Trading System."""

from enum import Enum


class Exchange(str, Enum):
    NSE = "NSE"
    BSE = "BSE"
    NFO = "NFO"  # NSE F&O
    BFO = "BFO"  # BSE F&O
    CDS = "CDS"  # Currency derivatives


class Segment(str, Enum):
    EQUITY = "EQUITY"
    FUTURES = "FUTURES"
    OPTIONS = "OPTIONS"


class InstrumentType(str, Enum):
    EQ = "EQ"
    FUT = "FUT"
    CE = "CE"
    PE = "PE"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"          # Stop Loss Limit
    SL_M = "SL-M"      # Stop Loss Market


class ProductType(str, Enum):
    MIS = "MIS"    # Intraday
    NRML = "NRML"  # Carry forward (for F&O)
    CNC = "CNC"    # Cash and carry (equity delivery)


class OrderStatus(str, Enum):
    NEW = "NEW"
    VALIDATING = "VALIDATING"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class StrategyState(str, Enum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class SignalType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ADJUST = "ADJUST"


class CircuitBreakerState(str, Enum):
    CLOSED = "CLOSED"        # Normal operation
    OPEN = "OPEN"            # Trading halted
    HALF_OPEN = "HALF_OPEN"  # Limited trading


class Timeframe(str, Enum):
    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    D1 = "1d"


class OptionType(str, Enum):
    CE = "CE"  # Call
    PE = "PE"  # Put


class Underlying(str, Enum):
    NIFTY = "NIFTY"
    BANKNIFTY = "BANKNIFTY"
    FINNIFTY = "FINNIFTY"
