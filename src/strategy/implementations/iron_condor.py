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
from src.core.types import OrderSide, OrderType
from src.strategy.base import BaseStrategy
from src.strategy.implementations.portfolio_pricing import find_available_wing_strike
from src.strategy.params import IronCondorParams
from src.strategy.regime import RegimeDetector
from src.strategy.registry import register_strategy
from src.strategy.scoring import IRON_CONDOR_CONFIG, score_strategy
from src.strategy.signals import adjust_signal, entry_signal, exit_signal

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
        # Apr 29 2026 Phase 1C: per-leg entry fill prices (₹/share). The
        # strategy SOLD short legs at bid and BOUGHT long legs at ask;
        # ``_entry_credit`` aggregates these for the full 4-leg position.
        # Per-leg fills are needed at adjustment time to attribute the
        # closed-side's realised P&L against its OWN entry basis (rather
        # than against the rebased post-roll _entry_credit which reflects
        # the *new* legs). After a roll, only the rolled side's fills are
        # updated; the un-touched side keeps its original entry basis.
        self._short_ce_entry_fill: float = 0.0
        self._short_pe_entry_fill: float = 0.0
        self._long_ce_entry_fill: float = 0.0
        self._long_pe_entry_fill: float = 0.0
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

        # Capture morning-open VIX for Gate B (idempotent; only on first call
        # after 9:15 IST per day). Done early so the captured value reflects
        # session open, not entry_time (default 9:20).
        self._capture_morning_vix_if_needed()

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

    def _compute_score(self) -> tuple[int, list[str]]:
        """Pure scorer — no state mutation. Used by both _try_entry
        (decide whether to actually enter) and evaluate_score (orchestrator
        comparison). Returns (score, reasons).
        """
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
        return int(score), reasons

    def evaluate_score(self) -> int:
        try:
            score, _ = self._compute_score()
            return score
        except Exception:
            return 0

    # ─── Realistic-fill helpers (Apr 29 2026, post chain-gap diagnostic) ──
    # The strategy's prior PnL bookkeeping used LTP for both entry credit
    # and exit debit. That's a midpoint estimate the broker doesn't
    # actually honour: short legs SELL at bid, long legs BUY at ask, and
    # exits flip those. On gdfl_v2's wider chain the LTP-vs-fill gap was
    # ~₹723/fill, dwarfing every other variable.
    #
    # Generic ``_bid_ask_for`` / ``_spread_pct`` were promoted to
    # BaseStrategy on Apr 29 (multi-model audit follow-up). The IC keeps
    # ``_entry_fill_credit`` / ``_exit_fill_debit`` here because the leg
    # structure (short CE + short PE + long CE wing + long PE wing) is
    # IC-specific; strangle/straddle/calendar implement their own.

    def _entry_fill_credit(self) -> float:
        """Net credit the broker would actually book at entry.

        Short legs fill at the bid (sell-side cross), long-wing legs at
        the ask (buy-side cross). The realised credit is therefore
        (sum of short bids) − (sum of long asks), which is strictly less
        than or equal to the LTP-based number the strategy used to log.
        """
        short_ce_bid, _ = self._bid_ask_for(self._short_ce_token)
        short_pe_bid, _ = self._bid_ask_for(self._short_pe_token)
        _, long_ce_ask = self._bid_ask_for(self._long_ce_token)
        _, long_pe_ask = self._bid_ask_for(self._long_pe_token)
        return (short_ce_bid + short_pe_bid) - (long_ce_ask + long_pe_ask)

    def _exit_fill_debit(self) -> float:
        """Net debit the broker would actually book at exit.

        Mirror of entry: short legs now BUY at ask (close at the offer),
        long legs now SELL at bid. The realised debit is therefore
        (sum of short asks) − (sum of long bids), which is strictly
        greater than or equal to the LTP-based number.
        """
        _, short_ce_ask = self._bid_ask_for(self._short_ce_token)
        _, short_pe_ask = self._bid_ask_for(self._short_pe_token)
        long_ce_bid, _ = self._bid_ask_for(self._long_ce_token)
        long_pe_bid, _ = self._bid_ask_for(self._long_pe_token)
        return (short_ce_ask + short_pe_ask) - (long_ce_bid + long_pe_bid)

    def _check_strike_liquidity(self, opt, leg_label: str) -> str | None:
        """Reject a candidate strike whose bid-ask spread exceeds
        ``params.max_spread_pct``. Returns ``None`` if liquid, otherwise
        a human-readable reason for the entry-skip log.

        ``params.max_spread_pct == 0`` disables the filter (kept for
        bisection / regression-test use)."""
        if self.params.max_spread_pct <= 0:
            return None
        if opt is None:
            return f"{leg_label}: missing chain entry"
        bid = float(opt.bid_price or 0)
        ask = float(opt.ask_price or 0)
        spread_pct = self._spread_pct(bid, ask)
        if spread_pct is None:
            return f"{leg_label}: bid/ask invalid (bid={bid}, ask={ask})"
        if spread_pct > self.params.max_spread_pct:
            return (
                f"{leg_label} {opt.tradingsymbol}: spread "
                f"{spread_pct:.1f}% > max {self.params.max_spread_pct}% "
                f"(bid={bid}, ask={ask})"
            )
        return None

    async def _try_entry(self) -> Signal | None:
        """Select strikes by delta and enter the iron condor."""
        # --- Signal scoring ---
        score, reasons = self._compute_score()
        reasons_str = ", ".join(reasons)
        logger.info(
            f"[SIGNAL_SCORE] strategy={self.strategy_id} score={score}/100 [{reasons_str}]"
        )
        # Per-tick → per-minute throttling for all entry-skip logs. See
        # _log_skip_throttled docstring on BaseStrategy for the 19,646-line/
        # 23-min Apr 21 audit-flood that motivated this.
        if score < 60:
            self._log_skip_throttled(
                "ENTRY_SKIP_SCORE",
                f"[{self.strategy_id}] Entry skipped: signal score {score}/100 < 60",
            )
            return None

        # Expiry-day 0DTE block — wings go illiquid + STT trap if ITM at close
        expiry_block = self._check_expiry_day_block(self.params.underlying)
        if expiry_block:
            self._log_skip_throttled(
                "ENTRY_SKIP_EXPIRY",
                f"[{self.strategy_id}] Entry skipped: {expiry_block}",
            )
            return None

        # VIX filter
        vix_block = self._check_vix_filter()
        if vix_block:
            self._log_skip_throttled(
                "ENTRY_SKIP_VIX",
                f"[{self.strategy_id}] Entry skipped: {vix_block}",
            )
            return None

        # Phase 3b Gate B — intraday VIX spike filter (PRE-REGISTERED, opt-in
        # via params.intraday_vix_spike_enabled). When enabled, blocks new IC
        # entries after activate_after time if VIX has risen >= threshold% from
        # morning open. Designed for the May 8 2025 spike pattern.
        spike_block = self._check_intraday_vix_spike_filter()
        if spike_block:
            self._log_skip_throttled(
                "ENTRY_SKIP_VIX_SPIKE",
                f"[{self.strategy_id}] Entry skipped: {spike_block}",
            )
            return None

        # PCR filter
        pcr_block = self._check_pcr_filter(self.params.underlying, self._expiry)
        if pcr_block:
            self._log_skip_throttled(
                "ENTRY_SKIP_PCR",
                f"[{self.strategy_id}] Entry skipped: {pcr_block}",
            )
            return None

        # Max pain filter
        mp_block = self._check_max_pain_filter(self.params.underlying, self._expiry)
        if mp_block:
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

        # Determine long (wing) strikes — with inward clamping (Apr 2026 fix).
        # 8-strike wings (400pts on NIFTY) often land outside the recorded
        # chain's strike range, blocking entry entirely. Clamping inward gives
        # us a narrower-than-target IC (smaller defined max loss) instead of
        # no IC at all.
        desired_wing_offset = self.params.wing_width_strikes * strike_interval
        self._short_ce_strike = float(best_short_ce.strike)
        self._short_pe_strike = float(best_short_pe.strike)

        long_ce_entry, ce_wing_offset = find_available_wing_strike(
            chain, self._short_ce_strike, desired_wing_offset, direction=+1,
            opt_attr="ce", strike_step=int(strike_interval),
        )
        long_pe_entry, pe_wing_offset = find_available_wing_strike(
            chain, self._short_pe_strike, desired_wing_offset, direction=-1,
            opt_attr="pe", strike_step=int(strike_interval),
        )

        if not long_ce_entry or not long_pe_entry:
            logger.warning(
                f"[{self.strategy_id}] Could not find any wing strikes within {desired_wing_offset}pts "
                f"(short_ce@{self._short_ce_strike} ce_found={long_ce_entry is not None}, "
                f"short_pe@{self._short_pe_strike} pe_found={long_pe_entry is not None})"
            )
            return None

        self._long_ce_strike = float(long_ce_entry.strike)
        self._long_pe_strike = float(long_pe_entry.strike)

        if ce_wing_offset != desired_wing_offset or pe_wing_offset != desired_wing_offset:
            logger.info(
                f"[{self.strategy_id}] IC WING CLAMPED: desired={desired_wing_offset}pts "
                f"actual CE={ce_wing_offset}pts PE={pe_wing_offset}pts"
            )

        # ── Liquidity gate (Apr 29 2026) — reject any leg whose bid-ask
        # spread exceeds params.max_spread_pct of mid. The chain-gap
        # diagnostic showed this was the dominant edge-killer on
        # gdfl_v2-shaped chains where deep wings have wide markets.
        liquidity_blocks: list[str] = []
        for opt, label in (
            (best_short_ce.ce, "short_ce"),
            (best_short_pe.pe, "short_pe"),
            (long_ce_entry.ce, "long_ce"),
            (long_pe_entry.pe, "long_pe"),
        ):
            block = self._check_strike_liquidity(opt, label)
            if block:
                liquidity_blocks.append(block)
        if liquidity_blocks:
            self._log_skip_throttled(
                "ENTRY_SKIP_ILLIQUID",
                f"[{self.strategy_id}] Entry skipped — illiquid leg(s): "
                + "; ".join(liquidity_blocks),
            )
            return None

        # Find all 4 legs in the chain
        self._short_ce_token = best_short_ce.ce.instrument_token
        self._short_ce_symbol = best_short_ce.ce.tradingsymbol
        self._short_pe_token = best_short_pe.pe.instrument_token
        self._short_pe_symbol = best_short_pe.pe.tradingsymbol
        self._long_ce_token = long_ce_entry.ce.instrument_token
        self._long_ce_symbol = long_ce_entry.ce.tradingsymbol
        self._long_pe_token = long_pe_entry.pe.instrument_token
        self._long_pe_symbol = long_pe_entry.pe.tradingsymbol

        # Calculate net credit using REALISTIC fills (bid/ask), not LTP.
        # See _entry_fill_credit docstring + Apr 29 chain-gap diagnostic.
        # Cache LTPs for the human-readable [ENTRY] log line below; they
        # are NOT used for any P&L bookkeeping.
        short_ce_ltp = self.ctx.get_ltp(self._short_ce_token)
        short_pe_ltp = self.ctx.get_ltp(self._short_pe_token)
        long_ce_ltp = self.ctx.get_ltp(self._long_ce_token)
        long_pe_ltp = self.ctx.get_ltp(self._long_pe_token)
        self._entry_credit = Decimal(str(round(self._entry_fill_credit(), 2)))
        # Apr 29 2026 Phase 1C: snapshot per-leg entry fill prices so a
        # later adjustment can attribute its closed-side realised P&L
        # against the right basis. Shorts fill at bid, longs at ask.
        self._short_ce_entry_fill, _ = self._bid_ask_for(self._short_ce_token)
        self._short_pe_entry_fill, _ = self._bid_ask_for(self._short_pe_token)
        _, self._long_ce_entry_fill = self._bid_ask_for(self._long_ce_token)
        _, self._long_pe_entry_fill = self._bid_ask_for(self._long_pe_token)

        # F1: LIMIT-at-mid. Every leg must price — a missing wing turns the
        # IC into a naked short.
        short_ce_leg = self._build_option_leg(
            self._short_ce_symbol, self._short_ce_token, OrderSide.SELL, self._quantity, opt=best_short_ce.ce,
        )
        short_pe_leg = self._build_option_leg(
            self._short_pe_symbol, self._short_pe_token, OrderSide.SELL, self._quantity, opt=best_short_pe.pe,
        )
        long_ce_leg = self._build_option_leg(
            self._long_ce_symbol, self._long_ce_token, OrderSide.BUY, self._quantity, opt=long_ce_entry.ce,
        )
        long_pe_leg = self._build_option_leg(
            self._long_pe_symbol, self._long_pe_token, OrderSide.BUY, self._quantity, opt=long_pe_entry.pe,
        )
        if any(leg is None for leg in (short_ce_leg, short_pe_leg, long_ce_leg, long_pe_leg)):
            logger.warning(f"[{self.strategy_id}] IC BLOCKED: could not price all 4 legs")
            return None
        legs = [
            # Short legs (sell near OTM)
            short_ce_leg,
            short_pe_leg,
            # Long legs (buy far OTM — wings)
            long_ce_leg,
            long_pe_leg,
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
        self._log_decision(
            "ENTER",
            leg="PREMIUM",
            mode="iron_condor",
            rule_score=score,
            threshold=60,
            entry_premium=float(self._entry_credit),
            quantity=self._quantity,
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

        # Resolve exit thresholds — vol-scaled when opt-in, hardcoded otherwise.
        dte = (self._expiry - self.ctx.clock.now().date()).days if self._expiry else 7
        pt_target_pct = self._compute_vol_scaled_exit_pct(
            "pt", dte, fallback_pct=self.params.profit_target_pct
        )
        sl_threshold_pct = self._compute_vol_scaled_exit_pct(
            "sl", dte, fallback_pct=self.params.stop_loss_pct
        )

        # Profit target — exit when spread value has decayed enough
        if pt_target_pct > 0:
            decay_pct = float((self._entry_credit - current_debit) / self._entry_credit * 100)
            if decay_pct >= pt_target_pct:
                logger.info(
                    f"[{self.strategy_id}] PROFIT TARGET: spread decayed {decay_pct:.1f}% "
                    f"(target: {pt_target_pct}%)"
                )
                self._stopped_for_day = True
                return self._create_exit_signal(f"Profit target: spread decayed {decay_pct:.1f}%")

        loss_pct = float((current_debit - self._entry_credit) / self._entry_credit * 100)

        # Stop loss check on entire position
        if loss_pct > sl_threshold_pct:
            logger.info(
                f"[{self.strategy_id}] STOP LOSS: position loss {loss_pct:.1f}% "
                f"(threshold: {sl_threshold_pct}%)"
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
            # F1: LIMIT-at-mid on close legs. Token-only lookups via
            # _build_option_leg fall back to the feed's latest tick's bid/ask.
            # Apr 29 2026 Phase 1C: snapshot the close-side fills BEFORE
            # building the close legs so we can attribute the rolled-side's
            # realised P&L to the ADJUST decision row. BUY-to-close pays
            # ask on the short, SELL-to-close receives bid on the long.
            _, close_short_ce_ask = self._bid_ask_for(self._short_ce_token)
            close_long_ce_bid, _ = self._bid_ask_for(self._long_ce_token)
            ce_side_close_debit = close_short_ce_ask - close_long_ce_bid
            ce_side_entry_credit = self._short_ce_entry_fill - self._long_ce_entry_fill
            ce_side_realized_pnl = (ce_side_entry_credit - ce_side_close_debit) * self._quantity

            close_old_short = self._build_option_leg(
                self._short_ce_symbol, self._short_ce_token, OrderSide.BUY, self._quantity,
            )
            close_old_wing = self._build_option_leg(
                self._long_ce_symbol, self._long_ce_token, OrderSide.SELL, self._quantity,
            )
            if close_old_short is None or close_old_wing is None:
                logger.warning(
                    f"[{self.strategy_id}] IC CALL ADJUST BLOCKED: could not price close legs"
                )
                return None
            close_legs.append(close_old_short)
            close_legs.append(close_old_wing)

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
            open_short_leg = self._build_option_leg(
                self._short_ce_symbol, self._short_ce_token, OrderSide.SELL, self._quantity, opt=best_ce.ce,
            )

            self._long_ce_token = wing_entry.ce.instrument_token
            self._long_ce_symbol = wing_entry.ce.tradingsymbol
            self._long_ce_strike = new_long_strike
            open_wing_leg = self._build_option_leg(
                self._long_ce_symbol, self._long_ce_token, OrderSide.BUY, self._quantity, opt=wing_entry.ce,
            )
            if open_short_leg is None or open_wing_leg is None:
                logger.warning(
                    f"[{self.strategy_id}] IC CALL ADJUST BLOCKED: could not price new legs — rolling back"
                )
                # Restore old state
                self._short_ce_token, self._short_ce_symbol, self._short_ce_strike = old_short_ce_token, old_short_ce_symbol, old_short_ce_strike
                self._long_ce_token, self._long_ce_symbol, self._long_ce_strike = old_long_ce_token, old_long_ce_symbol, old_long_ce_strike
                return None
            open_legs.append(open_short_leg)
            open_legs.append(open_wing_leg)

        elif side == "put":
            # F1: LIMIT-at-mid on close legs.
            # Apr 29 2026 Phase 1C: snapshot close-side fills before
            # building legs so the closed-side realised P&L is captured
            # in the ADJUST decision row. Mirror of the call branch above.
            _, close_short_pe_ask = self._bid_ask_for(self._short_pe_token)
            close_long_pe_bid, _ = self._bid_ask_for(self._long_pe_token)
            pe_side_close_debit = close_short_pe_ask - close_long_pe_bid
            pe_side_entry_credit = self._short_pe_entry_fill - self._long_pe_entry_fill
            pe_side_realized_pnl = (pe_side_entry_credit - pe_side_close_debit) * self._quantity

            close_old_short = self._build_option_leg(
                self._short_pe_symbol, self._short_pe_token, OrderSide.BUY, self._quantity,
            )
            close_old_wing = self._build_option_leg(
                self._long_pe_symbol, self._long_pe_token, OrderSide.SELL, self._quantity,
            )
            if close_old_short is None or close_old_wing is None:
                logger.warning(
                    f"[{self.strategy_id}] IC PUT ADJUST BLOCKED: could not price close legs"
                )
                return None
            close_legs.append(close_old_short)
            close_legs.append(close_old_wing)

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
            open_short_leg = self._build_option_leg(
                self._short_pe_symbol, self._short_pe_token, OrderSide.SELL, self._quantity, opt=best_pe.pe,
            )

            self._long_pe_token = wing_entry.pe.instrument_token
            self._long_pe_symbol = wing_entry.pe.tradingsymbol
            self._long_pe_strike = new_long_strike
            open_wing_leg = self._build_option_leg(
                self._long_pe_symbol, self._long_pe_token, OrderSide.BUY, self._quantity, opt=wing_entry.pe,
            )
            if open_short_leg is None or open_wing_leg is None:
                logger.warning(
                    f"[{self.strategy_id}] IC PUT ADJUST BLOCKED: could not price new legs — rolling back"
                )
                self._short_pe_token, self._short_pe_symbol, self._short_pe_strike = old_short_pe_token, old_short_pe_symbol, old_short_pe_strike
                self._long_pe_token, self._long_pe_symbol, self._long_pe_strike = old_long_pe_token, old_long_pe_symbol, old_long_pe_strike
                return None
            open_legs.append(open_short_leg)
            open_legs.append(open_wing_leg)

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

        # Refresh per-leg entry fills for the rolled side BEFORE
        # recomputing the aggregate entry_credit. The un-rolled side
        # keeps its original entry basis (so a future EXIT or second
        # ADJUST attributes its P&L correctly).
        if side == "call":
            self._short_ce_entry_fill, _ = self._bid_ask_for(self._short_ce_token)
            _, self._long_ce_entry_fill = self._bid_ask_for(self._long_ce_token)
        else:
            self._short_pe_entry_fill, _ = self._bid_ask_for(self._short_pe_token)
            _, self._long_pe_entry_fill = self._bid_ask_for(self._long_pe_token)

        # Recalculate entry credit after adjustment using REALISTIC fills
        # (bid/ask), mirroring the initial-entry path at line ~414. The
        # pre-Apr-29 code re-read LTP here, silently re-introducing the
        # midpoint fiction the chain-gap diagnostic exposed. Audit-flagged
        # by 5/6 reviewers as the most-confident remaining bookkeeping bug
        # post f944986. See ``_entry_fill_credit`` docstring on this class.
        self._entry_credit = Decimal(str(round(self._entry_fill_credit(), 2)))

        # Apr 29 2026 Phase 1C: emit an ADJUST decision row capturing the
        # closed-side's realised P&L. Without this row, the eventual EXIT
        # would compute outcome_pnl against the *post-roll* _entry_credit,
        # making the adjusted side's realised P&L invisible to the
        # decision log (it lives only in broker.trades). Downstream
        # stratifiers can now sum ENTER + ADJUSTs + EXIT under one
        # trade_id for full lifecycle attribution.
        if side == "call":
            side_realized = float(ce_side_realized_pnl)
            side_label = "CE"
        else:
            side_realized = float(pe_side_realized_pnl)
            side_label = "PE"
        self._log_adjust_decision(
            leg="PREMIUM",
            mode="iron_condor",
            side_label=side_label,
            side_realized_pnl=side_realized,
            new_entry_premium=float(self._entry_credit),
        )

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
        # Realistic fill-based debit (Apr 29 2026, post chain-gap diagnostic).
        # Short legs BUY at ask, long legs SELL at bid. The resulting
        # ``outcome_pnl`` matches what the broker actually books — no
        # more LTP-based fiction.
        exit_debit = float(self._exit_fill_debit())
        pnl_estimate = float(self._entry_credit) - exit_debit
        logger.info(
            f"[EXIT] strategy={self.strategy_id} reason={reason} "
            f"entry_credit={self._entry_credit} exit_debit={exit_debit:.2f} "
            f"estimated_pnl={pnl_estimate:.2f} qty={self._quantity}"
        )
        self._log_decision(
            "EXIT",
            leg="PREMIUM",
            mode="iron_condor",
            entry_premium=float(self._entry_credit),
            quantity=self._quantity,
            exit_reason=reason,
            outcome_pnl=float(pnl_estimate) * self._quantity,
        )
        self._entered = False
        self._stopped_for_day = True
        # F1: LIMIT-at-mid on exit, with per-leg MARKET fallback to ensure
        # the position can always be flattened.
        def _exit_leg(sym: str, tok: int, side: OrderSide) -> SignalLeg:
            leg = self._build_option_leg(sym, tok, side, self._quantity)
            if leg is not None:
                return leg
            logger.warning(
                f"[{self.strategy_id}] EXIT fallback to MARKET for {sym} — no bid/ask"
            )
            return SignalLeg(
                tradingsymbol=sym,
                instrument_token=tok,
                order_side=side,
                quantity=self._quantity,
                order_type=OrderType.MARKET,
            )
        legs = [
            # Buy back short legs
            _exit_leg(self._short_ce_symbol, self._short_ce_token, OrderSide.BUY),
            _exit_leg(self._short_pe_symbol, self._short_pe_token, OrderSide.BUY),
            # Sell long legs
            _exit_leg(self._long_ce_symbol, self._long_ce_token, OrderSide.SELL),
            _exit_leg(self._long_pe_symbol, self._long_pe_token, OrderSide.SELL),
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
        # Inherited skip-log dedup: clear so today's first expiry/VIX
        # block log isn't shadowed by yesterday's stale minute key.
        self._last_skip_log_minute.clear()

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
