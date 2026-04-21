"""Short Strangle strategy — sells OTM CE + OTM PE based on delta."""

import logging
import os
import time as _time
from datetime import date, time
from decimal import Decimal

from src.core.constants import LOT_SIZES
from src.core.models import Signal, SignalLeg, Subscription, Tick
from src.core.types import OrderSide, SignalType
from src.strategy.base import BaseStrategy
from src.strategy.params import ShortStrangleParams
from src.strategy.regime import RegimeDetector
from src.strategy.registry import register_strategy
from src.strategy.scoring import SHORT_STRANGLE_CONFIG, score_strategy
from src.strategy.signals import adjust_signal, entry_signal, exit_signal, make_leg

logger = logging.getLogger(__name__)


@register_strategy("short_strangle", ShortStrangleParams)
class ShortStrangleStrategy(BaseStrategy):
    """Sells OTM Call + OTM Put selected by delta.

    Unlike fixed-strike strangle, this selects strikes based on
    option delta for consistent risk profile across market conditions.
    """

    params: ShortStrangleParams

    MAX_ADJUSTMENTS_PER_DAY = 2

    def __init__(self, strategy_id: str, params: ShortStrangleParams):
        super().__init__(strategy_id, params)
        self._entered = False
        self._stopped_for_day = False
        self._regime: RegimeDetector | None = None
        self._paper_mode: bool = os.environ.get("PAPER_TRADING", "false").lower() == "true"
        self._ce_token: int = 0
        self._pe_token: int = 0
        self._ce_symbol: str = ""
        self._pe_symbol: str = ""
        self._ce_strike: float = 0
        self._pe_strike: float = 0
        self._entry_premium: Decimal = Decimal("0")
        self._peak_premium: Decimal = Decimal("0")  # For trailing stop
        self._expiry: date | None = None
        self._lot_size = LOT_SIZES.get(params.underlying, 75)
        self._quantity = params.quantity_lots * self._lot_size
        self._adjustments_today: int = 0
        self._last_adjustment_date: date | None = None
        self._last_adjustment_time: float = 0.0  # Unix timestamp cooldown

    def get_subscriptions(self) -> Subscription:
        return Subscription(instrument_tokens=[], timeframes=[])

    async def on_start(self) -> None:
        self._expiry = self.ctx.next_expiry(self.params.underlying)
        self._regime = RegimeDetector(self.ctx._feed, self.ctx._aggregator, self.ctx._chain_builder)
        logger.info(
            f"[{self.strategy_id}] Started: {self.params.underlying} "
            f"expiry={self._expiry} call_delta={self.params.call_delta} "
            f"put_delta={self.params.put_delta}"
        )

    async def on_tick(self, tick: Tick) -> Signal | None:
        now = self.ctx.clock.now()

        # Expiry rollover
        new_expiry = self._check_expiry_rollover(self._expiry, self.params.underlying)
        if new_expiry:
            self._expiry = new_expiry

        if now.time() >= self.params.exit_time and self._entered:
            return self._create_exit_signal("Exit time reached")

        if not self._entered and not self._stopped_for_day and now.time() >= self.params.entry_time:
            return await self._try_entry()

        if self._entered:
            return self._check_adjustments()

        return None

    async def _try_entry(self) -> Signal | None:
        """Select strikes by delta and enter."""
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
            SHORT_STRANGLE_CONFIG, vix, morning_range_pct, move_from_open_pct,
            pcr_oi, is_expiry_day, dte,
        )
        reasons_str = ", ".join(reasons)
        logger.info(
            f"[SIGNAL_SCORE] strategy={self.strategy_id} score={score}/100 [{reasons_str}]"
        )
        # All Entry-skipped / SHADOW_BLOCK lines below are routed through
        # _log_skip_throttled so a sustained block (e.g. 0DTE expiry day)
        # emits one line/min/reason instead of one line/tick. Apr 21
        # produced thousands of identical "Entry skipped" lines per
        # strategy. The dedup key partitions reasons so a state flip
        # (e.g. VIX moves out of band → score recovers) surfaces on the
        # next tick.
        if score < 60:
            if self._paper_mode:
                self._log_skip_throttled(
                    "SHADOW_BLOCK_SCORE",
                    f"[SHADOW_BLOCK] strategy={self.strategy_id} score={score}/100 "
                    f"< 60 — proceeding anyway (paper mode)",
                )
            else:
                self._log_skip_throttled(
                    "ENTRY_SKIP_SCORE",
                    f"[{self.strategy_id}] Entry skipped: signal score {score}/100 < 60",
                )
                return None

        # Expiry-day 0DTE block — never enter naked premium when today == expiry
        expiry_block = self._check_expiry_day_block(self.params.underlying)
        if expiry_block:
            self._log_skip_throttled(
                "ENTRY_SKIP_EXPIRY",
                f"[{self.strategy_id}] Entry skipped: {expiry_block}",
            )
            return None

        # VIX filter — skip entry in high-volatility environments
        vix_block = self._check_vix_filter()
        if vix_block:
            if self._paper_mode:
                self._log_skip_throttled(
                    "SHADOW_BLOCK_VIX",
                    f"[{self.strategy_id}] [SHADOW_BLOCK] {vix_block}",
                )
            else:
                self._log_skip_throttled(
                    "ENTRY_SKIP_VIX",
                    f"[{self.strategy_id}] Entry skipped: {vix_block}",
                )
                return None

        # Trend filter — skip if market is trending >0.7% from open
        trend_block = self._check_trend_filter(self.params.underlying)
        if trend_block:
            if self._paper_mode:
                self._log_skip_throttled(
                    "SHADOW_BLOCK_TREND",
                    f"[{self.strategy_id}] [SHADOW_BLOCK] {trend_block}",
                )
            else:
                self._log_skip_throttled(
                    "ENTRY_SKIP_TREND",
                    f"[{self.strategy_id}] Entry skipped: {trend_block}",
                )
                return None

        # PCR filter
        pcr_block = self._check_pcr_filter(self.params.underlying, self._expiry)
        if pcr_block:
            if self._paper_mode:
                self._log_skip_throttled(
                    "SHADOW_BLOCK_PCR",
                    f"[{self.strategy_id}] [SHADOW_BLOCK] {pcr_block}",
                )
            else:
                self._log_skip_throttled(
                    "ENTRY_SKIP_PCR",
                    f"[{self.strategy_id}] Entry skipped: {pcr_block}",
                )
                return None

        # Max pain filter
        mp_block = self._check_max_pain_filter(self.params.underlying, self._expiry)
        if mp_block:
            if self._paper_mode:
                self._log_skip_throttled(
                    "SHADOW_BLOCK_MP",
                    f"[{self.strategy_id}] [SHADOW_BLOCK] {mp_block}",
                )
            else:
                self._log_skip_throttled(
                    "ENTRY_SKIP_MP",
                    f"[{self.strategy_id}] Entry skipped: {mp_block}",
                )
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

        # Find CE strike closest to target delta
        best_ce = None
        best_ce_diff = float("inf")
        best_pe = None
        best_pe_diff = float("inf")

        for entry in chain.strikes:
            if entry.ce and entry.ce.greeks.delta > 0:
                diff = abs(entry.ce.greeks.delta - self.params.call_delta)
                if diff < best_ce_diff:
                    best_ce_diff = diff
                    best_ce = entry

            if entry.pe and entry.pe.greeks.delta < 0:
                diff = abs(entry.pe.greeks.delta - self.params.put_delta)
                if diff < best_pe_diff:
                    best_pe_diff = diff
                    best_pe = entry

        if not best_ce or not best_ce.ce or not best_pe or not best_pe.pe:
            logger.warning(f"[{self.strategy_id}] Could not find suitable strikes")
            return None

        self._ce_token = best_ce.ce.instrument_token
        self._ce_symbol = best_ce.ce.tradingsymbol
        self._ce_strike = float(best_ce.strike)
        self._pe_token = best_pe.pe.instrument_token
        self._pe_symbol = best_pe.pe.tradingsymbol
        self._pe_strike = float(best_pe.strike)

        ce_ltp = self.ctx.get_ltp(self._ce_token)
        pe_ltp = self.ctx.get_ltp(self._pe_token)
        self._entry_premium = ce_ltp + pe_ltp
        self._peak_premium = self._entry_premium

        legs = [
            make_leg(self._ce_symbol, self._ce_token, OrderSide.SELL, self._quantity),
            make_leg(self._pe_symbol, self._pe_token, OrderSide.SELL, self._quantity),
        ]

        self._entered = True
        ce_delta = best_ce.ce.greeks.delta if best_ce.ce else 0
        pe_delta = best_pe.pe.greeks.delta if best_pe.pe else 0
        ce_iv = best_ce.ce.greeks.iv if best_ce.ce else 0
        pe_iv = best_pe.pe.greeks.iv if best_pe.pe else 0
        logger.info(
            f"[ENTRY] strategy={self.strategy_id} type=short_strangle "
            f"ce_strike={self._ce_strike} ce_premium={ce_ltp} "
            f"ce_delta={ce_delta:.3f} ce_iv={ce_iv:.1f} "
            f"pe_strike={self._pe_strike} pe_premium={pe_ltp} "
            f"pe_delta={pe_delta:.3f} pe_iv={pe_iv:.1f} "
            f"total_premium={self._entry_premium} qty={self._quantity}"
        )
        self._log_decision(
            "ENTER",
            leg="PREMIUM",
            mode="strangle",
            rule_score=score,
            threshold=60,
            entry_premium=float(self._entry_premium),
            quantity=self._quantity,
        )

        return entry_signal(
            self.strategy_id, legs,
            f"Strangle CE@{self._ce_strike} PE@{self._pe_strike}"
        )

    def _check_adjustments(self) -> Signal | None:
        """Check if position needs adjustment based on delta movement."""
        if self._entry_premium <= 0:
            return None

        ce_ltp = self.ctx.get_ltp(self._ce_token)
        pe_ltp = self.ctx.get_ltp(self._pe_token)
        current_premium = ce_ltp + pe_ltp

        change_pct = float((current_premium - self._entry_premium) / self._entry_premium * 100)

        # Profit target — exit when premium has decayed enough
        if self.params.profit_target_pct > 0:
            decay_pct = float((self._entry_premium - current_premium) / self._entry_premium * 100)
            if decay_pct >= self.params.profit_target_pct:
                logger.info(
                    f"[{self.strategy_id}] PROFIT TARGET: premium decayed {decay_pct:.1f}% "
                    f"(target: {self.params.profit_target_pct}%)"
                )
                self._stopped_for_day = True
                return self._create_exit_signal(f"Profit target: premium decayed {decay_pct:.1f}%")

        # Stop loss check
        if change_pct > self.params.stop_loss_pct:
            logger.info(
                f"[{self.strategy_id}] STOP LOSS: premium up {change_pct:.1f}% "
                f"(threshold: {self.params.stop_loss_pct}%)"
            )
            self._stopped_for_day = True
            return self._create_exit_signal(f"Stop loss: +{change_pct:.1f}%")

        # Trailing stop — lock in profits as premium decays
        # Tighten trail stop after 2pm (gamma risk increases near close)
        trail_pct = self.params.trail_stop_pct
        if trail_pct > 0:
            now_time = self.ctx.clock.now().time()
            if now_time >= time(14, 0):
                trail_pct = trail_pct * 0.6  # 40% tighter after 2pm

        if trail_pct > 0:
            if current_premium < self._peak_premium:
                self._peak_premium = min(self._peak_premium, current_premium)
            elif current_premium > self._peak_premium:
                decay_pct = float(
                    (self._entry_premium - self._peak_premium) / self._entry_premium * 100
                )
                if not self._can_activate_trail_stop(decay_pct):
                    return None
                bounce_pct = float(
                    (current_premium - self._peak_premium) / self._entry_premium * 100
                )
                if bounce_pct > trail_pct:
                    logger.info(
                        f"[{self.strategy_id}] TRAIL STOP: bounced {bounce_pct:.1f}%, "
                        f"locking {decay_pct:.1f}% profit"
                    )
                    self._stopped_for_day = True
                    return self._create_exit_signal(
                        f"Trailing stop: bounced {bounce_pct:.1f}%"
                    )

        # Reset adjustment counter on new day
        today = self.ctx.clock.now().date()
        if self._last_adjustment_date != today:
            self._adjustments_today = 0
            self._last_adjustment_date = today

        # Cap adjustments per day to avoid charge bleed
        if self._adjustments_today >= self.MAX_ADJUSTMENTS_PER_DAY:
            return None

        # Cooldown: don't generate adjustment signals more than once per 30s
        if self.ctx.clock.now().timestamp() - self._last_adjustment_time < 30 * 60:
            return None

        # Delta-based adjustment — roll the losing leg to new delta target
        chain = self.ctx.get_option_chain(self.params.underlying, self._expiry)
        if not chain:
            return None

        for entry in chain.strikes:
            if entry.ce and entry.ce.instrument_token == self._ce_token:
                if abs(entry.ce.greeks.delta) > self.params.adjustment_delta_threshold:
                    return self._roll_leg("CE", chain, ce_ltp)
                break

        for entry in chain.strikes:
            if entry.pe and entry.pe.instrument_token == self._pe_token:
                if abs(entry.pe.greeks.delta) > self.params.adjustment_delta_threshold:
                    return self._roll_leg("PE", chain, pe_ltp)
                break

        return None

    def _roll_leg(self, leg_type: str, chain, current_ltp: Decimal) -> Signal | None:
        """Roll a leg back to target delta strike."""
        legs: list[SignalLeg] = []

        if leg_type == "CE":
            # Find new CE at target delta
            best = None
            best_diff = float("inf")
            for entry in chain.strikes:
                if entry.ce and entry.ce.greeks.delta > 0:
                    diff = abs(entry.ce.greeks.delta - self.params.call_delta)
                    if diff < best_diff:
                        best_diff = diff
                        best = entry

            if not best or not best.ce:
                logger.warning(f"[{self.strategy_id}] Could not find new CE for roll")
                return None

            # Skip if rolling to same strike (no-op)
            if best.ce.instrument_token == self._ce_token:
                logger.debug(f"[{self.strategy_id}] CE roll skipped: same strike")
                return None

            # Close existing CE
            legs.append(make_leg(self._ce_symbol, self._ce_token, OrderSide.BUY, self._quantity))

            old_strike = self._ce_strike
            self._ce_token = best.ce.instrument_token
            self._ce_symbol = best.ce.tradingsymbol
            self._ce_strike = float(best.strike)
            legs.append(make_leg(self._ce_symbol, self._ce_token, OrderSide.SELL, self._quantity))

            logger.info(
                f"[{self.strategy_id}] ROLL CE: {old_strike} -> {self._ce_strike} "
                f"(delta was {self.params.adjustment_delta_threshold}+)"
            )
        else:
            # Find new PE at target delta
            best = None
            best_diff = float("inf")
            for entry in chain.strikes:
                if entry.pe and entry.pe.greeks.delta < 0:
                    diff = abs(entry.pe.greeks.delta - self.params.put_delta)
                    if diff < best_diff:
                        best_diff = diff
                        best = entry

            if not best or not best.pe:
                logger.warning(f"[{self.strategy_id}] Could not find new PE for roll")
                return None

            # Skip if rolling to same strike (no-op)
            if best.pe.instrument_token == self._pe_token:
                logger.debug(f"[{self.strategy_id}] PE roll skipped: same strike")
                return None

            # Close existing PE
            legs.append(make_leg(self._pe_symbol, self._pe_token, OrderSide.BUY, self._quantity))

            old_strike = self._pe_strike
            self._pe_token = best.pe.instrument_token
            self._pe_symbol = best.pe.tradingsymbol
            self._pe_strike = float(best.strike)
            legs.append(make_leg(self._pe_symbol, self._pe_token, OrderSide.SELL, self._quantity))

            logger.info(
                f"[{self.strategy_id}] ROLL PE: {old_strike} -> {self._pe_strike} "
                f"(delta was {self.params.adjustment_delta_threshold}+)"
            )

        # Re-record entry premium after roll
        self._entry_premium = self.ctx.get_ltp(self._ce_token) + self.ctx.get_ltp(self._pe_token)
        self._peak_premium = self._entry_premium
        self._adjustments_today += 1
        self._last_adjustment_time = self.ctx.clock.now().timestamp()

        logger.info(
            f"[{self.strategy_id}] Adjustment {self._adjustments_today}/{self.MAX_ADJUSTMENTS_PER_DAY} today"
        )

        return adjust_signal(
            self.strategy_id, legs,
            f"Rolled {leg_type} to delta target"
        )

    def _create_exit_signal(self, reason: str) -> Signal:
        ce_ltp = self.ctx.get_ltp(self._ce_token)
        pe_ltp = self.ctx.get_ltp(self._pe_token)
        exit_premium = ce_ltp + pe_ltp
        pnl_estimate = self._entry_premium - exit_premium
        logger.info(
            f"[EXIT] strategy={self.strategy_id} reason={reason} "
            f"entry_premium={self._entry_premium} exit_premium={exit_premium} "
            f"estimated_pnl={pnl_estimate} qty={self._quantity}"
        )
        # Premium short: P&L per lot = entry - exit, scaled by qty.
        outcome_pnl = float(pnl_estimate) * self._quantity
        self._log_decision(
            "EXIT",
            leg="PREMIUM",
            mode="strangle",
            entry_premium=float(self._entry_premium),
            quantity=self._quantity,
            exit_reason=reason,
            outcome_pnl=outcome_pnl,
        )
        legs = [
            make_leg(self._ce_symbol, self._ce_token, OrderSide.BUY, self._quantity),
            make_leg(self._pe_symbol, self._pe_token, OrderSide.BUY, self._quantity),
        ]
        self._entered = False
        self._stopped_for_day = True
        return exit_signal(self.strategy_id, legs, reason)

    async def on_stop(self) -> None:
        if self._entered:
            logger.info(f"[{self.strategy_id}] Stopping with open position")

    def get_state_data(self) -> dict:
        return {
            "entered": self._entered,
            "stopped_for_day": self._stopped_for_day,
            "ce_token": self._ce_token,
            "pe_token": self._pe_token,
            "ce_symbol": self._ce_symbol,
            "pe_symbol": self._pe_symbol,
            "ce_strike": self._ce_strike,
            "pe_strike": self._pe_strike,
            "entry_premium": str(self._entry_premium),
            "peak_premium": str(self._peak_premium),
            "adjustments_today": self._adjustments_today,
            "last_adjustment_date": self._last_adjustment_date.isoformat() if self._last_adjustment_date else None,
        }

    def load_state_data(self, data: dict) -> None:
        self._entered = data.get("entered", False)
        self._stopped_for_day = data.get("stopped_for_day", False)
        self._ce_token = data.get("ce_token", 0)
        self._pe_token = data.get("pe_token", 0)
        self._ce_symbol = data.get("ce_symbol", "")
        self._pe_symbol = data.get("pe_symbol", "")
        self._ce_strike = data.get("ce_strike", 0)
        self._pe_strike = data.get("pe_strike", 0)
        self._entry_premium = Decimal(data.get("entry_premium", "0"))
        self._peak_premium = Decimal(data.get("peak_premium", "0"))
        self._adjustments_today = data.get("adjustments_today", 0)
        adj_date = data.get("last_adjustment_date")
        self._last_adjustment_date = date.fromisoformat(adj_date) if adj_date else None
