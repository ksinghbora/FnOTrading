"""Advisory and confluence audit ORM models."""

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.session import Base


class AdvisoryModel(Base):
    __tablename__ = "advisories"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    full_advisory: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    input_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    day_bias: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ConfluenceAuditModel(Base):
    __tablename__ = "confluence_audits"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True, unique=True)
    decisions: Mapped[dict] = mapped_column(JSONB, nullable=False, default=list)
    agree_count: Mapped[int] = mapped_column(Integer, default=0)
    disagree_count: Mapped[int] = mapped_column(Integer, default=0)
    ai_right_count: Mapped[int] = mapped_column(Integer, default=0)
    rule_right_count: Mapped[int] = mapped_column(Integer, default=0)
    pnl_with_ai: Mapped[float] = mapped_column(Float, default=0.0)
    pnl_rules_only: Mapped[float] = mapped_column(Float, default=0.0)
    ai_alpha: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
