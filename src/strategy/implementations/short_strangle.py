"""Short Strangle strategy — sells OTM CE + OTM PE based on delta."""

import logging
import os
import time as _time
from datetime import date, time
from decimal import Decimal

from src.core.constants import LOT_SIZES
from src.core.models import Signal, SignalLeg, Subscription, Tick
from src.core.types import OrderSide, OrderType, SignalType
from src.strategy.base import BaseStrategy
from src.strategy.event_calendar import EventCalendar
from src.strategy.params import ShortStrangleParams
from src.strategy.regime import RegimeDetector
from src.strategy.registry import register_strategy
from src.strategy.scoring import SHORT_STRANGLE_CONFIG, score_strategy
from src.strategy.signals import adjust_signal, entry_signal, exit_signal

logger = logging.getLogger(__name__)


@register_strategy("short_strangle", ShortStrangleParams)
class ShortStrangleStrategy(BaseStrategy):
    """Sells OTM Call + OTM Put selected by delta.

    Unlike fixed-strike strangle, this selects strikes based on
    option delta for consistent risk profile across market conditions.
    """

    params: ShortStrangleParams

    MAX_ADJUSTMENTS_PER_DAY = 2

    # ─── Realistic-fill helpers (Apr 29 2026 multi-model audit fix) ──
    # Strangle has 2 short legs (CE + PE), no wings. The broker SELLS at
    # bid on entry and BUYS at ask on exit; the pre-fix code used LTP
    # midpoint for both, hiding the per-leg cross-spread cost in the
    # decision log. ``_bid_ask_for`` lives on BaseStrategy.

    def _entry_fill_credit(self) -> float:
        """Net credit at entry: SELL CE at bid + SELL PE at bid (per share)."""
        ce_bid, _ = self._bid_ask_for(self._ce_token)
        pe_bid, _ = self._bid_ask_for(self._pe_token)
        return ce_bid + pe_bid

    def _exit_fill_debit(self) -> float:
        """Net debit at exit: BUY CE at ask + BUY PE at ask (per share)."""
        _, ce_ask = self._bid_ask_for(self._ce_token)
        _, pe_ask = self._bid_ask_for(self._pe_token)
        return ce_ask + pe_ask

    def __init__(self, strategy_id: str, params: ShortStrangleParams):
        super().__init__(strategy_id, params)
        self._entered = False
        self._stopped_for_day = False
        self._regime: RegimeDetector | None = None
        # May 7 2026 Phase 1: calendar-aware filter (parallel to IC v2).
        # Loaded only when params.require_calendar_filter is True.
        self._event_calendar: EventCalendar | None = None
        self._paper_mode: bool = os.environ.get("PAPER_TRADING", "false").lower() == "true"
        self._ce_token: int = 0
        self._pe_token: int = 0
        self._ce_symbol: str = ""
        self._pe_symbol: str = ""
        self._ce_strike: float = 0
        self._pe_strike: float = 0
        self._entry_premium: Decimal = Decimal("0")
        self._peak_premium: Decimal = Decimal("0")  # For trailing stop
        # Apr 29 2026 Phase 1C: per-leg entry fill prices (₹/share). The
        # strategy SOLD each short at bid; per-leg fills let the
        # adjustment path attribute the rolled leg's realised P&L
        # against its own entry basis instead of against the post-roll
        # _entry_premium (which only reflects the new strike).
        self._ce_entry_fill: float = 0.0
        self._pe_entry_fill: float = 0.0
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
        # May 7 2026: pass simulated clock so backtest day-rollover detection
        # works (matches IC v2 / IB pattern).
        self._regime = RegimeDetector(
            self.ctx._feed, self.ctx._aggregator, self.ctx._chain_builder,
            clock=self.ctx.clock,
        )

        # May 7 2026 Phase 1: warm up daily-close deque for v2 regime
        # gate. Without this, the gate returns "insufficient_data"
        # perpetually because the launchd daily restart resets the
        # in-memory deque every morning. Failure non-fatal — falls back
        # to gradual in-memory accumulation.
        if getattr(self.params, "require_premium_selling_regime_v2", False):
            spot_token = self.ctx.get_spot_token(self.params.underlying)
            if spot_token is None:
                logger.warning(
                    f"[{self.strategy_id}] No spot token for {self.params.underlying} — "
                    f"v2 regime warmup skipped"
                )
            else:
                seeded = await self._regime.warmup_daily_closes(
                    self.ctx.get_historical_data,
                    self.params.underlying,
                    spot_token,
                )
                logger.info(
                    f"[{self.strategy_id}] v2 regime warmup: "
                    f"seeded {seeded} daily closes for {self.params.underlying}"
                )

        # May 7 2026 Phase 1: load EventCalendar for calendar-aware
        # filter (parallel to IC v2). Cheap CSV load (~143 rows).
        if getattr(self.params, "require_calendar_filter", False):
            try:
                self._event_calendar = EventCalendar()
                logger.info(
                    f"[{self.strategy_id}] calendar filter active: "
                    f"allowed_dow={self.params.allowed_days_of_week} "
                    f"block_pre_event_days={self.params.block_pre_event_days} "
                    f"block_friday={self.params.block_friday}"
                )
            except Exception as e:
                logger.warning(
                    f"[{self.strategy_id}] EventCalendar load failed: {e} — "
                    f"calendar filter disabled for this run"
                )
                self._event_calendar = None

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
            SHORT_STRANGLE_CONFIG, vix, morning_range_pct, move_from_open_pct,
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
        """Select strikes by delta and enter."""
        # --- Signal scoring (always logged; gating is mode-dependent) ---
        score, reasons = self._compute_score()
        reasons_str = ", ".join(reasons)
        logger.info(
            f"[SIGNAL_SCORE] strategy={self.strategy_id} score={score}/100 [{reasons_str}]"
        )
        # May 7 2026 Phase 1 fix: the score gate moved INTO the legacy
        # else-branch below (matches IC v2's structure). When
        # require_premium_selling_regime_v2=True, the score gate is
        # bypassed — only the v2 regime gate (CI+VRP) decides. This
        # fix was necessary because the score gate was killing entries
        # at score 22/100 even when the v2 gate would have allowed them
        # (the SS Phase 1 first-smoke saw only 9 trips because the
        # score gate filtered nearly everything before v2 ran).

        # Expiry-day 0DTE block — never enter naked premium when today == expiry
        expiry_block = self._check_expiry_day_block(self.params.underlying)
        if expiry_block:
            self._log_skip_throttled(
                "ENTRY_SKIP_EXPIRY",
                f"[{self.strategy_id}] Entry skipped: {expiry_block}",
            )
            return None

        # ─── May 7 2026 Phase 1: calendar-aware filter (parallel to IC v2) ──
        # Three sub-checks, all opt-in via require_calendar_filter:
        #   1. Day-of-week filter (default Tue/Wed/Thu — Anurag Goel
        #      Sharpe-1.96 NIFTY backtest)
        #   2. Pre-event block (T-1 before HARD_BLOCK events from
        #      data/event_days.csv: RBI MPC, FOMC, Budget, CPI)
        #   3. Friday block opt-in (extra weekend-gap insurance)
        if getattr(self.params, "require_calendar_filter", False) and self._event_calendar is not None:
            now = self.ctx.clock.now()
            today = now.date()
            dow = today.weekday()  # 0=Mon, 4=Fri

            # 1. Day-of-week
            allowed = self.params.allowed_days_of_week
            if allowed and dow not in allowed:
                self._log_skip_throttled(
                    "ENTRY_SKIP_DOW",
                    f"[{self.strategy_id}] Entry skipped: day-of-week {dow} not in {allowed}",
                )
                return None

            # 2. Pre-event window (weekday-only count; matches IC v2)
            pre_days = int(self.params.block_pre_event_days)
            if pre_days > 0:
                from datetime import timedelta
                for offset in range(1, pre_days * 2 + 3):
                    check_date = today + timedelta(days=offset)
                    is_blocked, ev_type = self._event_calendar.is_hard_blocked(check_date)
                    if is_blocked:
                        d = today + timedelta(days=1)
                        td = 0
                        while d <= check_date:
                            if d.weekday() < 5:
                                td += 1
                            d += timedelta(days=1)
                        if td <= pre_days:
                            self._log_skip_throttled(
                                "ENTRY_SKIP_PRE_EVENT",
                                f"[{self.strategy_id}] Entry skipped: "
                                f"{ev_type} in {td} trading day(s)",
                            )
                            return None
                        break

            # 3. Friday block (opt-in)
            if self.params.block_friday and dow == 4:
                self._log_skip_throttled(
                    "ENTRY_SKIP_FRIDAY",
                    f"[{self.strategy_id}] Entry skipped: Friday weekend-gap block",
                )
                return None

        # ─── May 7 2026 Phase 1: v2 regime gate (bypasses legacy filters) ──
        # When require_premium_selling_regime_v2 is True, the strategy
        # gates entries SOLELY on CI ≥ 61.8 AND VRP > 0 — same gate
        # validated on IC v2 (+₹584/324 trades on holdout).
        if getattr(self.params, "require_premium_selling_regime_v2", False):
            if not self._regime:
                self._log_skip_throttled(
                    "ENTRY_SKIP_REGIME_V2_NO_DETECTOR",
                    f"[{self.strategy_id}] Entry skipped: regime detector unavailable",
                )
                return None
            ok, metrics = self._regime.is_premium_selling_favorable_v2(self.params.underlying)
            if not ok:
                self._log_skip_throttled(
                    "ENTRY_SKIP_REGIME_V2_GATE",
                    f"[{self.strategy_id}] Entry skipped: regime gate v2 "
                    f"{metrics.get('reason', '?')}",
                )
                return None
            # v2 mode: skip every legacy filter and proceed directly
            # to chain selection.
        else:
            # ─── Legacy heuristic-filter pipeline (default behaviour) ─

            # Apr 29 Phase 2: threshold sourced from params (was
            # hardcoded 60). Moved here from the top of _try_entry so
            # it only gates in legacy mode — when v2 regime gate is
            # active, we let CI+VRP decide and skip the score check.
            score_thr = int(self.params.entry_score_threshold)
            if score < score_thr:
                self._log_skip_throttled(
                    "ENTRY_SKIP_SCORE",
                    f"[{self.strategy_id}] Entry skipped: signal score {score}/100 < {score_thr}",
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

        # Apr 29 Phase 2: liquidity gate — reject either leg whose
        # bid-ask spread exceeds params.max_spread_pct of mid. Promoted
        # to BaseStrategy so strangle inherits the same filter as IC.
        liquidity_blocks: list[str] = []
        for opt, label in ((best_ce.ce, "ce"), (best_pe.pe, "pe")):
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

        self._ce_token = best_ce.ce.instrument_token
        self._ce_symbol = best_ce.ce.tradingsymbol
        self._ce_strike = float(best_ce.strike)
        self._pe_token = best_pe.pe.instrument_token
        self._pe_symbol = best_pe.pe.tradingsymbol
        self._pe_strike = float(best_pe.strike)

        # Realistic-fill credit (Apr 29 2026): SELL legs cross the bid,
        # not the LTP midpoint. The pre-fix LTP path inflated entry
        # premium by ~one half-spread per leg, mismatching what the
        # broker actually books.
        ce_ltp = self.ctx.get_ltp(self._ce_token)  # logging only
        pe_ltp = self.ctx.get_ltp(self._pe_token)  # logging only
        self._entry_premium = Decimal(str(round(self._entry_fill_credit(), 2)))
        self._peak_premium = self._entry_premium
        # Apr 29 2026 Phase 1C: snapshot per-leg entry fills for ADJUST
        # attribution. Both legs SELL at bid.
        self._ce_entry_fill, _ = self._bid_ask_for(self._ce_token)
        self._pe_entry_fill, _ = self._bid_ask_for(self._pe_token)

        # F1: LIMIT-at-mid. If either leg can't be priced, abort entry.
        ce_leg = self._build_option_leg(
            self._ce_symbol, self._ce_token, OrderSide.SELL, self._quantity, opt=best_ce.ce,
        )
        pe_leg = self._build_option_leg(
            self._pe_symbol, self._pe_token, OrderSide.SELL, self._quantity, opt=best_pe.pe,
        )
        if ce_leg is None or pe_leg is None:
            logger.warning(
                f"[{self.strategy_id}] STRANGLE BLOCKED: could not price legs"
            )
            return None
        legs = [ce_leg, pe_leg]

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
            threshold=int(self.params.entry_score_threshold),
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

        # Apr 30 2026 multi-model audit fix: PT/SL/trail thresholds
        # use the REALISTIC close cost (BUY both legs at ask), not LTP
        # midpoint. Eliminates the prior decide-on-mid-fill-on-bid/ask
        # inconsistency. ``_exit_fill_debit`` is what the broker
        # actually books on close. LTPs cached for human-readable logs.
        # Wrap to Decimal so the existing Decimal arithmetic on
        # ``_entry_premium`` / ``_peak_premium`` doesn't TypeError.
        ce_ltp = self.ctx.get_ltp(self._ce_token)
        pe_ltp = self.ctx.get_ltp(self._pe_token)
        current_premium = Decimal(str(round(self._exit_fill_debit(), 2)))

        change_pct = float((current_premium - self._entry_premium) / self._entry_premium * 100)

        # Resolve exit thresholds — vol-scaled when opt-in, hardcoded otherwise.
        # `_compute_vol_scaled_exit_pct` returns `fallback_pct` verbatim when
        # `vol_scaled_exits=False`, so behavior is unchanged by default.
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
        if change_pct > sl_threshold_pct:
            logger.info(
                f"[{self.strategy_id}] STOP LOSS: premium up {change_pct:.1f}% "
                f"(threshold: {sl_threshold_pct}%)"
            )
            self._stopped_for_day = True
            return self._create_exit_signal(f"Stop loss: +{change_pct:.1f}%")

        # Trailing stop — lock in profits as premium decays
        # Tighten trail stop after 2pm (gamma risk increases near close)
        trail_pct = self._compute_vol_scaled_exit_pct(
            "trail", dte, fallback_pct=self.params.trail_stop_pct
        )
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

            # Apr 29 2026 Phase 1C: snapshot the OLD CE close fill BEFORE
            # the token is replaced. BUY-to-close pays ask; realized
            # P&L = (sold-at-bid - bought-at-ask) × quantity. The old
            # _ce_entry_fill is the entry-time bid for THIS leg.
            _, close_old_ce_ask = self._bid_ask_for(self._ce_token)
            ce_realized_pnl = (self._ce_entry_fill - close_old_ce_ask) * self._quantity

            # F1: LIMIT-at-mid for both close + open legs of the roll.
            close_leg = self._build_option_leg(
                self._ce_symbol, self._ce_token, OrderSide.BUY, self._quantity,
            )

            old_strike = self._ce_strike
            self._ce_token = best.ce.instrument_token
            self._ce_symbol = best.ce.tradingsymbol
            self._ce_strike = float(best.strike)
            open_leg = self._build_option_leg(
                self._ce_symbol, self._ce_token, OrderSide.SELL, self._quantity, opt=best.ce,
            )
            if close_leg is None or open_leg is None:
                logger.warning(
                    f"[{self.strategy_id}] CE ROLL BLOCKED: could not price close/open legs"
                )
                return None
            legs.append(close_leg)
            legs.append(open_leg)
            # Refresh CE entry fill to the new strike's bid; PE basis
            # untouched so a future EXIT or PE roll attributes correctly.
            self._ce_entry_fill, _ = self._bid_ask_for(self._ce_token)

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

            # Apr 29 2026 Phase 1C: same realized-P&L capture as the CE
            # branch — snapshot the close fill before the swap.
            _, close_old_pe_ask = self._bid_ask_for(self._pe_token)
            pe_realized_pnl = (self._pe_entry_fill - close_old_pe_ask) * self._quantity

            # F1: LIMIT-at-mid for both close + open legs of the roll.
            close_leg = self._build_option_leg(
                self._pe_symbol, self._pe_token, OrderSide.BUY, self._quantity,
            )

            old_strike = self._pe_strike
            self._pe_token = best.pe.instrument_token
            self._pe_symbol = best.pe.tradingsymbol
            self._pe_strike = float(best.strike)
            open_leg = self._build_option_leg(
                self._pe_symbol, self._pe_token, OrderSide.SELL, self._quantity, opt=best.pe,
            )
            if close_leg is None or open_leg is None:
                logger.warning(
                    f"[{self.strategy_id}] PE ROLL BLOCKED: could not price close/open legs"
                )
                return None
            legs.append(close_leg)
            legs.append(open_leg)
            # Refresh PE entry fill to new strike's bid; CE basis untouched.
            self._pe_entry_fill, _ = self._bid_ask_for(self._pe_token)

            logger.info(
                f"[{self.strategy_id}] ROLL PE: {old_strike} -> {self._pe_strike} "
                f"(delta was {self.params.adjustment_delta_threshold}+)"
            )

        # Re-record entry premium after roll using REALISTIC fills, not
        # LTP — see _entry_fill_credit docstring + Apr 29 multi-model
        # audit (5/6 reviewers flagged this exact LTP-rebase pattern).
        self._entry_premium = Decimal(str(round(self._entry_fill_credit(), 2)))
        self._peak_premium = self._entry_premium

        # Apr 29 2026 Phase 1C: emit an ADJUST decision row so the roll's
        # closed-leg realised P&L is captured under the active trade_id.
        # The eventual EXIT computes outcome_pnl against the *post-roll*
        # _entry_premium, so without this row the roll's P&L would be
        # invisible to the decision log (lives only in broker.trades).
        side_realized = ce_realized_pnl if leg_type == "CE" else pe_realized_pnl
        self._log_adjust_decision(
            leg="PREMIUM",
            mode="strangle",
            side_label=leg_type,
            side_realized_pnl=float(side_realized),
            new_entry_premium=float(self._entry_premium),
        )

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
        # Realistic-fill exit (Apr 29 2026): BUY both legs at ask. The
        # pre-fix LTP path made outcome_pnl read midpoint-to-midpoint
        # while the broker actually crossed the spread on both sides.
        exit_premium = Decimal(str(round(self._exit_fill_debit(), 2)))
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
        # F1: LIMIT-at-mid on exit, with per-leg MARKET fallback. Exiting is
        # time-critical (SL/trail fired); better to cross the spread on a
        # single leg than leave the position half-open.
        def _exit_leg(sym: str, tok: int) -> "SignalLeg":
            leg = self._build_option_leg(sym, tok, OrderSide.BUY, self._quantity)
            if leg is not None:
                return leg
            logger.warning(
                f"[{self.strategy_id}] EXIT fallback to MARKET for {sym} — no bid/ask"
            )
            return SignalLeg(
                tradingsymbol=sym,
                instrument_token=tok,
                order_side=OrderSide.BUY,
                quantity=self._quantity,
                order_type=OrderType.MARKET,
            )
        legs = [
            _exit_leg(self._ce_symbol, self._ce_token),
            _exit_leg(self._pe_symbol, self._pe_token),
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
