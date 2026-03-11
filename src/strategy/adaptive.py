"""Adaptive strategy selector — automatically picks the best strategy per regime.

Instead of running a fixed strategy all day, this meta-strategy:
1. Waits until 9:25 AM (5 min of data after open)
2. Assesses the market regime via RegimeDetector
3. Launches the recommended strategy with adjusted position size
4. Re-assesses every 30 minutes and can switch strategies mid-day
"""

import logging
from datetime import date, time
from decimal import Decimal

from src.core.constants import LOT_SIZES
from src.core.models import Signal, Subscription, Tick
from src.strategy.base import BaseStrategy
from src.strategy.params import BaseStrategyParams, IronCondorParams, ShortStrangleParams
from src.strategy.regime import MarketRegime, RegimeDetector, RegimeSnapshot
from src.strategy.registry import register_strategy
from src.strategy.signals import exit_signal, make_leg

from src.core.types import OrderSide

logger = logging.getLogger(__name__)


class AdaptiveParams(BaseStrategyParams):
    """Parameters for the adaptive meta-strategy."""

    entry_time: time = time(9, 25)         # Wait 10 min after open for data
    reassess_interval_minutes: int = 30    # Re-check regime every N minutes
    max_switches_per_day: int = 2          # Don't flip-flop too much
    # Strangle defaults (when regime selects strangle)
    strangle_call_delta: float = 0.20
    strangle_put_delta: float = -0.20
    strangle_stop_loss_pct: float = 50.0
    strangle_trail_stop_pct: float = 20.0
    # Iron condor defaults (when regime selects IC)
    ic_short_call_delta: float = 0.20
    ic_short_put_delta: float = -0.20
    ic_wing_width: int = 10
    ic_stop_loss_pct: float = 100.0


@register_strategy("adaptive", AdaptiveParams)
class AdaptiveStrategy(BaseStrategy):
    """Meta-strategy that selects the optimal strategy based on market regime.

    Flow:
    1. on_start: initialize regime detector
    2. on_tick (9:25+): assess regime -> launch sub-strategy
    3. Every 30 min: re-assess, switch if regime changed
    4. Exit time: close everything
    """

    params: AdaptiveParams

    def __init__(self, strategy_id: str, params: AdaptiveParams):
        super().__init__(strategy_id, params)
        self._regime_detector: RegimeDetector | None = None
        self._current_regime: MarketRegime = MarketRegime.UNKNOWN
        self._current_strategy_type: str = ""  # "short_strangle" or "iron_condor"
        self._entered = False
        self._switches_today: int = 0
        self._last_assess_minute: int = -1
        self._expiry: date | None = None
        self._lot_size: int = LOT_SIZES.get(params.underlying, 75)
        self._quantity: int = params.quantity_lots * self._lot_size

        # Active position tracking (common for both sub-strategies)
        self._short_ce_token: int = 0
        self._short_pe_token: int = 0
        self._short_ce_symbol: str = ""
        self._short_pe_symbol: str = ""
        self._long_ce_token: int = 0  # Only for iron condor
        self._long_pe_token: int = 0
        self._long_ce_symbol: str = ""
        self._long_pe_symbol: str = ""
        self._entry_premium: Decimal = Decimal("0")
        self._peak_premium: Decimal = Decimal("0")

    def get_subscriptions(self) -> Subscription:
        return Subscription(instrument_tokens=[], timeframes=[])

    async def on_start(self) -> None:
        self._expiry = self.ctx.next_expiry(self.params.underlying)
        self._regime_detector = RegimeDetector(
            self.ctx._feed, self.ctx._aggregator, self.ctx._chain_builder
        )
        logger.info(
            f"[{self.strategy_id}] Adaptive strategy started: "
            f"underlying={self.params.underlying} expiry={self._expiry}"
        )

    async def on_tick(self, tick: Tick) -> Signal | None:
        now = self.ctx.clock.now()

        # Expiry rollover
        new_expiry = self._check_expiry_rollover(self._expiry, self.params.underlying)
        if new_expiry:
            self._expiry = new_expiry

        # Exit time
        if now.time() >= self.params.exit_time and self._entered:
            return self._create_exit_signal("Exit time reached")

        # Not yet entry time
        if now.time() < self.params.entry_time:
            return None

        # Assess regime (on first tick after entry time, then every N minutes)
        current_minute = now.hour * 60 + now.minute
        should_assess = (
            not self._entered
            or (current_minute - self._last_assess_minute >= self.params.reassess_interval_minutes)
        )

        if should_assess and self._regime_detector:
            self._last_assess_minute = current_minute
            snapshot = self._regime_detector.assess(self.params.underlying)
            return self._handle_regime(snapshot)

        # Monitor active position
        if self._entered:
            return self._check_position()

        return None

    def _handle_regime(self, snapshot: RegimeSnapshot) -> Signal | None:
        """Act on the regime assessment."""
        new_regime = snapshot.regime

        # Should sit out
        if snapshot.recommended_strategy == "sit_out":
            if self._entered:
                logger.info(
                    f"[{self.strategy_id}] Regime changed to {new_regime.value}, "
                    f"closing position: {snapshot.reason}"
                )
                return self._create_exit_signal(f"Regime: {snapshot.reason}")
            self._current_regime = new_regime
            return None

        # Not entered yet — enter with recommended strategy
        if not self._entered:
            self._current_regime = new_regime
            self._quantity = max(
                self._lot_size,
                int(self.params.quantity_lots * snapshot.recommended_lots_multiplier) * self._lot_size
            )
            if snapshot.recommended_strategy == "iron_condor":
                return self._enter_iron_condor()
            else:
                return self._enter_strangle()

        # Already entered — check if regime changed enough to warrant a switch
        if (
            new_regime != self._current_regime
            and self._switches_today < self.params.max_switches_per_day
        ):
            # Only switch on significant regime changes
            significant_change = (
                (self._current_regime in (MarketRegime.LOW_VOL_RANGE, MarketRegime.NORMAL_RANGE)
                 and new_regime in (MarketRegime.TRENDING, MarketRegime.EXTREME_VOL))
                or
                (self._current_regime == MarketRegime.HIGH_VOL_RANGE
                 and new_regime in (MarketRegime.TRENDING, MarketRegime.EXTREME_VOL))
            )
            if significant_change:
                self._switches_today += 1
                logger.info(
                    f"[{self.strategy_id}] Regime shift: {self._current_regime.value} -> "
                    f"{new_regime.value}. Closing position (switch #{self._switches_today})"
                )
                self._current_regime = new_regime
                return self._create_exit_signal(f"Regime shift to {new_regime.value}")

        return None

    def _enter_strangle(self) -> Signal | None:
        """Enter a short strangle using delta-based strike selection."""
        chain = self.ctx.get_option_chain(self.params.underlying, self._expiry)
        if not chain or not chain.strikes:
            return None

        # Find CE at target delta
        best_ce = None
        best_ce_diff = float("inf")
        best_pe = None
        best_pe_diff = float("inf")

        for entry in chain.strikes:
            if entry.ce and entry.ce.greeks.delta > 0:
                diff = abs(entry.ce.greeks.delta - self.params.strangle_call_delta)
                if diff < best_ce_diff:
                    best_ce_diff = diff
                    best_ce = entry
            if entry.pe and entry.pe.greeks.delta < 0:
                diff = abs(entry.pe.greeks.delta - self.params.strangle_put_delta)
                if diff < best_pe_diff:
                    best_pe_diff = diff
                    best_pe = entry

        if not best_ce or not best_ce.ce or not best_pe or not best_pe.pe:
            return None

        self._short_ce_token = best_ce.ce.instrument_token
        self._short_ce_symbol = best_ce.ce.tradingsymbol
        self._short_pe_token = best_pe.pe.instrument_token
        self._short_pe_symbol = best_pe.pe.tradingsymbol
        self._long_ce_token = 0
        self._long_pe_token = 0

        ce_ltp = self.ctx.get_ltp(self._short_ce_token)
        pe_ltp = self.ctx.get_ltp(self._short_pe_token)
        self._entry_premium = ce_ltp + pe_ltp
        self._peak_premium = self._entry_premium

        from src.strategy.signals import entry_signal
        legs = [
            make_leg(self._short_ce_symbol, self._short_ce_token, OrderSide.SELL, self._quantity),
            make_leg(self._short_pe_symbol, self._short_pe_token, OrderSide.SELL, self._quantity),
        ]

        self._entered = True
        self._current_strategy_type = "short_strangle"

        logger.info(
            f"[{self.strategy_id}] ENTRY (strangle): "
            f"CE@{float(best_ce.strike)} PE@{float(best_pe.strike)} "
            f"premium={self._entry_premium} qty={self._quantity} "
            f"regime={self._current_regime.value}"
        )

        return entry_signal(
            self.strategy_id, legs,
            f"Adaptive strangle: regime={self._current_regime.value}"
        )

    def _enter_iron_condor(self) -> Signal | None:
        """Enter an iron condor with delta-based short strikes and fixed wings."""
        chain = self.ctx.get_option_chain(self.params.underlying, self._expiry)
        if not chain or not chain.strikes:
            return None

        strike_interval = 50 if self.params.underlying in ("NIFTY", "FINNIFTY") else 100

        # Find short strikes by delta
        best_short_ce = None
        best_ce_diff = float("inf")
        best_short_pe = None
        best_pe_diff = float("inf")

        for entry in chain.strikes:
            if entry.ce and entry.ce.greeks.delta > 0:
                diff = abs(entry.ce.greeks.delta - self.params.ic_short_call_delta)
                if diff < best_ce_diff:
                    best_ce_diff = diff
                    best_short_ce = entry
            if entry.pe and entry.pe.greeks.delta < 0:
                diff = abs(entry.pe.greeks.delta - self.params.ic_short_put_delta)
                if diff < best_pe_diff:
                    best_pe_diff = diff
                    best_short_pe = entry

        if not best_short_ce or not best_short_ce.ce or not best_short_pe or not best_short_pe.pe:
            return None

        # Calculate wing strikes
        short_ce_strike = float(best_short_ce.strike)
        short_pe_strike = float(best_short_pe.strike)
        long_ce_strike = short_ce_strike + (self.params.ic_wing_width * strike_interval)
        long_pe_strike = short_pe_strike - (self.params.ic_wing_width * strike_interval)

        # Find long leg instruments
        long_ce_entry = None
        long_pe_entry = None
        for entry in chain.strikes:
            s = float(entry.strike)
            if s == long_ce_strike and entry.ce:
                long_ce_entry = entry
            if s == long_pe_strike and entry.pe:
                long_pe_entry = entry

        if not long_ce_entry or not long_ce_entry.ce or not long_pe_entry or not long_pe_entry.pe:
            logger.warning(
                f"[{self.strategy_id}] Could not find IC wing strikes "
                f"(CE@{long_ce_strike}, PE@{long_pe_strike}). Aborting entry."
            )
            return None

        self._short_ce_token = best_short_ce.ce.instrument_token
        self._short_ce_symbol = best_short_ce.ce.tradingsymbol
        self._short_pe_token = best_short_pe.pe.instrument_token
        self._short_pe_symbol = best_short_pe.pe.tradingsymbol
        self._long_ce_token = long_ce_entry.ce.instrument_token
        self._long_ce_symbol = long_ce_entry.ce.tradingsymbol
        self._long_pe_token = long_pe_entry.pe.instrument_token
        self._long_pe_symbol = long_pe_entry.pe.tradingsymbol

        short_ce_ltp = self.ctx.get_ltp(self._short_ce_token)
        short_pe_ltp = self.ctx.get_ltp(self._short_pe_token)
        long_ce_ltp = self.ctx.get_ltp(self._long_ce_token)
        long_pe_ltp = self.ctx.get_ltp(self._long_pe_token)
        self._entry_premium = (short_ce_ltp + short_pe_ltp) - (long_ce_ltp + long_pe_ltp)
        self._peak_premium = self._entry_premium

        from src.strategy.signals import entry_signal
        legs = [
            make_leg(self._short_ce_symbol, self._short_ce_token, OrderSide.SELL, self._quantity),
            make_leg(self._short_pe_symbol, self._short_pe_token, OrderSide.SELL, self._quantity),
            make_leg(self._long_ce_symbol, self._long_ce_token, OrderSide.BUY, self._quantity),
            make_leg(self._long_pe_symbol, self._long_pe_token, OrderSide.BUY, self._quantity),
        ]

        self._entered = True
        self._current_strategy_type = "iron_condor"

        logger.info(
            f"[{self.strategy_id}] ENTRY (iron condor): "
            f"short CE@{short_ce_strike} PE@{short_pe_strike} "
            f"long CE@{long_ce_strike} PE@{long_pe_strike} "
            f"credit={self._entry_premium} qty={self._quantity} "
            f"regime={self._current_regime.value}"
        )

        return entry_signal(
            self.strategy_id, legs,
            f"Adaptive IC: regime={self._current_regime.value}"
        )

    def _check_position(self) -> Signal | None:
        """Monitor active position for stop loss and trailing stop."""
        short_ce_ltp = self.ctx.get_ltp(self._short_ce_token)
        short_pe_ltp = self.ctx.get_ltp(self._short_pe_token)
        current_cost = short_ce_ltp + short_pe_ltp

        if self._current_strategy_type == "iron_condor":
            long_ce_ltp = self.ctx.get_ltp(self._long_ce_token)
            long_pe_ltp = self.ctx.get_ltp(self._long_pe_token)
            current_cost = current_cost - (long_ce_ltp + long_pe_ltp)

        if self._entry_premium <= 0:
            return None

        # For premium sellers: premium going UP means losing money
        change_pct = float((current_cost - self._entry_premium) / self._entry_premium * 100)

        # Stop loss
        sl_pct = (
            self.params.ic_stop_loss_pct
            if self._current_strategy_type == "iron_condor"
            else self.params.strangle_stop_loss_pct
        )
        if change_pct > sl_pct:
            self._entered = False
            return self._create_exit_signal(f"Stop loss: +{change_pct:.1f}%")

        # Trailing stop (strangle only, IC has defined risk)
        trail_pct = self.params.strangle_trail_stop_pct
        if self._current_strategy_type == "short_strangle" and trail_pct > 0:
            if current_cost < self._peak_premium:
                self._peak_premium = min(self._peak_premium, current_cost)
            elif current_cost > self._peak_premium:
                bounce_pct = float(
                    (current_cost - self._peak_premium) / self._entry_premium * 100
                )
                if bounce_pct > trail_pct:
                    self._entered = False
                    return self._create_exit_signal(
                        f"Trailing stop: bounced {bounce_pct:.1f}%"
                    )

        return None

    def _create_exit_signal(self, reason: str) -> Signal:
        """Close all legs of the active position."""
        legs = [
            make_leg(self._short_ce_symbol, self._short_ce_token, OrderSide.BUY, self._quantity),
            make_leg(self._short_pe_symbol, self._short_pe_token, OrderSide.BUY, self._quantity),
        ]
        if self._current_strategy_type == "iron_condor" and self._long_ce_token:
            legs.extend([
                make_leg(self._long_ce_symbol, self._long_ce_token, OrderSide.SELL, self._quantity),
                make_leg(self._long_pe_symbol, self._long_pe_token, OrderSide.SELL, self._quantity),
            ])

        self._entered = False
        return exit_signal(self.strategy_id, legs, reason)

    async def on_stop(self) -> None:
        if self._entered:
            logger.info(
                f"[{self.strategy_id}] Stopping with open {self._current_strategy_type} position"
            )

    def get_state_data(self) -> dict:
        return {
            "entered": self._entered,
            "current_strategy_type": self._current_strategy_type,
            "current_regime": self._current_regime.value,
            "switches_today": self._switches_today,
            "short_ce_token": self._short_ce_token,
            "short_pe_token": self._short_pe_token,
            "short_ce_symbol": self._short_ce_symbol,
            "short_pe_symbol": self._short_pe_symbol,
            "long_ce_token": self._long_ce_token,
            "long_pe_token": self._long_pe_token,
            "long_ce_symbol": self._long_ce_symbol,
            "long_pe_symbol": self._long_pe_symbol,
            "entry_premium": str(self._entry_premium),
            "peak_premium": str(self._peak_premium),
        }

    def load_state_data(self, data: dict) -> None:
        self._entered = data.get("entered", False)
        self._current_strategy_type = data.get("current_strategy_type", "")
        try:
            self._current_regime = MarketRegime(data.get("current_regime", "unknown"))
        except ValueError:
            self._current_regime = MarketRegime.UNKNOWN
        self._switches_today = data.get("switches_today", 0)
        self._short_ce_token = data.get("short_ce_token", 0)
        self._short_pe_token = data.get("short_pe_token", 0)
        self._short_ce_symbol = data.get("short_ce_symbol", "")
        self._short_pe_symbol = data.get("short_pe_symbol", "")
        self._long_ce_token = data.get("long_ce_token", 0)
        self._long_pe_token = data.get("long_pe_token", 0)
        self._long_ce_symbol = data.get("long_ce_symbol", "")
        self._long_pe_symbol = data.get("long_pe_symbol", "")
        self._entry_premium = Decimal(data.get("entry_premium", "0"))
        self._peak_premium = Decimal(data.get("peak_premium", "0"))
