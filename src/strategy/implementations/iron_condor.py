"""Iron Condor strategy — defined-risk spread with 4 legs.

Sells near-OTM CE + PE and buys far-OTM CE + PE for protection.
Profits from time decay when the market stays within the short strikes.
Defined max loss = wing width - net credit received.
"""

import logging
import os
import time as _time
from datetime import date, time
from decimal import Decimal

from src.core.constants import LOT_SIZES
from src.core.models import Signal, SignalLeg, Subscription, Tick
from src.core.types import OrderSide
from src.strategy.base import BaseStrategy
from src.strategy.params import IronCondorParams
from src.strategy.regime import RegimeDetector
from src.strategy.registry import register_strategy
from src.strategy.scoring import IRON_CONDOR_CONFIG, score_strategy
from src.strategy.signals import adjust_signal, entry_signal, exit_signal, make_leg

logger = logging.getLogger(__name__)


@register_strategy("iron_condor", IronCondorParams)
class IronCondorStrategy(BaseStrategy):
    """Sells near-OTM CE + PE and buys far-OTM CE + PE (wings).

    Features:
    - Delta-based strike selection for short legs
    - Wing width configurable in number of strikes
    - Defined risk: max loss = wing width - net credit
    - Adjustment: close threatened side and re-enter at new strikes
    - Stop loss at configurable % of max credit received
    - Expiry day: no adjustments (gamma too high, let wings protect)
    - Max 2 adjustments per day to prevent churn
    """

    params: IronCondorParams

    MAX_ADJUSTMENTS_PER_DAY = 2  # Cap adjustments to prevent churn

    def __init__(self, strategy_id: str, params: IronCondorParams):
        super().__init__(strategy_id, params)
        self._entered = False
        self._stopped_for_day = False
        self._regime: RegimeDetector | None = None
        self._paper_mode: bool = os.environ.get("PAPER_TRADING", "false").lower() == "true"
        # Short legs
        self._short_ce_token: int = 0
        self._short_pe_token: int = 0
        self._short_ce_symbol: str = ""
        self._short_pe_symbol: str = ""
        self._short_ce_strike: float = 0
        self._short_pe_strike: float = 0
        # Long legs (wings)
        self._long_ce_token: int = 0
        self._long_pe_token: int = 0
        self._long_ce_symbol: str = ""
        self._long_pe_symbol: str = ""
        self._long_ce_strike: float = 0
        self._long_pe_strike: float = 0
        # Tracking
        self._entry_credit: Decimal = Decimal("0")
        self._expiry: date | None = None
        self._lot_size: int = LOT_SIZES.get(params.underlying, 75)
        self._quantity: int = params.quantity_lots * self._lot_size
        self._last_adjustment_time: float = 0.0
        self._adjustments_today: int = 0

    def get_subscriptions(self) -> Subscription:
        return Subscription(instrument_tokens=[], timeframes=[])

    async def on_start(self) -> None:
        self._expiry = self.ctx.next_expiry(self.params.underlying)
        self._regime = RegimeDetector(self.ctx._feed, self.ctx._aggregator, self.ctx._chain_builder)
        logger.info(
            f"[{self.strategy_id}] Started: {self.params.underlying} "
            f"expiry={self._expiry} short_call_delta={self.params.short_call_delta} "
            f"short_put_delta={self.params.short_put_delta} "
            f"wing_width={self.params.wing_width_strikes}"
        )

    async def on_tick(self, tick: Tick) -> Signal | None:
        now = self.ctx.clock.now()

        # Expiry rollover
        new_expiry = self._check_expiry_rollover(self._expiry, self.params.underlying)
        if new_expiry:
            self._expiry = new_expiry

        # Stop for day to prevent re-entry loop after exit_time
        if now.time() >= self.params.exit_time and self._entered:
            self._stopped_for_day = True
            return self._create_exit_signal("Exit time reached")

        if not self._entered and not self._stopped_for_day and now.time() >= self.params.entry_time:
            return await self._try_entry()

        if self._entered:
            return self._check_adjustments()

        return None

    async def _try_entry(self) -> Signal | None:
        """Select strikes by delta and enter the iron condor."""
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
            IRON_CONDOR_CONFIG, vix, morning_range_pct, move_from_open_pct,
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

        # Expiry-day 0DTE block — wings go illiquid + STT trap if ITM at close
        expiry_block = self._check_expiry_day_block(self.params.underlying)
        if expiry_block:
            logger.info(f"[{self.strategy_id}] Entry skipped: {expiry_block}")
            return None

        # VIX filter
        vix_block = self._check_vix_filter()
        if vix_block:
            if self._paper_mode:
                logger.info(f"[{self.strategy_id}] [SHADOW_BLOCK] {vix_block}")
            else:
                logger.info(f"[{self.strategy_id}] Entry skipped: {vix_block}")
                return None

        # PCR filter
        pcr_block = self._check_pcr_filter(self.params.underlying, self._expiry)
        if pcr_block:
            if self._paper_mode:
                logger.info(f"[{self.strategy_id}] [SHADOW_BLOCK] {pcr_block}")
            else:
                logger.info(f"[{self.strategy_id}] Entry skipped: {pcr_block}")
                return None

        # Max pain filter
        mp_block = self._check_max_pain_filter(self.params.underlying, self._expiry)
        if mp_block:
            if self._paper_mode:
                logger.info(f"[{self.strategy_id}] [SHADOW_BLOCK] {mp_block}")
            else:
                logger.info(f"[{self.strategy_id}] Entry skipped: {mp_block}")
                return None

        # Log IV skew and OI levels for research
        self._log_iv_skew(self.params.underlying, self._expiry)
        self._log_oi_levels(self.params.underlying, self._expiry)

        chain = self.ctx.get_option_chain(self.params.underlying, self._expiry)
        if not chain or not chain.strikes:
            return None

        # VIX-adjusted position sizing
        adjusted_lots = self._get_vix_adjusted_lots()
        self._quantity = adjusted_lots * self._lot_size

        strike_interval = 50 if self.params.underlying in ("NIFTY", "FINNIFTY") else 100

        # Find short CE strike closest to target delta
        best_short_ce = None
        best_short_ce_diff = float("inf")
        best_short_pe = None
        best_short_pe_diff = float("inf")

        for entry in chain.strikes:
            if entry.ce and entry.ce.greeks.delta > 0:
                diff = abs(entry.ce.greeks.delta - self.params.short_call_delta)
                if diff < best_short_ce_diff:
                    best_short_ce_diff = diff
                    best_short_ce = entry

            if entry.pe and entry.pe.greeks.delta < 0:
                diff = abs(entry.pe.greeks.delta - self.params.short_put_delta)
                if diff < best_short_pe_diff:
                    best_short_pe_diff = diff
                    best_short_pe = entry

        if (
            not best_short_ce or not best_short_ce.ce
            or not best_short_pe or not best_short_pe.pe
        ):
            logger.warning(f"[{self.strategy_id}] Could not find suitable short strikes")
            return None

        # Determine long (wing) strikes
        wing_offset = self.params.wing_width_strikes * strike_interval
        self._short_ce_strike = float(best_short_ce.strike)
        self._short_pe_strike = float(best_short_pe.strike)
        self._long_ce_strike = self._short_ce_strike + wing_offset
        self._long_pe_strike = self._short_pe_strike - wing_offset

        # Find all 4 legs in the chain
        self._short_ce_token = best_short_ce.ce.instrument_token
        self._short_ce_symbol = best_short_ce.ce.tradingsymbol
        self._short_pe_token = best_short_pe.pe.instrument_token
        self._short_pe_symbol = best_short_pe.pe.tradingsymbol

        long_ce_found = False
        long_pe_found = False

        for entry in chain.strikes:
            strike = float(entry.strike)
            if strike == self._long_ce_strike and entry.ce:
                self._long_ce_token = entry.ce.instrument_token
                self._long_ce_symbol = entry.ce.tradingsymbol
                long_ce_found = True
            elif strike == self._long_pe_strike and entry.pe:
                self._long_pe_token = entry.pe.instrument_token
                self._long_pe_symbol = entry.pe.tradingsymbol
                long_pe_found = True

            if long_ce_found and long_pe_found:
                break

        if not long_ce_found or not long_pe_found:
            logger.warning(
                f"[{self.strategy_id}] Could not find wing strikes: "
                f"long_ce@{self._long_ce_strike} found={long_ce_found}, "
                f"long_pe@{self._long_pe_strike} found={long_pe_found}"
            )
            return None

        # Calculate net credit
        short_ce_ltp = self.ctx.get_ltp(self._short_ce_token)
        short_pe_ltp = self.ctx.get_ltp(self._short_pe_token)
        long_ce_ltp = self.ctx.get_ltp(self._long_ce_token)
        long_pe_ltp = self.ctx.get_ltp(self._long_pe_token)
        self._entry_credit = (short_ce_ltp + short_pe_ltp) - (long_ce_ltp + long_pe_ltp)

        legs = [
            # Short legs (sell near OTM)
            make_leg(self._short_ce_symbol, self._short_ce_token, OrderSide.SELL, self._quantity),
            make_leg(self._short_pe_symbol, self._short_pe_token, OrderSide.SELL, self._quantity),
            # Long legs (buy far OTM — wings)
            make_leg(self._long_ce_symbol, self._long_ce_token, OrderSide.BUY, self._quantity),
            make_leg(self._long_pe_symbol, self._long_pe_token, OrderSide.BUY, self._quantity),
        ]

        self._entered = True
        logger.info(
            f"[ENTRY] strategy={self.strategy_id} type=iron_condor "
            f"short_ce={self._short_ce_strike}@{short_ce_ltp} "
            f"short_pe={self._short_pe_strike}@{short_pe_ltp} "
            f"long_ce={self._long_ce_strike}@{long_ce_ltp} "
            f"long_pe={self._long_pe_strike}@{long_pe_ltp} "
            f"net_credit={self._entry_credit} qty={self._quantity}"
        )

        return entry_signal(
            self.strategy_id, legs,
            f"Iron Condor CE@{self._short_ce_strike}/{self._long_ce_strike} "
            f"PE@{self._short_pe_strike}/{self._long_pe_strike}"
        )

    def _check_adjustments(self) -> Signal | None:
        """Monitor position and adjust threatened side if needed."""
        if not self._entered or self._entry_credit <= 0:
            return None

        # Calculate current net value of the position
        short_ce_ltp = self.ctx.get_ltp(self._short_ce_token)
        short_pe_ltp = self.ctx.get_ltp(self._short_pe_token)
        long_ce_ltp = self.ctx.get_ltp(self._long_ce_token)
        long_pe_ltp = self.ctx.get_ltp(self._long_pe_token)

        current_debit = (short_ce_ltp + short_pe_ltp) - (long_ce_ltp + long_pe_ltp)

        # Profit target — exit when spread value has decayed enough
        if self.params.profit_target_pct > 0:
            decay_pct = float((self._entry_credit - current_debit) / self._entry_credit * 100)
            if decay_pct >= self.params.profit_target_pct:
                logger.info(
                    f"[{self.strategy_id}] PROFIT TARGET: spread decayed {decay_pct:.1f}% "
                    f"(target: {self.params.profit_target_pct}%)"
                )
                self._stopped_for_day = True
                return self._create_exit_signal(f"Profit target: spread decayed {decay_pct:.1f}%")

        loss_pct = float((current_debit - self._entry_credit) / self._entry_credit * 100)

        # Stop loss check on entire position
        if loss_pct > self.params.stop_loss_pct:
            logger.info(
                f"[{self.strategy_id}] STOP LOSS: position loss {loss_pct:.1f}% "
                f"(threshold: {self.params.stop_loss_pct}%)"
            )
            self._stopped_for_day = True
            return self._create_exit_signal(f"Stop loss: position loss +{loss_pct:.1f}%")

        # Cooldown: don't adjust more than once per 30 minutes (simulated time)
        # Uses strategy clock so it works in both live and backtest
        now_ts = self.ctx.clock.now().timestamp()
        if now_ts - self._last_adjustment_time < 30 * 60:
            return None

        # Skip adjustment if less than 1 hour to close — cost > remaining theta
        now_time = self.ctx.clock.now().time()
        if now_time >= time(14, 15):
            return None

        # Cap adjustments per day — each adjustment locks in loss + charges
        if self._adjustments_today >= self.MAX_ADJUSTMENTS_PER_DAY:
            return None

        # Skip adjustments on expiry day — gamma is too high, adjustments churn
        # Wings provide defined risk protection, let them do their job
        if self._expiry and self.ctx.clock.now().date() == self._expiry:
            logger.debug(
                f"[{self.strategy_id}] Skipping adjustment on expiry day — wings protect"
            )
            return None

        # Check if one side is threatened
        # Cost to close a spread = buy back short - sell long
        # Positive value = spread is losing money for seller (short leg ITM)
        call_close_cost = float(short_ce_ltp - long_ce_ltp)
        put_close_cost = float(short_pe_ltp - long_pe_ltp)
        entry_credit_f = float(self._entry_credit)
        threshold = self.params.adjustment_threshold_pct / 100.0

        if entry_credit_f > 0 and call_close_cost / entry_credit_f > threshold:
            logger.info(
                f"[{self.strategy_id}] ADJUSTMENT: Call side threatened, "
                f"call spread close cost={call_close_cost:.1f} "
                f"({call_close_cost / entry_credit_f * 100:.1f}% of credit)"
            )
            return self._adjust_threatened_side("call")

        if entry_credit_f > 0 and put_close_cost / entry_credit_f > threshold:
            logger.info(
                f"[{self.strategy_id}] ADJUSTMENT: Put side threatened, "
                f"put spread close cost={put_close_cost:.1f} "
                f"({put_close_cost / entry_credit_f * 100:.1f}% of credit)"
            )
            return self._adjust_threatened_side("put")

        return None

    def _adjust_threatened_side(self, side: str) -> Signal | None:
        """Close the threatened side and re-enter at new strikes.

        This creates an ADJUST signal that closes the losing spread
        and opens a new one closer to the current spot.
        Returns None if a valid 4-leg adjustment cannot be formed
        (prevents creating naked short positions).
        """
        chain = self.ctx.get_option_chain(self.params.underlying, self._expiry)
        if not chain or not chain.strikes:
            logger.warning(f"[{self.strategy_id}] No chain available for {side} adjustment")
            return None

        strike_interval = 50 if self.params.underlying in ("NIFTY", "FINNIFTY") else 100
        wing_offset = self.params.wing_width_strikes * strike_interval

        # Build close legs and new open legs separately
        close_legs: list[SignalLeg] = []
        open_legs: list[SignalLeg] = []

        # Save old state in case we need to rollback
        old_short_ce_token, old_short_ce_symbol, old_short_ce_strike = self._short_ce_token, self._short_ce_symbol, self._short_ce_strike
        old_long_ce_token, old_long_ce_symbol, old_long_ce_strike = self._long_ce_token, self._long_ce_symbol, self._long_ce_strike
        old_short_pe_token, old_short_pe_symbol, old_short_pe_strike = self._short_pe_token, self._short_pe_symbol, self._short_pe_strike
        old_long_pe_token, old_long_pe_symbol, old_long_pe_strike = self._long_pe_token, self._long_pe_symbol, self._long_pe_strike

        if side == "call":
            # Close existing call spread
            close_legs.append(make_leg(self._short_ce_symbol, self._short_ce_token, OrderSide.BUY, self._quantity))
            close_legs.append(make_leg(self._long_ce_symbol, self._long_ce_token, OrderSide.SELL, self._quantity))

            # Find new short CE at current target delta
            best_ce = None
            best_diff = float("inf")
            for entry in chain.strikes:
                if entry.ce and entry.ce.greeks.delta > 0:
                    diff = abs(entry.ce.greeks.delta - self.params.short_call_delta)
                    if diff < best_diff:
                        best_diff = diff
                        best_ce = entry

            if not best_ce or not best_ce.ce:
                logger.warning(f"[{self.strategy_id}] Cannot find new short CE for adjustment")
                return None

            new_short_strike = float(best_ce.strike)
            new_long_strike = new_short_strike + wing_offset

            # Find wing strike FIRST before committing to adjustment
            wing_entry = None
            for entry in chain.strikes:
                if float(entry.strike) == new_long_strike and entry.ce:
                    wing_entry = entry
                    break

            if not wing_entry or not wing_entry.ce:
                logger.warning(
                    f"[{self.strategy_id}] Cannot find wing CE at {new_long_strike} — "
                    f"aborting adjustment to prevent naked short"
                )
                return None

            # Both strikes found — safe to proceed
            self._short_ce_token = best_ce.ce.instrument_token
            self._short_ce_symbol = best_ce.ce.tradingsymbol
            self._short_ce_strike = new_short_strike
            open_legs.append(make_leg(self._short_ce_symbol, self._short_ce_token, OrderSide.SELL, self._quantity))

            self._long_ce_token = wing_entry.ce.instrument_token
            self._long_ce_symbol = wing_entry.ce.tradingsymbol
            self._long_ce_strike = new_long_strike
            open_legs.append(make_leg(self._long_ce_symbol, self._long_ce_token, OrderSide.BUY, self._quantity))

        elif side == "put":
            # Close existing put spread
            close_legs.append(make_leg(self._short_pe_symbol, self._short_pe_token, OrderSide.BUY, self._quantity))
            close_legs.append(make_leg(self._long_pe_symbol, self._long_pe_token, OrderSide.SELL, self._quantity))

            # Find new short PE at current target delta
            best_pe = None
            best_diff = float("inf")
            for entry in chain.strikes:
                if entry.pe and entry.pe.greeks.delta < 0:
                    diff = abs(entry.pe.greeks.delta - self.params.short_put_delta)
                    if diff < best_diff:
                        best_diff = diff
                        best_pe = entry

            if not best_pe or not best_pe.pe:
                logger.warning(f"[{self.strategy_id}] Cannot find new short PE for adjustment")
                return None

            new_short_strike = float(best_pe.strike)
            new_long_strike = new_short_strike - wing_offset

            # Find wing strike FIRST before committing to adjustment
            wing_entry = None
            for entry in chain.strikes:
                if float(entry.strike) == new_long_strike and entry.pe:
                    wing_entry = entry
                    break

            if not wing_entry or not wing_entry.pe:
                logger.warning(
                    f"[{self.strategy_id}] Cannot find wing PE at {new_long_strike} — "
                    f"aborting adjustment to prevent naked short"
                )
                return None

            # Both strikes found — safe to proceed
            self._short_pe_token = best_pe.pe.instrument_token
            self._short_pe_symbol = best_pe.pe.tradingsymbol
            self._short_pe_strike = new_short_strike
            open_legs.append(make_leg(self._short_pe_symbol, self._short_pe_token, OrderSide.SELL, self._quantity))

            self._long_pe_token = wing_entry.pe.instrument_token
            self._long_pe_symbol = wing_entry.pe.tradingsymbol
            self._long_pe_strike = new_long_strike
            open_legs.append(make_leg(self._long_pe_symbol, self._long_pe_token, OrderSide.BUY, self._quantity))

        # Validate we have exactly 4 legs (2 close + 2 open) for a complete spread roll
        all_legs = close_legs + open_legs
        if len(all_legs) != 4:
            logger.error(
                f"[{self.strategy_id}] Adjustment produced {len(all_legs)} legs instead of 4 — "
                f"rolling back to prevent naked position"
            )
            # Rollback state
            self._short_ce_token, self._short_ce_symbol, self._short_ce_strike = old_short_ce_token, old_short_ce_symbol, old_short_ce_strike
            self._long_ce_token, self._long_ce_symbol, self._long_ce_strike = old_long_ce_token, old_long_ce_symbol, old_long_ce_strike
            self._short_pe_token, self._short_pe_symbol, self._short_pe_strike = old_short_pe_token, old_short_pe_symbol, old_short_pe_strike
            self._long_pe_token, self._long_pe_symbol, self._long_pe_strike = old_long_pe_token, old_long_pe_symbol, old_long_pe_strike
            return None

        # Recalculate entry credit after adjustment
        short_ce_ltp = self.ctx.get_ltp(self._short_ce_token)
        short_pe_ltp = self.ctx.get_ltp(self._short_pe_token)
        long_ce_ltp = self.ctx.get_ltp(self._long_ce_token)
        long_pe_ltp = self.ctx.get_ltp(self._long_pe_token)
        self._entry_credit = (short_ce_ltp + short_pe_ltp) - (long_ce_ltp + long_pe_ltp)

        self._last_adjustment_time = self.ctx.clock.now().timestamp()
        self._adjustments_today += 1
        logger.info(
            f"[{self.strategy_id}] ADJUSTED {side} side "
            f"({self._adjustments_today}/{self.MAX_ADJUSTMENTS_PER_DAY} today): "
            f"new short_CE@{self._short_ce_strike} short_PE@{self._short_pe_strike} "
            f"long_CE@{self._long_ce_strike} long_PE@{self._long_pe_strike}"
        )

        return adjust_signal(
            self.strategy_id, all_legs,
            f"Adjust {side} side: re-entered at new strikes"
        )

    def _create_exit_signal(self, reason: str) -> Signal:
        """Create signal to close all 4 legs."""
        short_ce_ltp = self.ctx.get_ltp(self._short_ce_token)
        short_pe_ltp = self.ctx.get_ltp(self._short_pe_token)
        long_ce_ltp = self.ctx.get_ltp(self._long_ce_token)
        long_pe_ltp = self.ctx.get_ltp(self._long_pe_token)
        exit_debit = (short_ce_ltp + short_pe_ltp) - (long_ce_ltp + long_pe_ltp)
        pnl_estimate = self._entry_credit - exit_debit
        logger.info(
            f"[EXIT] strategy={self.strategy_id} reason={reason} "
            f"entry_credit={self._entry_credit} exit_debit={exit_debit} "
            f"estimated_pnl={pnl_estimate} qty={self._quantity}"
        )
        self._entered = False
        self._stopped_for_day = True
        legs = [
            # Buy back short legs
            make_leg(self._short_ce_symbol, self._short_ce_token, OrderSide.BUY, self._quantity),
            make_leg(self._short_pe_symbol, self._short_pe_token, OrderSide.BUY, self._quantity),
            # Sell long legs
            make_leg(self._long_ce_symbol, self._long_ce_token, OrderSide.SELL, self._quantity),
            make_leg(self._long_pe_symbol, self._long_pe_token, OrderSide.SELL, self._quantity),
        ]
        return exit_signal(self.strategy_id, legs, reason)

    async def on_stop(self) -> None:
        if self._entered:
            logger.info(f"[{self.strategy_id}] Stopping with open position")

    def reset_day_state(self) -> None:
        """Reset intraday flags at start of new trading day."""
        self._entered = False
        self._stopped_for_day = False
        self._adjustments_today = 0

    def get_state_data(self) -> dict:
        return {
            "entered": self._entered,
            "short_ce_token": self._short_ce_token,
            "short_pe_token": self._short_pe_token,
            "short_ce_symbol": self._short_ce_symbol,
            "short_pe_symbol": self._short_pe_symbol,
            "short_ce_strike": self._short_ce_strike,
            "short_pe_strike": self._short_pe_strike,
            "long_ce_token": self._long_ce_token,
            "long_pe_token": self._long_pe_token,
            "long_ce_symbol": self._long_ce_symbol,
            "long_pe_symbol": self._long_pe_symbol,
            "long_ce_strike": self._long_ce_strike,
            "long_pe_strike": self._long_pe_strike,
            "entry_credit": str(self._entry_credit),
            "stopped_for_day": self._stopped_for_day,
            "adjustments_today": self._adjustments_today,
        }

    def load_state_data(self, data: dict) -> None:
        self._entered = data.get("entered", False)
        self._stopped_for_day = data.get("stopped_for_day", False)
        self._short_ce_token = data.get("short_ce_token", 0)
        self._short_pe_token = data.get("short_pe_token", 0)
        self._short_ce_symbol = data.get("short_ce_symbol", "")
        self._short_pe_symbol = data.get("short_pe_symbol", "")
        self._short_ce_strike = data.get("short_ce_strike", 0)
        self._short_pe_strike = data.get("short_pe_strike", 0)
        self._long_ce_token = data.get("long_ce_token", 0)
        self._long_pe_token = data.get("long_pe_token", 0)
        self._long_ce_symbol = data.get("long_ce_symbol", "")
        self._long_pe_symbol = data.get("long_pe_symbol", "")
        self._long_ce_strike = data.get("long_ce_strike", 0)
        self._long_pe_strike = data.get("long_pe_strike", 0)
        self._entry_credit = Decimal(data.get("entry_credit", "0"))
        self._adjustments_today = data.get("adjustments_today", 0)
