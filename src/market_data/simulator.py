"""Synthetic market data simulator for paper trading / demo mode.

Generates realistic ticks for NIFTY, BANKNIFTY spot prices, India VIX,
and option chains so the full system pipeline runs without a live broker
connection. Publishes tick events through the EventBus so strategies,
aggregator, and option chain builder all receive data.
"""

import asyncio
import logging
import math
import random
from datetime import date, datetime, timedelta
from decimal import Decimal

from src.core.constants import INDIA_VIX_TOKEN, LOT_SIZES
from src.core.events import Event, EventBus, EventType
from src.core.types import OptionType
from src.market_data.option_chain import OptionChainBuilder

logger = logging.getLogger(__name__)

# Actual Kite instrument tokens for index spot
NIFTY_SPOT_TOKEN = 256265
BANKNIFTY_SPOT_TOKEN = 260105

# Synthetic option tokens start here (avoid real token collisions)
_SYNTH_TOKEN_BASE = 500_000


def _next_tuesday(from_date: date) -> date:
    """Find the next Tuesday on or after from_date."""
    days_ahead = (1 - from_date.weekday()) % 7  # Tuesday = 1
    if days_ahead == 0 and from_date.weekday() == 1:
        days_ahead = 7  # Next week's Tuesday
    return from_date + timedelta(days=days_ahead)


def _last_tuesday_of_month(year: int, month: int) -> date:
    """Find the last Tuesday of a given month."""
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    last_day = next_month - timedelta(days=1)
    days_back = (last_day.weekday() - 1) % 7  # Tuesday = 1
    return last_day - timedelta(days=days_back)


class SimulationEngine:
    """Generates synthetic market ticks for demo / paper trading.

    Produces realistic price movements using geometric Brownian motion
    for spot prices and Black-Scholes-derived prices for options.
    """

    def __init__(
        self,
        event_bus: EventBus,
        chain_builder: OptionChainBuilder,
        tick_interval: float = 1.0,  # seconds between ticks
    ):
        self._event_bus = event_bus
        self._chain_builder = chain_builder
        self._tick_interval = tick_interval
        self._running = False
        self._task: asyncio.Task | None = None

        # Current synthetic prices
        self._nifty_spot = 22500.0
        self._banknifty_spot = 48000.0
        self._vix = 14.5

        # Session tracking
        self._nifty_open = self._nifty_spot
        self._banknifty_open = self._banknifty_spot
        self._tick_count = 0

        # Option token registry: (underlying, strike, opt_type) -> token
        self._option_tokens: dict[tuple[str, float, str], int] = {}
        self._next_token = _SYNTH_TOKEN_BASE

        # Expiry dates
        today = date.today()
        self._nifty_expiry = _next_tuesday(today)
        self._banknifty_expiry = _last_tuesday_of_month(today.year, today.month)
        if self._banknifty_expiry <= today:
            # Roll to next month
            if today.month == 12:
                self._banknifty_expiry = _last_tuesday_of_month(today.year + 1, 1)
            else:
                self._banknifty_expiry = _last_tuesday_of_month(today.year, today.month + 1)

    def _alloc_token(self, underlying: str, strike: float, opt_type: str) -> int:
        """Allocate a synthetic instrument token for an option."""
        key = (underlying, strike, opt_type)
        if key not in self._option_tokens:
            self._option_tokens[key] = self._next_token
            self._next_token += 1
        return self._option_tokens[key]

    async def start(self) -> None:
        """Start the simulation loop."""
        if self._running:
            return
        self._running = True

        # Register spot instruments
        self._chain_builder.register_spot(NIFTY_SPOT_TOKEN, "NIFTY")
        self._chain_builder.register_spot(BANKNIFTY_SPOT_TOKEN, "BANKNIFTY")

        # Register option instruments around current ATM
        self._register_options("NIFTY", self._nifty_spot, 50, 20, self._nifty_expiry)
        self._register_options("BANKNIFTY", self._banknifty_spot, 100, 20, self._banknifty_expiry)

        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            f"SimulationEngine started: NIFTY={self._nifty_spot:.0f} "
            f"BANKNIFTY={self._banknifty_spot:.0f} VIX={self._vix:.1f} "
            f"interval={self._tick_interval}s"
        )

    async def stop(self) -> None:
        """Stop the simulation."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("SimulationEngine stopped")

    def _register_options(
        self,
        underlying: str,
        spot: float,
        step: int,
        num_strikes: int,
        expiry: date,
    ) -> None:
        """Register synthetic option instruments in the chain builder."""
        atm = round(spot / step) * step
        for i in range(-num_strikes, num_strikes + 1):
            strike = atm + i * step
            strike_dec = Decimal(str(strike))

            for opt_type in (OptionType.CE, OptionType.PE):
                token = self._alloc_token(underlying, strike, opt_type.value)
                suffix = "CE" if opt_type == OptionType.CE else "PE"
                expiry_str = expiry.strftime("%y%b").upper()
                symbol = f"{underlying}{expiry_str}{strike}{suffix}"

                self._chain_builder.register_option(
                    instrument_token=token,
                    underlying=underlying,
                    expiry=expiry,
                    strike=strike_dec,
                    option_type=opt_type,
                    tradingsymbol=symbol,
                )

    async def _run_loop(self) -> None:
        """Main simulation loop."""
        while self._running:
            try:
                await self._generate_tick_batch()
                self._tick_count += 1
                await asyncio.sleep(self._tick_interval)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("SimulationEngine tick error")
                await asyncio.sleep(self._tick_interval)

    async def _generate_tick_batch(self) -> None:
        """Generate one batch of ticks for all instruments."""
        now = datetime.now()

        # Update prices with random walk (realistic per-second vol)
        self._nifty_spot = self._walk_price(self._nifty_spot, vol=0.00007)
        self._banknifty_spot = self._walk_price(self._banknifty_spot, vol=0.0001)
        self._vix = self._walk_vix(self._vix)

        # Publish spot ticks
        await self._publish_tick(
            NIFTY_SPOT_TOKEN, "NIFTY", self._nifty_spot, now
        )
        await self._publish_tick(
            BANKNIFTY_SPOT_TOKEN, "BANKNIFTY", self._banknifty_spot, now
        )

        # Publish VIX tick
        await self._publish_tick(
            INDIA_VIX_TOKEN, "INDIA VIX", self._vix, now
        )

        # Publish option ticks
        await self._publish_option_ticks("NIFTY", self._nifty_spot, self._nifty_expiry, now)
        await self._publish_option_ticks("BANKNIFTY", self._banknifty_spot, self._banknifty_expiry, now)

    async def _publish_tick(
        self, token: int, symbol: str, price: float, ts: datetime,
        volume: int = 0, oi: int = 0,
    ) -> None:
        """Publish a single tick event."""
        spread = max(0.05, price * 0.0001)
        tick_data = {
            "instrument_token": token,
            "tradingsymbol": symbol,
            "timestamp": ts.isoformat(),
            "ltp": str(round(price, 2)),
            "volume": volume,
            "oi": oi,
            "bid_price": str(round(price - spread, 2)),
            "ask_price": str(round(price + spread, 2)),
            "bid_qty": random.randint(50, 500),
            "ask_qty": random.randint(50, 500),
            "high": str(round(price * 1.002, 2)),
            "low": str(round(price * 0.998, 2)),
            "open": str(round(price, 2)),
            "close": str(round(price * 0.999, 2)),
        }
        event = Event.create(EventType.TICK, source="simulator", tick=tick_data)
        await self._event_bus.publish(event)

    async def _publish_option_ticks(
        self, underlying: str, spot: float, expiry: date, ts: datetime,
    ) -> None:
        """Publish ticks for all registered options of an underlying."""
        T = max(1 / 365, (expiry - date.today()).days / 365)
        r = 0.07
        iv_base = self._vix / 100  # Convert VIX percentage to decimal

        step = 50 if underlying == "NIFTY" else 100
        atm = round(spot / step) * step

        for i in range(-20, 21):
            strike = atm + i * step
            for opt_type in ("CE", "PE"):
                token = self._option_tokens.get((underlying, strike, opt_type))
                if token is None:
                    continue

                # Black-Scholes price
                price = self._bs_price(spot, strike, T, r, iv_base, opt_type)
                if price < 0.05:
                    price = 0.05

                # Add small noise (realistic bid-ask jitter)
                price *= 1 + random.gauss(0, 0.001)
                price = max(0.05, price)

                # Synthetic OI (higher near ATM)
                distance = abs(strike - spot) / spot
                oi = int(max(1000, 50000 * math.exp(-distance * 30)))
                volume = int(oi * random.uniform(0.1, 0.5))

                suffix = "CE" if opt_type == "CE" else "PE"
                expiry_str = expiry.strftime("%y%b").upper()
                symbol = f"{underlying}{expiry_str}{int(strike)}{suffix}"

                await self._publish_tick(
                    token, symbol, round(price, 2), ts,
                    volume=volume, oi=oi,
                )

    @staticmethod
    def _bs_price(
        S: float, K: float, T: float, r: float, sigma: float, opt_type: str,
    ) -> float:
        """Black-Scholes option price."""
        if sigma <= 0 or T <= 0:
            # Intrinsic value only
            if opt_type == "CE":
                return max(0, S - K)
            return max(0, K - S)

        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)

        if opt_type == "CE":
            price = S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
        else:
            price = K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)

        return max(0.0, price)

    @staticmethod
    def _walk_price(price: float, vol: float = 0.0003) -> float:
        """Random walk with slight mean reversion."""
        change = random.gauss(0, vol)
        return price * (1 + change)

    @staticmethod
    def _walk_vix(vix: float, mean: float = 14.5, speed: float = 0.02) -> float:
        """Mean-reverting VIX process (Ornstein-Uhlenbeck)."""
        noise = random.gauss(0, 0.15)
        reversion = speed * (mean - vix)
        new_vix = vix + reversion + noise
        return max(8.0, min(40.0, new_vix))


def _norm_cdf(x: float) -> float:
    """Standard normal CDF approximation (Abramowitz & Stegun)."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))
