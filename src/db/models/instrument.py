"""Instrument master ORM model."""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.session import Base


class InstrumentModel(Base):
    __tablename__ = "instruments"

    instrument_token: Mapped[int] = mapped_column(Integer, primary_key=True)
    exchange: Mapped[str] = mapped_column(String(10), nullable=False)
    tradingsymbol: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    segment: Mapped[str] = mapped_column(String(20), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(10), default="")
    strike: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    lot_size: Mapped[int] = mapped_column(Integer, default=1)
    tick_size: Mapped[float] = mapped_column(Numeric(6, 2), default=0.05)
    underlying: Mapped[str] = mapped_column(String(200), default="", index=True)
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
