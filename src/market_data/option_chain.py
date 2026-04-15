"""Live option chain builder — constructs and maintains option chain from ticks."""

import logging
from datetime import date, datetime
from decimal import Decimal

from src.core.constants import RISK_FREE_RATE
from src.core.clock import MarketClock
from src.core.events import Event, EventBus, EventType
from src.core.models import Greeks, OptionChain, OptionChainEntry, OptionData, Tick
from src.core.types import OptionType
from src.options.chain_analyzer import compute_max_pain, compute_pcr_oi, compute_pcr_volume, find_atm_strike
from src.options.greeks import compute_greeks
from src.options.iv import compute_iv

logger = logging.getLogger(__name__)


class OptionChainBuilder:
    """Builds and maintains live option chains from tick data.

    Enriches each option with computed Greeks and IV.
    Caches the chain in Redis for fast access by strategies.
    """

    def __init__(
        self,
        event_bus: EventBus,
        clock: MarketClock,
        redis_client=None,
    ):
        self._event_bus = event_bus
        self._clock = clock
        self._redis = redis_client

        # {underlying: {expiry: OptionChain}}
        self._chains: dict[str, dict[date, OptionChain]] = {}

        # Mapping: instrument_token -> (underlying, expiry, strike, option_type)
        self._token_map: dict[int, tuple[str, date, Decimal, OptionType]] = {}

        # Mapping: instrument_token -> tradingsymbol (from instrument master, not ticks)
        self._symbol_map: dict[int, str] = {}

        # Spot prices for underlyings
        self._spot_prices: dict[str, Decimal] = {}
        self._spot_tokens: dict[int, str] = {}  # spot token -> underlying name

        # OI change tracking — stores previous session's OI per (underlying, expiry, strike, type)
        self._prev_day_oi: dict[tuple[str, date, float, str], int] = {}
        self._oi_snapshot_date: date | None = None

        # Logging throttling
        self._tick_count = 0
        self._last_spot_log: dict[str, float] = {}  # underlying -> last logged spot

    def register_option(
        self,
        instrument_token: int,
        underlying: str,
        expiry: date,
        strike: Decimal,
        option_type: OptionType,
        tradingsymbol: str,
    ) -> None:
        """Register an option instrument for chain building."""
        self._token_map[instrument_token] = (underlying, expiry, strike, option_type)
        # Store tradingsymbol from instrument master — ticks don't include it
        self._symbol_map[instrument_token] = tradingsymbol

        if underlying not in self._chains:
            self._chains[underlying] = {}
        if expiry not in self._chains[underlying]:
            self._chains[underlying][expiry] = OptionChain(
                underlying=underlying,
                expiry=expiry,
                spot_price=Decimal("0"),
                atm_strike=Decimal("0"),
            )

    def register_spot(self, instrument_token: int, underlying: str) -> None:
        """Register a spot/index instrument for underlying price tracking."""
        self._spot_tokens[instrument_token] = underlying

    async def on_tick(self, event: Event) -> None:
        """Process tick event to update option chain."""
        tick_data = event.payload.get("tick")
        if not tick_data:
            return

        tick = Tick(**tick_data)
        token = tick.instrument_token

        self._tick_count += 1

        # Check if it's a spot tick
        if token in self._spot_tokens:
            underlying = self._spot_tokens[token]
            self._spot_prices[underlying] = tick.ltp
            # Update spot price in all chains for this underlying
            for chain in self._chains.get(underlying, {}).values():
                chain.spot_price = tick.ltp
                chain.atm_strike = find_atm_strike(float(tick.ltp), chain.strikes)
            # Throttled spot logging — log on >0.1% move or first tick
            last_logged = self._last_spot_log.get(underlying, 0)
            if last_logged == 0 or abs(float(tick.ltp) - last_logged) / last_logged > 0.001:
                chains = list(self._chains.get(underlying, {}).values())
                atm = chains[-1].atm_strike if chains else "N/A"
                logger.info(
                    f"[SPOT] underlying={underlying} price={tick.ltp} "
                    f"atm_strike={atm}"
                )
                self._last_spot_log[underlying] = float(tick.ltp)
            return

        # Check if it's an option tick
        if token not in self._token_map:
            return

        underlying, expiry, strike, option_type = self._token_map[token]
        chain = self._chains.get(underlying, {}).get(expiry)
        if not chain:
            return

        # Find or create strike entry
        entry = self._find_or_create_entry(chain, strike)

        # Compute time to expiry
        T = self._clock.time_to_expiry_years(expiry)
        spot = float(self._spot_prices.get(underlying, 0))

        # Compute IV and Greeks
        if spot > 0 and float(tick.ltp) > 0 and T > 0:
            iv = compute_iv(
                float(tick.ltp), spot, float(strike), T, RISK_FREE_RATE, option_type.value
            )
            if iv is None:
                iv = 0.0
            greeks = compute_greeks(spot, float(strike), T, RISK_FREE_RATE, iv, option_type.value)
        else:
            iv = 0.0
            greeks = Greeks()

        # Build option data — use registered tradingsymbol (ticks don't include it)
        opt_data = OptionData(
            tradingsymbol=self._symbol_map.get(token, tick.tradingsymbol),
            instrument_token=token,
            strike=strike,
            option_type=option_type,
            expiry=expiry,
            ltp=tick.ltp,
            bid_price=tick.bid_price,
            ask_price=tick.ask_price,
            volume=tick.volume,
            oi=tick.oi,
            greeks=greeks,
        )

        if option_type == OptionType.CE:
            entry.ce = opt_data
        else:
            entry.pe = opt_data

        # Update chain aggregates
        chain.updated_at = datetime.now()
        chain.pcr_oi = compute_pcr_oi(chain)
        chain.pcr_volume = compute_pcr_volume(chain)
        chain.max_pain = compute_max_pain(chain)
        chain.total_ce_oi = sum(e.ce.oi for e in chain.strikes if e.ce)
        chain.total_pe_oi = sum(e.pe.oi for e in chain.strikes if e.pe)

        # Periodic chain summary (every 1000 option ticks)
        if self._tick_count % 1000 == 0:
            complete = sum(1 for e in chain.strikes if e.ce and e.pe)
            logger.info(
                f"[CHAIN] underlying={underlying} expiry={expiry} "
                f"strikes={len(chain.strikes)} complete={complete} "
                f"pcr_oi={chain.pcr_oi:.2f} max_pain={chain.max_pain} "
                f"total_ce_oi={chain.total_ce_oi} total_pe_oi={chain.total_pe_oi}"
            )

        # Cache in Redis (throttled, not every tick)
        if self._redis:
            await self._cache_chain(underlying, expiry, chain)

    def snapshot_oi_for_next_day(self) -> None:
        """Save current OI as previous-day reference. Call at end of trading day."""
        today = self._clock.today()
        if self._oi_snapshot_date == today:
            return  # Already snapshotted today

        self._prev_day_oi.clear()
        for underlying, expiry_chains in self._chains.items():
            for expiry, chain in expiry_chains.items():
                for entry in chain.strikes:
                    strike = float(entry.strike)
                    if entry.ce and entry.ce.oi > 0:
                        self._prev_day_oi[(underlying, expiry, strike, "CE")] = entry.ce.oi
                    if entry.pe and entry.pe.oi > 0:
                        self._prev_day_oi[(underlying, expiry, strike, "PE")] = entry.pe.oi

        self._oi_snapshot_date = today
        logger.info(f"[OI_SNAPSHOT] Saved {len(self._prev_day_oi)} OI entries for next day reference")

    def get_oi_change(self, underlying: str, expiry: date, strike: float, opt_type: str) -> int:
        """Get OI change from previous day. Positive = new positions added."""
        prev = self._prev_day_oi.get((underlying, expiry, strike, opt_type), 0)
        chain = self.get_chain(underlying, expiry)
        if not chain:
            return 0
        for entry in chain.strikes:
            if float(entry.strike) == strike:
                opt = entry.ce if opt_type == "CE" else entry.pe
                if opt:
                    return opt.oi - prev
        return 0

    def get_total_oi_changes(self, underlying: str, expiry: date) -> tuple[int, int]:
        """Get total CE and PE OI changes from previous day."""
        chain = self.get_chain(underlying, expiry)
        if not chain:
            return 0, 0
        ce_change = 0
        pe_change = 0
        for entry in chain.strikes:
            strike = float(entry.strike)
            if entry.ce:
                prev = self._prev_day_oi.get((underlying, expiry, strike, "CE"), 0)
                ce_change += entry.ce.oi - prev
            if entry.pe:
                prev = self._prev_day_oi.get((underlying, expiry, strike, "PE"), 0)
                pe_change += entry.pe.oi - prev
        return ce_change, pe_change

    def get_chain(self, underlying: str, expiry: date) -> OptionChain | None:
        """Get the current option chain."""
        return self._chains.get(underlying, {}).get(expiry)

    def get_all_expiries(self, underlying: str) -> list[date]:
        """Get all available expiry dates for an underlying."""
        return sorted(self._chains.get(underlying, {}).keys())

    def get_spot_price(self, underlying: str) -> Decimal:
        """Get the latest spot price for an underlying."""
        return self._spot_prices.get(underlying, Decimal("0"))

    def _find_or_create_entry(self, chain: OptionChain, strike: Decimal) -> OptionChainEntry:
        """Find existing strike entry or create a new one."""
        for entry in chain.strikes:
            if entry.strike == strike:
                return entry
        entry = OptionChainEntry(strike=strike)
        chain.strikes.append(entry)
        chain.strikes.sort(key=lambda e: float(e.strike))
        return entry

    async def _cache_chain(self, underlying: str, expiry: date, chain: OptionChain) -> None:
        """Cache option chain in Redis."""
        try:
            key = f"chain:{underlying}:{expiry.isoformat()}"
            await self._redis.set(key, chain.model_dump_json(), ex=30)
        except Exception:
            pass  # Non-critical
