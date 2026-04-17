"""Persistent strategy state store — saves day flags to DB so restart preserves them.

The Apr 16 incident: IC re-entered immediately on restart because in-memory
`_entered`/`_stopped_for_day`/`_trades_today` got reset to defaults. This module
keeps state durable, keyed by (strategy_id, trading_date), so:
  - Restart on the same trading day → load today's state and resume.
  - Restart on a new trading day → no record → fresh start (correct daily reset).
"""

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from src.db.models.strategy_state import StrategyStateModel

logger = logging.getLogger(__name__)


class StrategyStateStore:
    """Async DB-backed state store for strategy day flags.

    Use save_state() after every state-mutating event (entry, exit, stop trigger).
    Use load_state() once at startup, before strategy.on_start().
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None):
        # session_factory may be None in tests/backtests that bypass DB
        self._session_factory = session_factory

    @property
    def enabled(self) -> bool:
        return self._session_factory is not None

    async def save_state(
        self, strategy_id: str, as_of_date: date, state_data: dict
    ) -> None:
        """Upsert today's state for a strategy. No-op if DB disabled."""
        if not self._session_factory:
            return
        try:
            async with self._session_factory() as session:
                stmt = pg_insert(StrategyStateModel).values(
                    strategy_id=strategy_id,
                    as_of_date=as_of_date,
                    state_data=state_data,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["strategy_id", "as_of_date"],
                    set_={"state_data": state_data},
                )
                await session.execute(stmt)
                await session.commit()
        except Exception as e:
            # State persistence must never crash the strategy — degrade gracefully.
            logger.warning(
                f"[STATE_STORE] save_state failed for {strategy_id}@{as_of_date}: {e}"
            )

    async def load_state(self, strategy_id: str, as_of_date: date) -> dict | None:
        """Load today's state. Returns None if no record exists or DB disabled."""
        if not self._session_factory:
            return None
        try:
            async with self._session_factory() as session:
                stmt = select(StrategyStateModel).where(
                    StrategyStateModel.strategy_id == strategy_id,
                    StrategyStateModel.as_of_date == as_of_date,
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                return row.state_data if row else None
        except Exception as e:
            logger.warning(
                f"[STATE_STORE] load_state failed for {strategy_id}@{as_of_date}: {e}"
            )
            return None
