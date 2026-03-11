"""Historical data feed for backtesting."""

import logging
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


class HistoricalDataFeed:
    """Reads historical OHLC data from TimescaleDB for backtesting."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def get_candles(
        self,
        instrument_token: int,
        from_date: datetime,
        to_date: datetime,
        timeframe: str = "1m",
    ) -> list[dict]:
        """Fetch historical candles from database."""
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT time, open, high, low, close, volume, oi "
                    "FROM candles "
                    "WHERE instrument_token = :token AND timeframe = :tf "
                    "AND time >= :from_dt AND time <= :to_dt "
                    "ORDER BY time"
                ),
                {
                    "token": instrument_token,
                    "tf": timeframe,
                    "from_dt": from_date,
                    "to_dt": to_date,
                },
            )
            rows = result.fetchall()

        return [
            {
                "date": row[0],
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": int(row[5]) if row[5] else 0,
                "oi": int(row[6]) if row[6] else 0,
            }
            for row in rows
        ]
