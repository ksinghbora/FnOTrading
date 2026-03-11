"""Order and Trade ORM models."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.session import Base


class OrderModel(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    broker_order_id: Mapped[str] = mapped_column(String(50), default="")
    strategy_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    instrument_token: Mapped[int] = mapped_column(Integer, nullable=False)
    tradingsymbol: Mapped[str] = mapped_column(String(50), nullable=False)
    order_side: Mapped[str] = mapped_column(String(10), nullable=False)
    order_type: Mapped[str] = mapped_column(String(10), nullable=False)
    product: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    trigger_price: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    fill_price: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    fill_quantity: Mapped[int] = mapped_column(Integer, default=0)
    slippage: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    placed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    group_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    tag: Mapped[str] = mapped_column(String(50), default="")


class TradeModel(Base):
    __tablename__ = "trades"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    strategy_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    instrument_token: Mapped[int] = mapped_column(Integer, nullable=False)
    tradingsymbol: Mapped[str] = mapped_column(String(50), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    charges: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    traded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
