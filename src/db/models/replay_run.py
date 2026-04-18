"""Replay-run ledger ORM model.

This is the SQL form of the ``data/replay_runs.jsonl`` audit trail written
by :func:`src.backtest.day_replay.replay_day`. The JSONL stays as the
durable, dependency-free record (see DATA_RELIABILITY_PLAN §7.7 — written
even when the DB is down). This table mirrors the same fields so analysts
can answer questions like:

    -- Has the golden-day hash drifted in the last 30 days?
    SELECT target_date, replay_hash, COUNT(*)
    FROM replay_runs
    WHERE strategy = 'portfolio' AND target_date >= now() - interval '30 days'
    GROUP BY target_date, replay_hash
    ORDER BY target_date DESC;

    -- Determinism rate per day (same code+params should always hash equal).
    SELECT target_date, code_sha, params_sha, COUNT(DISTINCT replay_hash) AS hash_variants
    FROM replay_runs
    WHERE error IS NULL
    GROUP BY target_date, code_sha, params_sha
    HAVING COUNT(DISTINCT replay_hash) > 1;

When a TimescaleDB extension is added (P2), this table promotes to a
hypertable in a single migration: ``SELECT create_hypertable('replay_runs',
'run_at', if_not_exists => TRUE);``. No model changes required. Until then
it's a vanilla indexed table.

Field ordering on the model is informational only — the DB row order is
governed by the migration. The JSONL serialization order is governed by
:meth:`src.backtest.day_replay.DayReplayResult` (which is itself stable
because the dataclass has an append-only field ordering convention — see
that class's docstring).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.session import Base


class ReplayRunModel(Base):
    """One row per call to :func:`src.backtest.day_replay.replay_day`."""

    __tablename__ = "replay_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # When the replay was *executed* (wall-clock UTC). For a hypertable
    # promotion this is the time-partition column.
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    # The trading day being replayed (NOT when the replay ran). Composite
    # index with strategy below — that's the most common query shape.
    target_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    strategy: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    underlying: Mapped[str] = mapped_column(String(20), nullable=False, default="NIFTY")
    seed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Determinism triple — same (code_sha, params_sha, seed) on the same
    # target_date MUST produce the same replay_hash. Mismatch is the headline
    # regression signal.
    code_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    params_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    params_source: Mapped[str] = mapped_column(String(255), nullable=False)
    snapshot_dir: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Engine outcomes. ``total_pnl`` and ``num_days`` are nullable because
    # an errored replay (engine raise) still lands a row with ``error`` set.
    total_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    num_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_coverage_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    fills_via_bid_ask: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fills_via_ltp_slip: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # The headline determinism token. SHA256 hex over canonical JSON of the
    # result body (excluding ``replay_hash`` and ``run_at``).
    replay_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Engine error captured into the row (NOT raised). Same field as
    # ``DayReplayResult.error`` — None on success.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
