"""Instrument master download, parsing, and caching."""

import logging
from datetime import date, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.broker.base import BrokerClient
from src.core.models import Instrument
from src.core.types import InstrumentType
from src.db.models.instrument import InstrumentModel

logger = logging.getLogger(__name__)


class InstrumentManager:
    """Downloads, parses, and caches the NSE F&O instrument master."""

    def __init__(self, broker: BrokerClient, session_factory):
        self._broker = broker
        self._session_factory = session_factory
        self._instruments: dict[int, Instrument] = {}
        self._symbol_map: dict[str, int] = {}  # tradingsymbol -> token

    async def download_and_store(self) -> int:
        """Download instrument master from broker and store in database.

        Returns the number of instruments stored.
        """
        # Download from NFO (F&O segment)
        nfo_instruments = await self._broker.get_instruments("NFO")
        # Also download NSE for spot/index data
        nse_instruments = await self._broker.get_instruments("NSE")

        all_instruments = nfo_instruments + nse_instruments
        logger.info(f"Downloaded {len(all_instruments)} instruments")

        async with self._session_factory() as session:
            # Clear existing instruments
            await session.execute(delete(InstrumentModel))

            for inst in all_instruments:
                model = InstrumentModel(
                    instrument_token=inst["instrument_token"],
                    exchange=inst.get("exchange", ""),
                    tradingsymbol=inst.get("tradingsymbol", ""),
                    name=inst.get("name", ""),
                    segment=inst.get("segment", ""),
                    instrument_type=inst.get("instrument_type", ""),
                    strike=float(inst.get("strike", 0)),
                    expiry=inst.get("expiry") or None,
                    lot_size=int(inst.get("lot_size", 1)),
                    tick_size=float(inst.get("tick_size", 0.05)),
                    underlying=self._extract_underlying(inst),
                )
                session.add(model)

            await session.commit()

        count = len(all_instruments)
        logger.info(f"Stored {count} instruments in database")

        # Refresh in-memory cache
        await self.load_from_db()
        return count

    async def load_from_db(self) -> None:
        """Load instruments from database into memory."""
        async with self._session_factory() as session:
            result = await session.execute(select(InstrumentModel))
            rows = result.scalars().all()

        self._instruments.clear()
        self._symbol_map.clear()

        for row in rows:
            inst = Instrument(
                instrument_token=row.instrument_token,
                exchange=row.exchange,
                tradingsymbol=row.tradingsymbol,
                name=row.name,
                segment=row.segment,
                instrument_type=InstrumentType(row.instrument_type)
                if row.instrument_type in InstrumentType.__members__
                else InstrumentType.EQ,
                strike=row.strike,
                expiry=row.expiry,
                lot_size=row.lot_size,
                tick_size=row.tick_size,
                underlying=row.underlying,
            )
            self._instruments[row.instrument_token] = inst
            self._symbol_map[row.tradingsymbol] = row.instrument_token

        logger.info(f"Loaded {len(self._instruments)} instruments into memory")

    def get_by_token(self, token: int) -> Instrument | None:
        return self._instruments.get(token)

    def get_by_symbol(self, tradingsymbol: str) -> Instrument | None:
        token = self._symbol_map.get(tradingsymbol)
        if token is not None:
            return self._instruments.get(token)
        return None

    def get_token(self, tradingsymbol: str) -> int | None:
        return self._symbol_map.get(tradingsymbol)

    def get_fno_instruments(
        self,
        underlying: str | None = None,
        instrument_type: InstrumentType | None = None,
        expiry: date | None = None,
    ) -> list[Instrument]:
        """Filter F&O instruments by criteria."""
        results = []
        for inst in self._instruments.values():
            if inst.segment not in ("NFO-OPT", "NFO-FUT"):
                continue
            if underlying and inst.underlying != underlying:
                continue
            if instrument_type and inst.instrument_type != instrument_type:
                continue
            if expiry and inst.expiry != expiry:
                continue
            results.append(inst)
        return results

    def get_option_chain_instruments(
        self, underlying: str, expiry: date
    ) -> list[Instrument]:
        """Get all option instruments for an underlying + expiry."""
        return [
            inst
            for inst in self._instruments.values()
            if inst.underlying == underlying
            and inst.expiry == expiry
            and inst.instrument_type in (InstrumentType.CE, InstrumentType.PE)
        ]

    def _extract_underlying(self, inst: dict) -> str:
        """Extract underlying name from instrument data."""
        name = inst.get("name", "")
        tradingsymbol = inst.get("tradingsymbol", "")
        segment = inst.get("segment", "")

        if segment in ("NFO-OPT", "NFO-FUT"):
            # For index derivatives
            if "NIFTY" in tradingsymbol and "BANKNIFTY" not in tradingsymbol and "FINNIFTY" not in tradingsymbol:
                return "NIFTY"
            elif "BANKNIFTY" in tradingsymbol:
                return "BANKNIFTY"
            elif "FINNIFTY" in tradingsymbol:
                return "FINNIFTY"
            else:
                return name  # Stock F&O — name is the underlying
        return name or tradingsymbol
