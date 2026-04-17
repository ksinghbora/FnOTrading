"""Strategy configuration and state persistence ORM models."""

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.session import Base


class StrategyConfigModel(Base):
    __tablename__ = "strategy_configs"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    strategy_type: Mapped[str] = mapped_column(String(50), nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="IDLE")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StrategyStateModel(Base):
    """Per-day strategy state — keyed by (strategy_id, as_of_date).

    The composite key is intentional: it lets us load today's state on restart
    without ever loading yesterday's stale flags (`_entered`, `_stopped_for_day`).
    Apr 16 incident: IC re-entered immediately on restart because state was wiped.
    """

    __tablename__ = "strategy_states"

    strategy_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, primary_key=True)
    state_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
