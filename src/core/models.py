"""Core Pydantic models shared across the system."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from src.core.types import (
    InstrumentType,
    OptionType,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    SignalType,
    Timeframe,
)


# ─── Market Data Models ─────────────────────────────────────────────


class Tick(BaseModel):
    """Real-time tick data from exchange."""

    instrument_token: int
    tradingsymbol: str
    timestamp: datetime
    ltp: Decimal
    volume: int = 0
    oi: int = 0
    bid_price: Decimal = Decimal("0")
    ask_price: Decimal = Decimal("0")
    bid_qty: int = 0
    ask_qty: int = 0
    high: Decimal = Decimal("0")
    low: Decimal = Decimal("0")
    open: Decimal = Decimal("0")
    close: Decimal = Decimal("0")


class OHLC(BaseModel):
    """OHLC candle data."""

    instrument_token: int
    tradingsymbol: str
    timestamp: datetime
    timeframe: Timeframe
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = 0
    oi: int = 0


class Instrument(BaseModel):
    """Instrument master data."""

    instrument_token: int
    exchange: str
    tradingsymbol: str
    name: str = ""
    segment: str
    instrument_type: InstrumentType
    strike: Decimal = Decimal("0")
    expiry: date | None = None
    lot_size: int = 1
    tick_size: Decimal = Decimal("0.05")
    underlying: str = ""


# ─── Greeks Models ───────────────────────────────────────────────────


class Greeks(BaseModel):
    """Option Greeks."""

    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    rho: float = 0.0
    iv: float = 0.0


class OptionData(BaseModel):
    """Single option strike data with Greeks."""

    tradingsymbol: str
    instrument_token: int
    strike: Decimal
    option_type: OptionType
    expiry: date
    ltp: Decimal = Decimal("0")
    bid_price: Decimal = Decimal("0")
    ask_price: Decimal = Decimal("0")
    volume: int = 0
    oi: int = 0
    greeks: Greeks = Field(default_factory=Greeks)


class OptionChainEntry(BaseModel):
    """A single strike in the option chain with CE and PE data."""

    strike: Decimal
    ce: OptionData | None = None
    pe: OptionData | None = None


class OptionChain(BaseModel):
    """Full option chain for an underlying + expiry."""

    underlying: str
    expiry: date
    spot_price: Decimal
    atm_strike: Decimal
    strikes: list[OptionChainEntry] = Field(default_factory=list)
    pcr_oi: float = 0.0
    pcr_volume: float = 0.0
    max_pain: Decimal = Decimal("0")
    total_ce_oi: int = 0
    total_pe_oi: int = 0
    updated_at: datetime | None = None


# ─── Order & Trade Models ───────────────────────────────────────────


class OrderRequest(BaseModel):
    """Request to place an order."""

    strategy_id: str
    instrument_token: int
    tradingsymbol: str
    order_side: OrderSide
    order_type: OrderType = OrderType.MARKET
    product: ProductType = ProductType.NRML
    quantity: int
    price: Decimal = Decimal("0")
    trigger_price: Decimal = Decimal("0")
    tag: str = ""
    group_id: str | None = None  # For multi-leg orders


class Order(BaseModel):
    """Order with full lifecycle state."""

    id: UUID = Field(default_factory=uuid4)
    broker_order_id: str = ""
    strategy_id: str
    instrument_token: int
    tradingsymbol: str
    order_side: OrderSide
    order_type: OrderType
    product: ProductType
    quantity: int
    price: Decimal = Decimal("0")
    trigger_price: Decimal = Decimal("0")
    status: OrderStatus = OrderStatus.NEW
    fill_price: Decimal = Decimal("0")
    fill_quantity: int = 0
    slippage: Decimal = Decimal("0")
    placed_at: datetime | None = None
    filled_at: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    tag: str = ""
    group_id: str | None = None


class Trade(BaseModel):
    """Executed trade record."""

    id: UUID = Field(default_factory=uuid4)
    order_id: UUID
    strategy_id: str
    instrument_token: int
    tradingsymbol: str
    side: OrderSide
    quantity: int
    price: Decimal
    charges: dict[str, Decimal] = Field(default_factory=dict)
    traded_at: datetime


# ─── Position & P&L Models ──────────────────────────────────────────


class Position(BaseModel):
    """Current position in an instrument."""

    instrument_token: int
    tradingsymbol: str
    strategy_id: str
    quantity: int  # Positive = long, Negative = short
    average_price: Decimal
    ltp: Decimal = Decimal("0")
    pnl: Decimal = Decimal("0")
    product: ProductType = ProductType.NRML
    greeks: Greeks = Field(default_factory=Greeks)


class PnL(BaseModel):
    """Profit and Loss summary."""

    realized: Decimal = Decimal("0")
    unrealized: Decimal = Decimal("0")
    charges: Decimal = Decimal("0")
    net: Decimal = Decimal("0")


class TradeCharges(BaseModel):
    """Breakdown of all charges for a trade."""

    brokerage: Decimal = Decimal("0")
    stt: Decimal = Decimal("0")
    transaction_charges: Decimal = Decimal("0")
    sebi_charges: Decimal = Decimal("0")
    gst: Decimal = Decimal("0")
    stamp_duty: Decimal = Decimal("0")
    total: Decimal = Decimal("0")


# ─── Signal Model ───────────────────────────────────────────────────


class SignalLeg(BaseModel):
    """A single leg of a trading signal."""

    tradingsymbol: str
    instrument_token: int
    order_side: OrderSide
    quantity: int
    order_type: OrderType = OrderType.MARKET
    price: Decimal = Decimal("0")


class Signal(BaseModel):
    """Trading signal generated by a strategy."""

    strategy_id: str
    signal_type: SignalType
    legs: list[SignalLeg]
    reason: str = ""
    timestamp: datetime = Field(default_factory=datetime.now)


# ─── Subscription Model ─────────────────────────────────────────────


class Subscription(BaseModel):
    """Instrument subscription request from a strategy."""

    instrument_tokens: list[int] = Field(default_factory=list)
    timeframes: list[Timeframe] = Field(default_factory=list)
