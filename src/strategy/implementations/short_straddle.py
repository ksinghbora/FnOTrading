"""Short Straddle strategy — sells ATM CE + PE with adjustments.

The bread-and-butter of Indian F&O algo trading.
Profits from time decay (theta) when the market stays range-bound.
"""

import logging
import os
from datetime import date, datetime, time
from decimal import Decimal

from src.core.constants import LOT_SIZES
from src.core.models import OHLC, Order, Signal, SignalLeg, Subscription, Tick
from src.core.types import OptionType, OrderSide, OrderType, SignalType
from src.strategy.base import BaseStrategy
from src.strategy.params import ShortStraddleParams
from src.strategy.regime import RegimeDetector
from src.strategy.registry import register_strategy
from src.strategy.scoring import SHORT_STRADDLE_CONFIG, score_strategy
from src.strategy.signals import entry_signal, exit_signal, adjust_signal

logger = logging.getLogger(__name__)


@register_strategy("short_straddle", ShortStraddleParams)
class ShortStraddleStrategy(BaseStrategy):
    """Sells ATM Call + ATM Put at a configured time.

    Features:
    - ATM strike selection based on spot price
    - Configurable entry/exit time
    - Adjustment when premium moves X% against
    - Stop loss at X% of total premium collected
    - Optional hedge with far OTM options
    """

    params: ShortStraddleParams
    # V5: short ATM CE + ATM PE — most aggressive premium-selling form.
    regime_family: str = "premium_selling"

    # ─── Realistic-fill helpers (Apr 29 2026 multi-model audit fix) ──
    # Same structure as strangle: two ATM short legs, no wings. SELL at
    # bid on entry, BUY at ask on exit. ``_bid_ask_for`` lives on
    # BaseStrategy. Replaces the LTP-midpoint fiction in the prior
    # outcome_pnl path.

    def _entry_fill_credit(self) -> float:
        """Net credit at entry: SELL CE at bid + SELL PE at bid."""
        ce_bid, _ = self._bid_ask_for(self._ce_token)
        pe_bid, _ = self._bid_ask_for(self._pe_token)
        return ce_bid + pe_bid

    def _exit_fill_debit(self) -> float:
        """Net debit at exit: BUY CE at ask + BUY PE at ask."""
        _, ce_ask = self._bid_ask_for(self._ce_token)
        _, pe_ask = self._bid_ask_for(self._pe_token)
        return ce_ask + pe_ask

    def __init__(self, strategy_id: str, params: ShortStraddleParams):
        super().__init__(strategy_id, params)
        self._entered = False
        self._stopped_for_day = False  # Prevents re-entry after stop loss
        self._regime: RegimeDetector | None = None
        self._paper_mode: bool = os.environ.get("PAPER_TRADING", "false").lower() == "true"
        self._ce_token: int = 0
        self._pe_token: int = 0
        self._ce_symbol: str = ""
        self._pe_symbol: str = ""
        self._hedge_ce_token: int = 0
        self._hedge_pe_token: int = 0
        self._hedge_ce_symbol: str = ""
        self._hedge_pe_symbol: str = ""
        self._entry_premium: Decimal = Decimal("0")
        self._peak_premium: Decimal = Decimal("0")  # For trailing stop
        # Apr 29 2026 Phase 1C: per-leg entry fill prices (bid each, since
        # both shorts SELL at bid). Used by _adjust_losing_leg to attribute
        # the rolled leg's realised P&L against the right basis.
        self._ce_entry_fill: float = 0.0
        self._pe_entry_fill: float = 0.0
        self._atm_strike: float = 0
        self._expiry: date | None = None
        self._lot_size: int = LOT_SIZES.get(params.underlying, 75)
        self._quantity: int = params.quantity_lots * self._lot_size

    def get_subscriptions(self) -> Subscription:
        # We subscribe dynamically after determining ATM strike
        return Subscription(instrument_tokens=[], timeframes=[])

    async def on_start(self) -> None:
        """Initialize — determine expiry and subscribe to spot."""
        self._expiry = self.ctx.next_expiry(self.params.underlying)
        self._regime = RegimeDetector(self.ctx._feed, self.ctx._aggregator, self.ctx._chain_builder)
        logger.info(
            f"[{self.strategy_id}] Started: {self.params.underlying} "
            f"expiry={self._expiry} lots={self.params.quantity_lots}"
        )

    async def on_tick(self, tick: Tick) -> Signal | None:
        now = self.ctx.clock.now()

        # Expiry rollover — if past expiry, update to next one
        new_expiry = self._check_expiry_rollover(self._expiry, self.params.underlying)
        if new_expiry:
            self._expiry = new_expiry

        # Check exit time — stop for day to prevent re-entry loop
        if now.time() >= self.params.exit_time and self._entered:
            self._stopped_for_day = True
            return self._create_exit_signal("Exit time reached")

        # Check entry time (don't re-enter after stop loss)
        if not self._entered and not self._stopped_for_day and now.time() >= self.params.entry_time:
            return await self._try_entry()

        # Monitor position if entered
        if self._entered:
            return self._check_adjustments(tick)

        return None

    def _compute_score(self) -> tuple[int, list[str]]:
        """Pure scorer — no state mutation. Used by _try_entry and
        evaluate_score (orchestrator). Returns (score, reasons)."""
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
            SHORT_STRADDLE_CONFIG, vix, morning_range_pct, move_from_open_pct,
            pcr_oi, is_expiry_day, dte,
        )
        return int(score), reasons

    def evaluate_score(self) -> int:
        try:
            score, _ = self._compute_score()
            return score
        except Exception:
            return 0

    async def _try_entry(self) -> Signal | None:
        """Enter the straddle — sell ATM CE + ATM PE."""
        # --- Signal scoring ---
        score, reasons = self._compute_score()
        reasons_str = ", ".join(reasons)
        logger.info(
            f"[SIGNAL_SCORE] strategy={self.strategy_id} score={score}/100 [{reasons_str}]"
        )
        # Per-tick → per-minute throttling for all entry-skip logs. Mirrors
        # the parallel changes in short_strangle and iron_condor; see
        # _log_skip_throttled docstring on BaseStrategy for rationale.
        # Apr 29 Phase 2: threshold sourced from params (was hardcoded 60).
        score_thr = int(self.params.entry_score_threshold)
        if score < score_thr:
            self._log_skip_throttled(
                "ENTRY_SKIP_SCORE",
                f"[{self.strategy_id}] Entry skipped: signal score {score}/100 < {score_thr}",
            )
            return None

        # Expiry-day 0DTE block — straddle is ATM, gets crushed worst by gamma vertical
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
            self._log_skip_throttled(
                "ENTRY_SKIP_VIX",
                f"[{self.strategy_id}] Entry skipped: {vix_block}",
            )
            return None

        # Trend filter — skip if market is trending >0.7% from open
        trend_block = self._check_trend_filter(self.params.underlying)
        if trend_block:
            self._log_skip_throttled(
                "ENTRY_SKIP_TREND",
                f"[{self.strategy_id}] Entry skipped: {trend_block}",
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

        spot = self.ctx.get_spot_price(self.params.underlying)
        if spot <= 0:
            return None

        # VIX-adjusted position sizing
        adjusted_lots = self._get_vix_adjusted_lots()
        self._quantity = adjusted_lots * self._lot_size

        # Determine ATM strike
        strike_interval = 50 if self.params.underlying in ("NIFTY", "FINNIFTY") else 100
        self._atm_strike = round(float(spot) / strike_interval) * strike_interval

        # Get option chain to find instruments
        chain = self.ctx.get_option_chain(self.params.underlying, self._expiry)
        if not chain:
            logger.warning(f"[{self.strategy_id}] No option chain available")
            return None

        # Find CE and PE at ATM strike. We hold OptionData refs locally
        # so the Phase-2 liquidity filter (below) can inspect bid/ask
        # before committing instance state.
        atm_ce_opt = None
        atm_pe_opt = None
        for entry in chain.strikes:
            if float(entry.strike) == self._atm_strike:
                atm_ce_opt = entry.ce
                atm_pe_opt = entry.pe
                break

        if not atm_ce_opt or not atm_pe_opt:
            logger.warning(f"[{self.strategy_id}] Could not find ATM options at strike {self._atm_strike}")
            return None

        # Apr 29 Phase 2: liquidity gate — reject either leg whose
        # bid-ask spread exceeds params.max_spread_pct of mid.
        liquidity_blocks: list[str] = []
        for opt, label in ((atm_ce_opt, "ce"), (atm_pe_opt, "pe")):
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

        # Commit tokens / symbols only after liquidity check passes.
        self._ce_token = atm_ce_opt.instrument_token
        self._ce_symbol = atm_ce_opt.tradingsymbol
        self._pe_token = atm_pe_opt.instrument_token
        self._pe_symbol = atm_pe_opt.tradingsymbol

        # Record entry premium using REALISTIC fills (bid for SELL legs).
        # Apr 29 2026 audit fix — see _entry_fill_credit docstring.
        ce_ltp = self.ctx.get_ltp(self._ce_token)  # logging only
        pe_ltp = self.ctx.get_ltp(self._pe_token)  # logging only
        self._entry_premium = Decimal(str(round(self._entry_fill_credit(), 2)))
        # Phase 1C: snapshot per-leg fills for ADJUST P&L attribution.
        self._ce_entry_fill, _ = self._bid_ask_for(self._ce_token)
        self._pe_entry_fill, _ = self._bid_ask_for(self._pe_token)

        # F1: LIMIT-at-mid. atm_ce_opt / atm_pe_opt were resolved above
        # for the liquidity check; reuse them here so we don't iterate
        # the chain a second time.
        ce_leg = self._build_option_leg(
            self._ce_symbol, self._ce_token, OrderSide.SELL, self._quantity, opt=atm_ce_opt,
        )
        pe_leg = self._build_option_leg(
            self._pe_symbol, self._pe_token, OrderSide.SELL, self._quantity, opt=atm_pe_opt,
        )
        if ce_leg is None or pe_leg is None:
            logger.warning(
                f"[{self.strategy_id}] STRADDLE BLOCKED: could not price ATM legs"
            )
            return None
        legs = [ce_leg, pe_leg]

        # Add hedge legs if configured
        if self.params.add_hedge:
            hedge_legs = self._create_hedge_legs(chain)
            if not hedge_legs and self.params.add_hedge:
                # Hedge was requested but couldn't be priced — continue without it.
                # _create_hedge_legs already logs its own warnings.
                pass
            legs.extend(hedge_legs)

        self._entered = True
        self._peak_premium = self._entry_premium  # Initialize trailing stop tracker
        logger.info(
            f"[ENTRY] strategy={self.strategy_id} type=short_straddle "
            f"underlying={self.params.underlying} spot={spot} "
            f"atm_strike={self._atm_strike} expiry={self._expiry} "
            f"ce_symbol={self._ce_symbol} ce_premium={ce_ltp} "
            f"pe_symbol={self._pe_symbol} pe_premium={pe_ltp} "
            f"total_premium={self._entry_premium} qty={self._quantity} "
            f"lots={adjusted_lots}"
        )
        self._log_decision(
            "ENTER",
            leg="PREMIUM",
            mode="straddle",
            rule_score=score,
            threshold=int(self.params.entry_score_threshold),
            entry_premium=float(self._entry_premium),
            quantity=self._quantity,
        )

        return entry_signal(self.strategy_id, legs, f"Straddle @ {self._atm_strike}")

    def _check_adjustments(self, tick: Tick) -> Signal | None:
        """Monitor position and adjust if needed."""
        if not self._entered:
            return None

        # Apr 30 2026 multi-model audit fix: PT/SL/trail thresholds
        # use the REALISTIC close cost (BUY both legs at ask), not LTP
        # midpoint. Mirror of the strangle/IC fix — see those strategies'
        # _check_adjustments docstrings for the rationale.
        ce_ltp = self.ctx.get_ltp(self._ce_token)
        pe_ltp = self.ctx.get_ltp(self._pe_token)
        current_premium = Decimal(str(round(self._exit_fill_debit(), 2)))

        if self._entry_premium <= 0:
            return None

        premium_change_pct = float((current_premium - self._entry_premium) / self._entry_premium * 100)

        # Resolve exit thresholds — vol-scaled when opt-in, hardcoded otherwise.
        dte = (self._expiry - self.ctx.clock.now().date()).days if self._expiry else 7
        pt_target_pct = self._compute_vol_scaled_exit_pct(
            "pt", dte, fallback_pct=self.params.profit_target_pct
        )
        sl_threshold_pct = self._compute_vol_scaled_exit_pct(
            "sl", dte, fallback_pct=self.params.stop_loss_pct
        )

        # Profit target — exit when premium has decayed enough
        if pt_target_pct > 0:
            decay_pct = float((self._entry_premium - current_premium) / self._entry_premium * 100)
            if decay_pct >= pt_target_pct:
                logger.info(
                    f"[{self.strategy_id}] PROFIT TARGET: premium decayed {decay_pct:.1f}% "
                    f"(target: {pt_target_pct}%)"
                )
                self._stopped_for_day = True
                return self._create_exit_signal(f"Profit target: premium decayed {decay_pct:.1f}%")

        # Stop loss check
        if premium_change_pct > sl_threshold_pct:
            logger.info(
                f"[{self.strategy_id}] STOP LOSS: premium up {premium_change_pct:.1f}% "
                f"(threshold: {sl_threshold_pct}%)"
            )
            self._stopped_for_day = True
            return self._create_exit_signal(f"Stop loss: premium +{premium_change_pct:.1f}%")

        # Trailing stop — lock in profits as premium decays
        trail_pct = self._compute_vol_scaled_exit_pct(
            "trail", dte, fallback_pct=self.params.trail_stop_pct
        )

        if trail_pct > 0 and current_premium < self._peak_premium:
            # Premium is decaying (good for us) — track the low
            self._peak_premium = min(self._peak_premium, current_premium)
        elif trail_pct > 0 and current_premium > self._peak_premium:
            # Premium bouncing back — check time + decay gates first
            profit_locked = float(
                (self._entry_premium - self._peak_premium) / self._entry_premium * 100
            )
            if self._can_activate_trail_stop(profit_locked):
                bounce_pct = float(
                    (current_premium - self._peak_premium) / self._entry_premium * 100
                )
                if bounce_pct > trail_pct:
                    logger.info(
                        f"[{self.strategy_id}] TRAIL STOP: premium bounced {bounce_pct:.1f}% "
                        f"from low, locking {profit_locked:.1f}% profit"
                    )
                    self._stopped_for_day = True
                    return self._create_exit_signal(
                        f"Trailing stop: bounced {bounce_pct:.1f}% from {self._peak_premium}"
                    )

        # Adjustment: shift the losing leg to the new ATM strike
        if premium_change_pct > self.params.adjustment_threshold_pct:
            return self._adjust_losing_leg(ce_ltp, pe_ltp)

        return None

    def _adjust_losing_leg(self, ce_ltp: Decimal, pe_ltp: Decimal) -> Signal | None:
        """Close the losing leg and re-enter at the new ATM strike."""
        spot = self.ctx.get_spot_price(self.params.underlying)
        if spot <= 0:
            return None

        strike_interval = 50 if self.params.underlying in ("NIFTY", "FINNIFTY") else 100
        new_atm = round(float(spot) / strike_interval) * strike_interval

        if new_atm == self._atm_strike:
            return None  # No shift needed, spot hasn't moved enough

        chain = self.ctx.get_option_chain(self.params.underlying, self._expiry)
        if not chain:
            return None

        # Determine which leg is losing (further ITM = higher premium)
        is_ce_losing = ce_ltp > pe_ltp
        legs: list[SignalLeg] = []

        # Apr 29 2026 Phase 1C: snapshot the close fill of the OLD losing
        # leg BEFORE the swap. BUY-to-close pays ask; realized = (entry
        # bid - close ask) × quantity. The un-touched leg's basis is
        # preserved.
        if is_ce_losing:
            _, close_old_ask = self._bid_ask_for(self._ce_token)
            losing_realized_pnl = (self._ce_entry_fill - close_old_ask) * self._quantity
        else:
            _, close_old_ask = self._bid_ask_for(self._pe_token)
            losing_realized_pnl = (self._pe_entry_fill - close_old_ask) * self._quantity

        # Find new strike BEFORE mutating state
        new_token = 0
        new_symbol = ""
        # F1: LIMIT-at-mid for both close + open legs of the adjustment.
        if is_ce_losing:
            close_leg = self._build_option_leg(
                self._ce_symbol, self._ce_token, OrderSide.BUY, self._quantity,
            )
            if close_leg is None:
                logger.warning(f"[{self.strategy_id}] ADJUST CE BLOCKED: no quote to close old CE")
                return None
            legs.append(close_leg)
            for entry in chain.strikes:
                if float(entry.strike) == new_atm and entry.ce:
                    new_token = entry.ce.instrument_token
                    new_symbol = entry.ce.tradingsymbol
                    open_leg = self._build_option_leg(
                        new_symbol, new_token, OrderSide.SELL, self._quantity, opt=entry.ce,
                    )
                    if open_leg is None:
                        logger.warning(f"[{self.strategy_id}] ADJUST CE BLOCKED: no quote for new CE at {new_atm}")
                        return None
                    legs.append(open_leg)
                    break
        else:
            close_leg = self._build_option_leg(
                self._pe_symbol, self._pe_token, OrderSide.BUY, self._quantity,
            )
            if close_leg is None:
                logger.warning(f"[{self.strategy_id}] ADJUST PE BLOCKED: no quote to close old PE")
                return None
            legs.append(close_leg)
            for entry in chain.strikes:
                if float(entry.strike) == new_atm and entry.pe:
                    new_token = entry.pe.instrument_token
                    new_symbol = entry.pe.tradingsymbol
                    open_leg = self._build_option_leg(
                        new_symbol, new_token, OrderSide.SELL, self._quantity, opt=entry.pe,
                    )
                    if open_leg is None:
                        logger.warning(f"[{self.strategy_id}] ADJUST PE BLOCKED: no quote for new PE at {new_atm}")
                        return None
                    legs.append(open_leg)
                    break

        if len(legs) != 2:
            logger.warning(f"[{self.strategy_id}] Could not find new ATM option at {new_atm}")
            return None

        # Now safe to mutate state — new strike confirmed
        old_atm = self._atm_strike
        self._atm_strike = new_atm
        if is_ce_losing:
            self._ce_token = new_token
            self._ce_symbol = new_symbol
        else:
            self._pe_token = new_token
            self._pe_symbol = new_symbol
        # Refresh the rolled leg's entry-fill basis BEFORE recomputing
        # the aggregate _entry_premium. The un-rolled leg's basis is
        # preserved so a future EXIT or second adjustment attributes
        # P&L correctly.
        if is_ce_losing:
            self._ce_entry_fill, _ = self._bid_ask_for(self._ce_token)
        else:
            self._pe_entry_fill, _ = self._bid_ask_for(self._pe_token)
        # Re-record entry premium after adjustment using REALISTIC fills.
        # Apr 29 2026 audit (5/6 reviewers flagged the LTP-rebase pattern).
        self._entry_premium = Decimal(str(round(self._entry_fill_credit(), 2)))

        side = "CE" if is_ce_losing else "PE"
        # Phase 1C: emit an ADJUST decision row capturing the closed
        # leg's realised P&L under the active trade_id. Without this,
        # the roll's P&L lives only in broker.trades and is never
        # attributed to the strategy's outcome_pnl chain.
        self._log_adjust_decision(
            leg="PREMIUM",
            mode="straddle",
            side_label=side,
            side_realized_pnl=float(losing_realized_pnl),
            new_entry_premium=float(self._entry_premium),
        )
        logger.info(
            f"[{self.strategy_id}] ADJUST: Shifted {side} from {old_atm} to {new_atm}"
        )

        return adjust_signal(
            self.strategy_id, legs,
            f"Shifted {side} from {old_atm} to {new_atm}"
        )

    def _create_exit_signal(self, reason: str) -> Signal:
        """Create signal to close all positions including hedges."""
        # Realistic-fill exit (Apr 29 2026): BUY both legs at ask. The
        # pre-fix LTP-midpoint path systematically over-reported
        # outcome_pnl by one half-spread per leg.
        exit_premium = Decimal(str(round(self._exit_fill_debit(), 2)))
        pnl_estimate = self._entry_premium - exit_premium
        logger.info(
            f"[EXIT] strategy={self.strategy_id} reason={reason} "
            f"entry_premium={self._entry_premium} exit_premium={exit_premium} "
            f"estimated_pnl={pnl_estimate} qty={self._quantity}"
        )
        self._log_decision(
            "EXIT",
            leg="PREMIUM",
            mode="straddle",
            entry_premium=float(self._entry_premium),
            quantity=self._quantity,
            exit_reason=reason,
            outcome_pnl=float(pnl_estimate) * self._quantity,
        )
        self._entered = False
        self._stopped_for_day = True

        # F1: LIMIT-at-mid on exit with per-leg MARKET fallback (can't stall exits).
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
            _exit_leg(self._ce_symbol, self._ce_token, OrderSide.BUY),
            _exit_leg(self._pe_symbol, self._pe_token, OrderSide.BUY),
        ]
        # Close hedge legs if they exist
        if self._hedge_ce_token:
            legs.append(_exit_leg(self._hedge_ce_symbol, self._hedge_ce_token, OrderSide.SELL))
        if self._hedge_pe_token:
            legs.append(_exit_leg(self._hedge_pe_symbol, self._hedge_pe_token, OrderSide.SELL))
        return exit_signal(self.strategy_id, legs, reason)

    def _create_hedge_legs(self, chain) -> list[SignalLeg]:
        """Create far OTM hedge legs for margin benefit and tail risk.

        IMPORTANT: Both CE and PE hedges must be added together.
        A partial hedge (one wing only) creates asymmetric risk worse than no hedge.
        If either leg cannot be found, no hedge legs are added.
        """
        offset = self.params.hedge_offset_strikes
        strike_interval = 50 if self.params.underlying in ("NIFTY", "FINNIFTY") else 100

        hedge_ce_strike = self._atm_strike + (offset * strike_interval)
        hedge_pe_strike = self._atm_strike - (offset * strike_interval)

        ce_token = 0
        ce_symbol = ""
        pe_token = 0
        pe_symbol = ""

        for entry in chain.strikes:
            strike = float(entry.strike)
            if strike == hedge_ce_strike and entry.ce:
                ce_token = entry.ce.instrument_token
                ce_symbol = entry.ce.tradingsymbol
            if strike == hedge_pe_strike and entry.pe:
                pe_token = entry.pe.instrument_token
                pe_symbol = entry.pe.tradingsymbol

        if not ce_token:
            logger.warning(
                f"[{self.strategy_id}] Hedge CE not found at strike {hedge_ce_strike} — "
                f"skipping hedge entirely to avoid asymmetric risk"
            )
            return []
        if not pe_token:
            logger.warning(
                f"[{self.strategy_id}] Hedge PE not found at strike {hedge_pe_strike} — "
                f"skipping hedge entirely to avoid asymmetric risk"
            )
            return []

        # Both legs found — safe to create symmetric hedge
        self._hedge_ce_token = ce_token
        self._hedge_ce_symbol = ce_symbol
        self._hedge_pe_token = pe_token
        self._hedge_pe_symbol = pe_symbol

        logger.info(
            f"[{self.strategy_id}] Hedge legs: CE={ce_symbol} @ {hedge_ce_strike}, "
            f"PE={pe_symbol} @ {hedge_pe_strike}"
        )
        # F1: LIMIT-at-mid for hedge legs. If either can't be priced (far OTM
        # often has 0 bid), skip the whole hedge — a one-sided hedge is worse
        # than no hedge (asymmetric risk). Matches the existing "both or
        # neither" invariant above.
        hedge_ce_opt = None
        hedge_pe_opt = None
        for entry in chain.strikes:
            if float(entry.strike) == hedge_ce_strike:
                hedge_ce_opt = entry.ce
            if float(entry.strike) == hedge_pe_strike:
                hedge_pe_opt = entry.pe
        ce_leg = self._build_option_leg(
            ce_symbol, ce_token, OrderSide.BUY, self._quantity, opt=hedge_ce_opt,
        )
        pe_leg = self._build_option_leg(
            pe_symbol, pe_token, OrderSide.BUY, self._quantity, opt=hedge_pe_opt,
        )
        if ce_leg is None or pe_leg is None:
            logger.warning(
                f"[{self.strategy_id}] Hedge legs un-priceable at strikes "
                f"({hedge_ce_strike}, {hedge_pe_strike}) — dropping hedge rather than "
                f"accepting asymmetric protection"
            )
            # Reset the stored hedge tokens so the exit path doesn't try to
            # close legs that never opened.
            self._hedge_ce_token = 0
            self._hedge_ce_symbol = ""
            self._hedge_pe_token = 0
            self._hedge_pe_symbol = ""
            return []
        return [ce_leg, pe_leg]

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
            "hedge_ce_token": self._hedge_ce_token,
            "hedge_pe_token": self._hedge_pe_token,
            "hedge_ce_symbol": self._hedge_ce_symbol,
            "hedge_pe_symbol": self._hedge_pe_symbol,
            "entry_premium": str(self._entry_premium),
            "peak_premium": str(self._peak_premium),
            "atm_strike": self._atm_strike,
        }

    def load_state_data(self, data: dict) -> None:
        self._entered = data.get("entered", False)
        self._stopped_for_day = data.get("stopped_for_day", False)
        self._ce_token = data.get("ce_token", 0)
        self._pe_token = data.get("pe_token", 0)
        self._ce_symbol = data.get("ce_symbol", "")
        self._pe_symbol = data.get("pe_symbol", "")
        self._hedge_ce_token = data.get("hedge_ce_token", 0)
        self._hedge_pe_token = data.get("hedge_pe_token", 0)
        self._hedge_ce_symbol = data.get("hedge_ce_symbol", "")
        self._hedge_pe_symbol = data.get("hedge_pe_symbol", "")
        self._entry_premium = Decimal(data.get("entry_premium", "0"))
        self._peak_premium = Decimal(data.get("peak_premium", "0"))
        self._atm_strike = data.get("atm_strike", 0)
