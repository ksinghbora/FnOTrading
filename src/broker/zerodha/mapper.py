"""Symbol mapping between internal representation and Kite instrument tokens."""

import logging
from datetime import date
from decimal import Decimal

from src.broker.zerodha.instruments import InstrumentManager
from src.core.types import InstrumentType, OptionType

logger = logging.getLogger(__name__)


class SymbolMapper:
    """Maps between human-readable option descriptions and Kite tradingsymbols."""

    def __init__(self, instrument_manager: InstrumentManager):
        self._instruments = instrument_manager

    def get_option_symbol(
        self,
        underlying: str,
        expiry: date,
        strike: float,
        option_type: OptionType,
    ) -> str | None:
        """Find the tradingsymbol for a specific option contract.

        Example: get_option_symbol("NIFTY", date(2026,3,17), 22500, OptionType.CE)
                 -> "NIFTY2631722500CE"
        """
        inst_type = InstrumentType.CE if option_type == OptionType.CE else InstrumentType.PE
        instruments = self._instruments.get_fno_instruments(
            underlying=underlying,
            instrument_type=inst_type,
            expiry=expiry,
        )
        for inst in instruments:
            if float(inst.strike) == float(strike):
                return inst.tradingsymbol
        return None

    def get_option_token(
        self,
        underlying: str,
        expiry: date,
        strike: float,
        option_type: OptionType,
    ) -> int | None:
        """Find the instrument token for a specific option contract."""
        symbol = self.get_option_symbol(underlying, expiry, strike, option_type)
        if symbol:
            return self._instruments.get_token(symbol)
        return None

    def get_futures_symbol(self, underlying: str, expiry: date) -> str | None:
        """Find the tradingsymbol for a futures contract."""
        instruments = self._instruments.get_fno_instruments(
            underlying=underlying,
            instrument_type=InstrumentType.FUT,
            expiry=expiry,
        )
        return instruments[0].tradingsymbol if instruments else None

    def get_futures_token(self, underlying: str, expiry: date) -> int | None:
        """Find the instrument token for a futures contract."""
        symbol = self.get_futures_symbol(underlying, expiry)
        if symbol:
            return self._instruments.get_token(symbol)
        return None

    def get_atm_strike(self, spot_price: float, underlying: str) -> float:
        """Calculate the ATM strike for a given spot price.

        Rounds to nearest valid strike interval (50 for NIFTY, 100 for BANKNIFTY).
        """
        strike_interval = self._get_strike_interval(underlying)
        return round(spot_price / strike_interval) * strike_interval

    def get_strikes_around_atm(
        self,
        spot_price: float,
        underlying: str,
        count: int = 10,
    ) -> list[float]:
        """Get 'count' strikes on each side of ATM."""
        atm = self.get_atm_strike(spot_price, underlying)
        interval = self._get_strike_interval(underlying)
        return [atm + (i * interval) for i in range(-count, count + 1)]

    def _get_strike_interval(self, underlying: str) -> float:
        """Get strike price interval for an underlying."""
        intervals = {
            "NIFTY": 50,
            "BANKNIFTY": 100,
            "FINNIFTY": 50,
        }
        return intervals.get(underlying, 50)
