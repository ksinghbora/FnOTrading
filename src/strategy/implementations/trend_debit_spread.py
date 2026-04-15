"""Trend Debit Spread strategy — profits from trending markets.

Buys debit spreads (bull call spread or bear put spread) when morning range
breakout is confirmed by OI levels. Naturally complementary to premium sellers
which sit out during TRENDING regime.

Defined risk: max loss = debit paid (spread width - max value).
"""

import logging
import os
import time as _time
from datetime import date, time
from decimal import Decimal

from src.core.constants import LOT_SIZES
from src.core.models import Signal, Subscription, Tick
from src.core.types import OrderSide, Timeframe
from src.options.chain_analyzer import get_high_oi_strikes
from src.strategy.base import BaseStrategy
from src.strategy.indicators import BreakoutSignal, momentum_breakout, oi_breakout_confirm
from src.strategy.params import TrendDebitSpreadParams
from src.strategy.regime import RegimeDetector
from src.strategy.registry import register_strategy
from src.strategy.scoring import TREND_DEBIT_SPREAD_CONFIG, score_strategy
from src.strategy.signals import entry_signal, exit_signal, make_leg

logger = logging.getLogger(__name__)


@register_strategy("trend_debit_spread", TrendDebitSpreadParams)
class TrendDebitSpreadStrategy(BaseStrategy):
    """Buys debit spreads on confirmed morning range breakouts.

    Entry Logic:
    1. After 9:45, get M5 candles -> compute morning range (9:15-9:30)
    2. Check if spot broke above morning high (UP) or below morning low (DOWN)
    3. Optionally confirm with OI levels (spot must breach high-OI resistance/support)
    4. Bullish: Buy CE at ATM+1 strike, Sell CE at ATM+1+width (Bull Call Spread)
    5. Bearish: Buy PE at ATM-1 strike, Sell PE at ATM-1-width (Bear Put Spread)

    Exit Logic:
    - Profit target: spread reaches X% of max value
    - Stop loss: spread drops X% from entry debit
    - Trailing stop: trail by X% after 50%+ of max profit
    - Time stop: exit at 15:00
    """

    params: TrendDebitSpreadParams

    def __init__(self, strategy_id: str, params: TrendDebitSpreadParams):
        super().__init__(strategy_id, params)
        self._entered = False
        self._stopped_for_day = False
        self._regime: RegimeDetector | None = None
        self._paper_mode: bool = os.environ.get("PAPER_TRADING", "false").lower() == "true"
        self._trades_today: int = 0
        self._direction: str = ""  # "UP" or "DOWN"
        # Spread leg tracking
        self._buy_token: int = 0
        self._buy_symbol: str = ""
        self._buy_strike: float = 0
        self._sell_token: int = 0
        self._sell_symbol: str = ""
        self._sell_strike: float = 0
        # P&L tracking
        self._entry_debit: Decimal = Decimal("0")  # Net premium paid
        self._max_spread_value: Decimal = Decimal("0")  # Theoretical max
        self._peak_value: Decimal = Decimal("0")  # Peak spread value for trailing stop
        # Config
        self._expiry: date | None = None
        self._lot_size: int = LOT_SIZES.get(params.underlying, 75)
        self._quantity: int = params.quantity_lots * self._lot_size

    def get_subscriptions(self) -> Subscription:
        return Subscription(instrument_tokens=[], timeframes=[])

    async def on_start(self) -> None:
        self._expiry = self.ctx.next_expiry(self.params.underlying)
        self._regime = RegimeDetector(self.ctx._feed, self.ctx._aggregator, self.ctx._chain_builder)
        logger.info(
            f"[{self.strategy_id}] Started: {self.params.underlying} "
            f"expiry={self._expiry} spread_width={self.params.spread_width_strikes} "
            f"log_only={self.params.log_only}"
        )

    async def on_tick(self, tick: Tick) -> Signal | None:
        now = self.ctx.clock.now()

        # Expiry rollover
        new_expiry = self._check_expiry_rollover(self._expiry, self.params.underlying)
        if new_expiry:
            self._expiry = new_expiry

        # Time-based exit
        if now.time() >= self.params.exit_time and self._entered:
            self._stopped_for_day = True
            return self._create_exit_signal("Exit time reached")

        # Hard block: DTE ≤ 2 — debit spreads need time to reach max value
        if self._expiry and not self._entered:
            dte = (self._expiry - now.date()).days
            if dte <= 2:
                if now.minute == 0:
                    logger.info(f"[{self.strategy_id}] TREND hard-blocked: DTE={dte} ≤ 2")
                return None

        # Entry window
        if (
            not self._entered
            and not self._stopped_for_day
            and now.time() >= self.params.entry_time
            and self._trades_today < self.params.max_trades_per_day
        ):
            return await self._try_entry()

        # Monitor open position
        if self._entered:
            return self._check_exit()

        return None

    async def _try_entry(self) -> Signal | None:
        """Detect breakout and enter debit spread."""
        # --- Signal scoring ---
        vix = self.ctx.get_vix()
        morning_range_pct = 0.0
        move_from_open_pct = 0.0
        if self._regime:
            regime = self._regime.assess(self.params.underlying)
            morning_range_pct = regime.morning_range_pct
            move_from_open_pct = regime.move_from_open_pct
        pcr_oi = 0.0
        if self._expiry:
            chain_for_score = self.ctx.get_option_chain(self.params.underlying, self._expiry)
            if chain_for_score:
                pcr_oi = chain_for_score.pcr_oi
        dte = (self._expiry - self.ctx.clock.now().date()).days if self._expiry else 0
        is_expiry_day = self._expiry == self.ctx.clock.now().date() if self._expiry else False

        score, reasons = score_strategy(
            TREND_DEBIT_SPREAD_CONFIG, vix, morning_range_pct, move_from_open_pct,
            pcr_oi, is_expiry_day, dte,
        )
        reasons_str = ", ".join(reasons)
        logger.info(
            f"[SIGNAL_SCORE] strategy={self.strategy_id} score={score}/100 [{reasons_str}]"
        )
        if score < 60:
            if self._paper_mode:
                logger.info(
                    f"[SHADOW_BLOCK] strategy={self.strategy_id} score={score}/100 "
                    f"< 60 — proceeding anyway (paper mode)"
                )
            else:
                logger.info(
                    f"[{self.strategy_id}] Entry skipped: signal score {score}/100 < 60"
                )
                return None

        # Get M5 candles for breakout detection
        spot_token = self._find_spot_token()
        if not spot_token:
            return None

        candles = self.ctx.get_candles(spot_token, Timeframe.M5, limit=50)
        if len(candles) < 4:
            return None

        # Detect morning range breakout
        breakout = momentum_breakout(
            candles,
            morning_candles=3,
            confirmation_pct=self.params.breakout_confirmation_pct,
        )

        if not breakout.direction:
            return None

        spot = float(self.ctx.get_spot_price(self.params.underlying))
        if spot <= 0:
            return None

        # Regime confirmation — breakout must align with regime assessment
        regime_confirms = True
        regime_label = "unknown"
        if self._regime:
            from src.strategy.regime import MarketRegime
            regime = self._regime.assess(self.params.underlying)
            regime_label = regime.regime.value
            regime_confirms = regime.regime == MarketRegime.TRENDING
            if not regime_confirms:
                if self._paper_mode:
                    logger.info(
                        f"[{self.strategy_id}] [SHADOW_BLOCK] Breakout detected but regime={regime_label} "
                        f"(not TRENDING) — entering anyway (paper mode)"
                    )
                else:
                    logger.info(
                        f"[{self.strategy_id}] Breakout {breakout.direction} blocked: "
                        f"regime={regime_label} (not TRENDING)"
                    )
                    return None

        # OI confirmation
        oi_confirmed = True
        if self.params.oi_confirm:
            chain = self.ctx.get_option_chain(self.params.underlying, self._expiry)
            if chain and chain.strikes:
                high_oi = get_high_oi_strikes(chain)
                oi_confirmed = oi_breakout_confirm(high_oi, spot, breakout.direction)

        # Log the signal regardless
        logger.info(
            f"[TREND_SIGNAL] strategy={self.strategy_id} "
            f"direction={breakout.direction} strength={breakout.strength:.2f}% "
            f"breakout_level={breakout.breakout_level:.0f} "
            f"morning_range=[{breakout.morning_low:.0f}-{breakout.morning_high:.0f}] "
            f"regime={regime_label} regime_confirms={regime_confirms} "
            f"spot={spot:.0f} oi_confirmed={oi_confirmed}"
        )

        if not oi_confirmed:
            logger.info(
                f"[{self.strategy_id}] Breakout {breakout.direction} not confirmed by OI levels"
            )
            return None

        # Log-only mode: don't actually trade
        if self.params.log_only:
            logger.info(
                f"[{self.strategy_id}] LOG_ONLY: Would enter {breakout.direction} "
                f"debit spread at spot={spot:.0f}"
            )
            return None

        # Build the debit spread
        return self._build_spread(breakout, spot)

    def _build_spread(self, breakout: BreakoutSignal, spot: float) -> Signal | None:
        """Construct the debit spread legs from the option chain."""
        chain = self.ctx.get_option_chain(self.params.underlying, self._expiry)
        if not chain or not chain.strikes:
            return None

        strike_interval = 50 if self.params.underlying in ("NIFTY", "FINNIFTY") else 100
        width = self.params.spread_width_strikes * strike_interval
        atm = float(chain.atm_strike)

        if breakout.direction == "UP":
            # Bull Call Spread: Buy CE at ATM (50Δ), Sell CE at ATM+width
            buy_strike = atm
            sell_strike = buy_strike + width
            option_type = "ce"
        else:
            # Bear Put Spread: Buy PE at ATM (50Δ), Sell PE at ATM-width
            buy_strike = atm
            sell_strike = buy_strike - width
            option_type = "pe"

        # Find the strikes in the chain
        buy_entry = None
        sell_entry = None
        for entry in chain.strikes:
            s = float(entry.strike)
            if s == buy_strike:
                buy_entry = entry
            elif s == sell_strike:
                sell_entry = entry
            if buy_entry and sell_entry:
                break

        if not buy_entry or not sell_entry:
            logger.warning(
                f"[{self.strategy_id}] Cannot find spread strikes: "
                f"buy@{buy_strike} sell@{sell_strike}"
            )
            return None

        # Get option data for the correct type
        if option_type == "ce":
            buy_opt = buy_entry.ce
            sell_opt = sell_entry.ce
        else:
            buy_opt = buy_entry.pe
            sell_opt = sell_entry.pe

        if not buy_opt or not sell_opt:
            logger.warning(
                f"[{self.strategy_id}] Missing {option_type.upper()} data for spread strikes"
            )
            return None

        # Store leg info
        self._buy_token = buy_opt.instrument_token
        self._buy_symbol = buy_opt.tradingsymbol
        self._buy_strike = buy_strike
        self._sell_token = sell_opt.instrument_token
        self._sell_symbol = sell_opt.tradingsymbol
        self._sell_strike = sell_strike
        self._direction = breakout.direction

        # Calculate entry debit and max spread value
        buy_ltp = self.ctx.get_ltp(self._buy_token)
        sell_ltp = self.ctx.get_ltp(self._sell_token)
        self._entry_debit = buy_ltp - sell_ltp  # Net premium paid
        self._max_spread_value = Decimal(str(abs(width)))  # Max value = strike width
        self._peak_value = self._entry_debit  # Start tracking from entry

        if self._entry_debit <= 0:
            logger.warning(
                f"[{self.strategy_id}] Invalid debit: buy@{buy_ltp} sell@{sell_ltp}"
            )
            return None

        # Minimum debit check — reject near-worthless expiry-day spreads
        if self._entry_debit < Decimal("10"):
            logger.info(
                f"[{self.strategy_id}] [SHADOW_BLOCK] Debit too low ({self._entry_debit:.2f}) "
                f"— likely expiry day, skipping"
            )
            return None

        legs = [
            make_leg(self._buy_symbol, self._buy_token, OrderSide.BUY, self._quantity),
            make_leg(self._sell_symbol, self._sell_token, OrderSide.SELL, self._quantity),
        ]

        self._entered = True
        self._trades_today += 1

        logger.info(
            f"[ENTRY] strategy={self.strategy_id} type=debit_spread "
            f"direction={self._direction} "
            f"buy={self._buy_strike}@{buy_ltp} sell={self._sell_strike}@{sell_ltp} "
            f"debit={self._entry_debit} max_value={self._max_spread_value} "
            f"qty={self._quantity}"
        )

        spread_type = "Bull Call" if self._direction == "UP" else "Bear Put"
        return entry_signal(
            self.strategy_id, legs,
            f"{spread_type} Spread: buy@{self._buy_strike} sell@{self._sell_strike}"
        )

    def _check_exit(self) -> Signal | None:
        """Monitor spread value and check exit conditions."""
        if not self._entered or self._entry_debit <= 0:
            return None

        buy_ltp = self.ctx.get_ltp(self._buy_token)
        sell_ltp = self.ctx.get_ltp(self._sell_token)
        current_value = buy_ltp - sell_ltp  # Current spread value

        # Update peak for trailing stop
        if current_value > self._peak_value:
            self._peak_value = current_value

        # Profit target: spread reaches X% of max theoretical value
        if self._max_spread_value > 0:
            value_pct = float(current_value / self._max_spread_value * 100)
            if value_pct >= self.params.profit_target_pct:
                return self._create_exit_signal(
                    f"Profit target: spread at {value_pct:.1f}% of max value"
                )

        # Stop loss: spread dropped X% from entry debit
        if self._entry_debit > 0:
            loss_pct = float(
                (self._entry_debit - current_value) / self._entry_debit * 100
            )
            if loss_pct >= self.params.stop_loss_pct:
                return self._create_exit_signal(
                    f"Stop loss: spread lost {loss_pct:.1f}% of entry debit"
                )

        # Trailing stop: once 20%+ of max profit reached, trail from peak
        if self.params.trailing_stop_pct > 0 and self._max_spread_value > 0:
            max_profit = self._max_spread_value - self._entry_debit
            if max_profit > 0:
                current_profit = current_value - self._entry_debit
                profit_pct = float(current_profit / max_profit * 100)
                if profit_pct >= 20:  # Activate after 20% of max profit (was 50% — never triggered)
                    pullback_from_peak = float(
                        (self._peak_value - current_value) / self._peak_value * 100
                    ) if self._peak_value > 0 else 0
                    if pullback_from_peak >= self.params.trailing_stop_pct:
                        return self._create_exit_signal(
                            f"Trailing stop: pullback {pullback_from_peak:.1f}% "
                            f"from peak (threshold: {self.params.trailing_stop_pct}%)"
                        )

        return None

    def _create_exit_signal(self, reason: str) -> Signal:
        """Create signal to close both spread legs."""
        buy_ltp = self.ctx.get_ltp(self._buy_token)
        sell_ltp = self.ctx.get_ltp(self._sell_token)
        exit_value = buy_ltp - sell_ltp
        pnl_estimate = exit_value - self._entry_debit

        logger.info(
            f"[EXIT] strategy={self.strategy_id} reason={reason} "
            f"direction={self._direction} "
            f"entry_debit={self._entry_debit} exit_value={exit_value} "
            f"estimated_pnl={pnl_estimate} qty={self._quantity}"
        )

        self._entered = False
        legs = [
            # Sell the long leg (close buy position)
            make_leg(self._buy_symbol, self._buy_token, OrderSide.SELL, self._quantity),
            # Buy back the short leg (close sell position)
            make_leg(self._sell_symbol, self._sell_token, OrderSide.BUY, self._quantity),
        ]
        return exit_signal(self.strategy_id, legs, reason)

    def _find_spot_token(self) -> int | None:
        """Find the spot instrument token for the underlying."""
        chain_builder = self.ctx._chain_builder
        for token, name in chain_builder._spot_tokens.items():
            if name == self.params.underlying:
                return token
        return None

    async def on_stop(self) -> None:
        if self._entered:
            logger.info(f"[{self.strategy_id}] Stopping with open position")

    def reset_day_state(self) -> None:
        """Reset intraday flags at start of new trading day."""
        self._entered = False
        self._stopped_for_day = False
        self._trades_today = 0
        self._direction = ""
        self._peak_value = Decimal("0")

    def get_state_data(self) -> dict:
        return {
            "entered": self._entered,
            "stopped_for_day": self._stopped_for_day,
            "trades_today": self._trades_today,
            "direction": self._direction,
            "buy_token": self._buy_token,
            "buy_symbol": self._buy_symbol,
            "buy_strike": self._buy_strike,
            "sell_token": self._sell_token,
            "sell_symbol": self._sell_symbol,
            "sell_strike": self._sell_strike,
            "entry_debit": str(self._entry_debit),
            "max_spread_value": str(self._max_spread_value),
            "peak_value": str(self._peak_value),
        }

    def load_state_data(self, data: dict) -> None:
        self._entered = data.get("entered", False)
        self._stopped_for_day = data.get("stopped_for_day", False)
        self._trades_today = data.get("trades_today", 0)
        self._direction = data.get("direction", "")
        self._buy_token = data.get("buy_token", 0)
        self._buy_symbol = data.get("buy_symbol", "")
        self._buy_strike = data.get("buy_strike", 0)
        self._sell_token = data.get("sell_token", 0)
        self._sell_symbol = data.get("sell_symbol", "")
        self._sell_strike = data.get("sell_strike", 0)
        self._entry_debit = Decimal(data.get("entry_debit", "0"))
        self._max_spread_value = Decimal(data.get("max_spread_value", "0"))
        self._peak_value = Decimal(data.get("peak_value", "0"))
