"""Tick and OHLC candle ORM models for TimescaleDB hypertables."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from src.db.session import Base


class TickModel(Base):
    """Raw tick data — stored in a TimescaleDB hypertable."""

    __tablename__ = "ticks"

    # Composite primary key: time + instrument_token
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    instrument_token: Mapped[int] = mapped_column(Integer, primary_key=True)
    tradingsymbol: Mapped[str] = mapped_column(String(50), nullable=False)
    ltp: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, default=0)
    oi: Mapped[int] = mapped_column(BigInteger, default=0)
    bid_price: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    ask_price: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    bid_qty: Mapped[int] = mapped_column(Integer, default=0)
    ask_qty: Mapped[int] = mapped_column(Integer, default=0)


class CandleModel(Base):
    """OHLC candle data — stored in a TimescaleDB hypertable."""

    __tablename__ = "candles"

    # Composite primary key: time + instrument_token + timeframe
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    instrument_token: Mapped[int] = mapped_column(Integer, primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(5), primary_key=True)
    tradingsymbol: Mapped[str] = mapped_column(String(50), nullable=False)
    open: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    high: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    low: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    close: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, default=0)
    oi: Mapped[int] = mapped_column(BigInteger, default=0)


class OptionChainSnapshotModel(Base):
    """Option chain snapshot — stored in a TimescaleDB hypertable."""

    __tablename__ = "option_chain_snapshots"

    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    underlying: Mapped[str] = mapped_column(String(20), primary_key=True)
    expiry: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    strike: Mapped[float] = mapped_column(Numeric(10, 2), primary_key=True)
    option_type: Mapped[str] = mapped_column(String(2), primary_key=True)
    ltp: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    iv: Mapped[float] = mapped_column(Numeric(8, 4), default=0)
    delta: Mapped[float] = mapped_column(Numeric(8, 4), default=0)
    gamma: Mapped[float] = mapped_column(Numeric(8, 6), default=0)
    theta: Mapped[float] = mapped_column(Numeric(8, 4), default=0)
    vega: Mapped[float] = mapped_column(Numeric(8, 4), default=0)
    oi: Mapped[int] = mapped_column(BigInteger, default=0)
    volume: Mapped[int] = mapped_column(BigInteger, default=0)
    bid_price: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    ask_price: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
