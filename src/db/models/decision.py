"""Decision snapshot ORM model — one row per entry/exit/skip decision."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.session import Base


class DecisionSnapshotModel(Base):
    """Captures full market state at each trading decision point.

    Used for ML training: features at decision time + outcome P&L.
    One row per ENTER/EXIT/SKIP decision.
    """

    __tablename__ = "decision_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    strategy_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    leg: Mapped[str] = mapped_column(String(10), nullable=False)  # PREMIUM / TREND
    decision: Mapped[str] = mapped_column(String(10), nullable=False)  # ENTER / EXIT / SKIP
    mode: Mapped[str] = mapped_column(String(20), default="")  # strangle / iron_condor / debit_spread

    # Market state
    spot: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    vix: Mapped[float] = mapped_column(Float, nullable=False)
    pcr_oi: Mapped[float] = mapped_column(Float, default=0)
    max_pain: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    max_pain_dist_pct: Mapped[float] = mapped_column(Float, default=0)
    iv_skew_ratio: Mapped[float] = mapped_column(Float, default=0)  # put_iv / call_iv

    # Intraday context
    move_from_open_pct: Mapped[float] = mapped_column(Float, default=0)
    morning_range_pct: Mapped[float] = mapped_column(Float, default=0)

    # Time features
    dte: Mapped[int] = mapped_column(Integer, default=0)
    hour: Mapped[int] = mapped_column(Integer, nullable=False)
    minute: Mapped[int] = mapped_column(Integer, nullable=False)
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)  # 0=Mon
    is_expiry: Mapped[int] = mapped_column(Integer, default=0)  # 0/1

    # Scoring
    rule_score: Mapped[int] = mapped_column(Integer, default=0)
    ai_adj: Mapped[int] = mapped_column(Integer, default=0)
    final_score: Mapped[int] = mapped_column(Integer, default=0)
    threshold: Mapped[int] = mapped_column(Integer, default=0)

    # Regime
    regime: Mapped[str] = mapped_column(String(20), default="")  # CALM/MODERATE/HIGH/EXTREME

    # Entry-specific
    entry_premium: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    quantity: Mapped[int] = mapped_column(Integer, default=0)

    # Exit-specific
    exit_reason: Mapped[str] = mapped_column(String(100), default="")

    # Outcome (filled after position closes — NULL until then)
    outcome_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    held_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
