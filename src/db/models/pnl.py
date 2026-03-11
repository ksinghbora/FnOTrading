"""P&L and Risk event ORM models."""

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.session import Base


class DailyPnLModel(Base):
    __tablename__ = "daily_pnl"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(50), primary_key=True, default="__overall__")
    realized_pnl: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    unrealized_pnl: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    charges: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    net_pnl: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    max_drawdown: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    num_trades: Mapped[int] = mapped_column(Integer, default=0)


class RiskEventModel(Base):
    __tablename__ = "risk_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    action_taken: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
