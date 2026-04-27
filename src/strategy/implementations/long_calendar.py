"""Long Calendar strategy — long vega, theta differential, positive-vol-expansion play.

Phase 3b candidate (Apr 27 2026): the FIRST positive-vega strategy in the
roster. Every previously-validated strategy (iron_condor, iron_butterfly,
short_strangle, short_straddle, trend_debit_spread) is short premium —
they all profit when vol stays low or compresses, and all share the same
tail-risk pattern (the Apr 7 tariff shock and May 8 vol spike that hurt
iron_condor's wf_coverage).

Long Calendar inverts that. Structure:
  SELL  front-week (or front-month) ATM-ish CE or PE
  BUY   back-week  (or back-month) same-strike same-type

Net debit position. Profits when:
  1. Underlying stays near the strike (theta differential — front decays
     faster than back)
  2. Implied vol expands (positive vega — back-leg gains more than front)
  3. Front-month decays toward zero on the morning of front expiry

Loses when:
  1. Underlying moves materially away from the strike
  2. Implied vol collapses (mean-reversion after a spike)
  3. Back-month crash without front-month protection

Key behavioral difference: a vol spike that causes IC stop-outs
typically PROFITS this strategy. So Long Calendar is a candidate
DIVERSIFIER alongside iron_condor — not a redundant short-premium
substitute.

Pre-registration discipline: per §VII the validation pipeline (CPCV +
walk-forward + cost-sensitivity + capacity + regime) is the same as
Phase 3a-revised used for the other 4 strategies. Holdout window is
fresh (calendar wasn't part of Phase 3a-revised's burned validation).
"""

import logging
import os
from datetime import date, time
from decimal import Decimal

from src.core.constants import LOT_SIZES
from src.core.models import OptionChain, Signal, Subscription, Tick
from src.core.types import OptionType, OrderSide, OrderType, SignalType
from src.strategy.base import BaseStrategy
from src.strategy.params import LongCalendarParams
from src.strategy.regime import RegimeDetector
from src.strategy.registry import register_strategy
from src.strategy.signals import entry_signal, exit_signal, make_leg

logger = logging.getLogger(__name__)


@register_strategy("long_calendar", LongCalendarParams)
class LongCalendarStrategy(BaseStrategy):
    """Long calendar spread on NIFTY (or other configurable underlying).

    Default config:
      - leg_type=CE, strike_offset_pct=0.0 → ATM call calendar
      - Sells front-week, buys back-week at the same strike
      - Profit target 30%, stop loss 50% of debit, hard stop on
        underlying moving >1.5% from strike
      - Closes 90 minutes before front-week expiry to avoid 0DTE gamma
        on the front leg
    """

    params: LongCalendarParams

    def __init__(self, strategy_id: str, params: LongCalendarParams):
        super().__init__(strategy_id, params)
        self._entered = False
        self._stopped_for_day = False
        self._regime: RegimeDetector | None = None
        self._paper_mode: bool = os.environ.get("PAPER_TRADING", "false").lower() == "true"
        # Strike + expiries
        self._strike: float = 0
        self._front_expiry: date | None = None
        self._back_expiry: date | None = None
        # Front leg (sold)
        self._front_token: int = 0
        self._front_symbol: str = ""
        # Back leg (bought)
        self._back_token: int = 0
        self._back_symbol: str = ""
        # Tracking
        self._entry_debit: Decimal = Decimal("0")
        self._peak_value: Decimal = Decimal("0")
        self._lot_size: int = LOT_SIZES.get(params.underlying, 75)
        self._quantity: int = params.quantity_lots * self._lot_size

    def get_subscriptions(self) -> Subscription:
        return Subscription(instrument_tokens=[], timeframes=[])

    @property
    def _expiry(self) -> date | None:
        # Alias front_expiry as _expiry so base helpers (e.g. _check_expiry_day_block,
        # _log_decision DTE computation) work without per-method overrides.
        return self._front_expiry

    async def on_start(self) -> None:
        self._front_expiry = self.ctx.next_expiry(self.params.underlying)
        self._back_expiry = self._find_back_expiry()
        self._regime = RegimeDetector(
            self.ctx._feed, self.ctx._aggregator, self.ctx._chain_builder
        )
        logger.info(
            f"[{self.strategy_id}] Started: {self.params.underlying} "
            f"front_expiry={self._front_expiry} back_expiry={self._back_expiry} "
            f"leg_type={self.params.leg_type} lots={self.params.quantity_lots}"
        )

    def _find_back_expiry(self) -> date | None:
        """Return the next available expiry strictly after the front."""
        if self._front_expiry is None:
            return None
        expiries = sorted(self.ctx.get_available_expiries(self.params.underlying))
        for e in expiries:
            if e > self._front_expiry:
                return e
        return None

    async def on_tick(self, tick: Tick) -> Signal | None:
        now = self.ctx.clock.now()

        # Capture morning VIX for any opt-in regime gates inherited from base
        self._capture_morning_vix_if_needed()

        # Expiry rollover — when front passes, advance both
        new_front = self._check_expiry_rollover(self._front_expiry, self.params.underlying)
        if new_front:
            self._front_expiry = new_front
            self._back_expiry = self._find_back_expiry()
            # Reset entry state on rollover
            self._entered = False
            self._stopped_for_day = False

        # End-of-day exit
        if now.time() >= self.params.exit_time and self._entered:
            self._stopped_for_day = True
            return self._create_exit_signal("Exit time reached")

        # Front-expiry-day buffer: close before front 0DTE gamma trap kicks in
        if (
            self._entered
            and self._front_expiry == now.date()
            and self._minutes_to_close(now.time()) <= self.params.front_close_buffer_minutes
        ):
            self._stopped_for_day = True
            return self._create_exit_signal(
                f"Front-expiry buffer: {self.params.front_close_buffer_minutes}min before close"
            )

        if not self._entered and not self._stopped_for_day and now.time() >= self.params.entry_time:
            return await self._try_entry()

        if self._entered:
            return self._check_exit_conditions()

        return None

    def _minutes_to_close(self, t: time) -> int:
        """Minutes from time t to 15:30 IST (market close)."""
        close = time(15, 30)
        return max(
            0,
            (close.hour - t.hour) * 60 + (close.minute - t.minute),
        )

    async def _try_entry(self) -> Signal | None:
        """Score the setup, apply filters, build the calendar legs."""
        if self._front_expiry is None or self._back_expiry is None:
            self._log_skip_throttled(
                "ENTRY_SKIP_NO_BACK_EXPIRY",
                f"[{self.strategy_id}] No back expiry available "
                f"(front={self._front_expiry})",
            )
            return None

        # Expiry-day block — never open a fresh calendar with front already
        # at/past expiry; that's just a long single-leg
        expiry_block = self._check_expiry_day_block(self.params.underlying)
        if expiry_block:
            self._log_skip_throttled(
                "ENTRY_SKIP_EXPIRY",
                f"[{self.strategy_id}] Entry skipped: {expiry_block}",
            )
            return None

        # VIX band — calendar wants moderate vol with room to expand
        vix_block = self._check_vix_filter()
        if vix_block:
            self._log_skip_throttled(
                "ENTRY_SKIP_VIX",
                f"[{self.strategy_id}] Entry skipped: {vix_block}",
            )
            return None

        # Phase 3b Gate B (intraday VIX spike) — opt-in via params
        spike_block = self._check_intraday_vix_spike_filter()
        if spike_block:
            self._log_skip_throttled(
                "ENTRY_SKIP_VIX_SPIKE",
                f"[{self.strategy_id}] Entry skipped: {spike_block}",
            )
            return None

        # PCR / max-pain default OFF for calendar (long-vega) but caller
        # may override via params. The base methods return None if disabled.
        if self.params.pcr_filter_enabled:
            pcr_block = self._check_pcr_filter(self.params.underlying, self._front_expiry)
            if pcr_block:
                self._log_skip_throttled(
                    "ENTRY_SKIP_PCR",
                    f"[{self.strategy_id}] Entry skipped: {pcr_block}",
                )
                return None
        if self.params.max_pain_filter_enabled:
            mp_block = self._check_max_pain_filter(self.params.underlying, self._front_expiry)
            if mp_block:
                self._log_skip_throttled(
                    "ENTRY_SKIP_MP",
                    f"[{self.strategy_id}] Entry skipped: {mp_block}",
                )
                return None

        # Pull spot + both chains
        spot = float(self.ctx.get_spot_price(self.params.underlying))
        if spot <= 0:
            return None
        front_chain = self.ctx.get_option_chain(self.params.underlying, self._front_expiry)
        back_chain = self.ctx.get_option_chain(self.params.underlying, self._back_expiry)
        if front_chain is None or back_chain is None or not front_chain.strikes or not back_chain.strikes:
            self._log_skip_throttled(
                "ENTRY_SKIP_NO_CHAIN",
                f"[{self.strategy_id}] Front or back chain unavailable",
            )
            return None

        # Determine target strike (ATM ± offset)
        strike_interval = 50 if self.params.underlying in ("NIFTY", "FINNIFTY") else 100
        offset_pts = spot * (self.params.strike_offset_pct / 100.0)
        target = spot + (offset_pts if self.params.leg_type == "CE" else -offset_pts)
        target_strike = round(target / strike_interval) * strike_interval

        # Find legs at that strike on BOTH chains (must exist on both)
        front_opt = self._find_leg(front_chain, target_strike, self.params.leg_type)
        back_opt = self._find_leg(back_chain, target_strike, self.params.leg_type)
        if front_opt is None or back_opt is None:
            self._log_skip_throttled(
                "ENTRY_SKIP_NO_STRIKE",
                f"[{self.strategy_id}] Strike {target_strike} {self.params.leg_type} "
                f"not on both chains",
            )
            return None

        # Build LIMIT-at-mid legs via base helper (returns None if un-priceable)
        front_leg = self._build_option_leg(
            front_opt.tradingsymbol,
            front_opt.instrument_token,
            OrderSide.SELL,
            self._quantity,
            opt=front_opt,
        )
        back_leg = self._build_option_leg(
            back_opt.tradingsymbol,
            back_opt.instrument_token,
            OrderSide.BUY,
            self._quantity,
            opt=back_opt,
        )
        if front_leg is None or back_leg is None:
            self._log_skip_throttled(
                "ENTRY_SKIP_PRICING",
                f"[{self.strategy_id}] CALENDAR BLOCKED: bid/ask missing on "
                f"front or back leg",
            )
            return None

        # Validate calendar economics: back must cost more than front (longer
        # time value). If inverted (e.g. cross-day pricing artifact), refuse.
        net_debit = back_leg.price - front_leg.price
        if net_debit <= Decimal("5"):
            self._log_skip_throttled(
                "ENTRY_SKIP_DEBIT",
                f"[{self.strategy_id}] Net debit {net_debit:.2f} <= 5 — "
                f"unusual term structure (front={front_leg.price} back={back_leg.price})",
            )
            return None

        # Commit state and emit signal
        self._strike = target_strike
        self._front_token = front_opt.instrument_token
        self._front_symbol = front_opt.tradingsymbol
        self._back_token = back_opt.instrument_token
        self._back_symbol = back_opt.tradingsymbol
        self._entry_debit = net_debit
        self._peak_value = net_debit
        self._entered = True

        logger.info(
            f"[ENTRY] strategy={self.strategy_id} type=long_calendar "
            f"underlying={self.params.underlying} spot={spot:.2f} "
            f"strike={self._strike} leg_type={self.params.leg_type} "
            f"front={self._front_symbol}@{front_leg.price} "
            f"back={self._back_symbol}@{back_leg.price} "
            f"net_debit={net_debit:.2f} qty={self._quantity}"
        )
        self._log_decision(
            "ENTER",
            leg="CALENDAR",
            mode="single_calendar",
            entry_premium=float(net_debit),
            quantity=self._quantity,
        )

        return entry_signal(
            self.strategy_id,
            [front_leg, back_leg],
            f"Long calendar @ {self._strike} {self.params.leg_type}, debit={net_debit:.2f}",
        )

    @staticmethod
    def _find_leg(chain: OptionChain, strike: float, leg_type: str):
        """Return the OptionData (CE or PE) at the given strike, or None."""
        for entry in chain.strikes:
            if float(entry.strike) == strike:
                return entry.ce if leg_type == "CE" else entry.pe
        return None

    def _check_exit_conditions(self) -> Signal | None:
        """Continuous exit checks: profit target, stop loss, underlying move."""
        if not self._entered:
            return None
        front_ltp = self.ctx.get_ltp(self._front_token)
        back_ltp = self.ctx.get_ltp(self._back_token)
        if front_ltp <= 0 or back_ltp <= 0:
            return None  # Pricing not available, hold position

        current_value = back_ltp - front_ltp  # spread MTM
        if current_value > self._peak_value:
            self._peak_value = current_value

        if self._entry_debit <= 0:
            return None
        change_pct = float((current_value - self._entry_debit) / self._entry_debit * 100)

        # Profit target
        if change_pct >= self.params.profit_target_pct:
            return self._create_exit_signal(
                f"Profit target: spread up {change_pct:.1f}% "
                f"(entry={self._entry_debit:.2f} now={current_value:.2f})"
            )

        # Stop loss
        if change_pct <= -self.params.stop_loss_pct:
            return self._create_exit_signal(
                f"Stop loss: spread down {abs(change_pct):.1f}% "
                f"(entry={self._entry_debit:.2f} now={current_value:.2f})"
            )

        # Underlying move stop
        spot = float(self.ctx.get_spot_price(self.params.underlying))
        if spot > 0 and self._strike > 0:
            move_pct = abs(spot - self._strike) / self._strike * 100
            if move_pct > self.params.max_underlying_move_pct:
                return self._create_exit_signal(
                    f"Underlying moved {move_pct:.2f}% from strike "
                    f"(spot={spot:.2f} strike={self._strike})"
                )

        return None

    async def on_stop(self) -> None:
        if self._entered:
            logger.info(f"[{self.strategy_id}] Stopping with open calendar position")

    def reset_day_state(self) -> None:
        """Reset intraday flags at start of new trading day.

        Critical: long_calendar is a MULTI-DAY position by design (sells
        front-week, buys back-week — held until front expiry approaches).
        Unlike short_straddle / iron_condor which always exit same-day,
        the calendar must NOT reset ``_entered`` daily, or it compounds
        into stacked positions over the week. Only reset the per-day
        skip-log dedup; entry/stop flags persist until expiry rollover
        or an explicit exit fires.
        """
        # Do NOT reset self._entered or self._stopped_for_day here.
        # Both are cleared in on_tick when _check_expiry_rollover fires
        # (front-week expiring → fresh week → fresh entry decision) or
        # explicitly by _create_exit_signal.
        self._last_skip_log_minute.clear()

    def _create_exit_signal(self, reason: str) -> Signal:
        """Close both legs of the calendar — buy back the front, sell the back."""
        self._stopped_for_day = True
        front_leg = make_leg(
            self._front_symbol,
            self._front_token,
            OrderSide.BUY,  # buy back the sold front
            self._quantity,
            order_type=OrderType.MARKET,
        )
        back_leg = make_leg(
            self._back_symbol,
            self._back_token,
            OrderSide.SELL,  # sell the bought back
            self._quantity,
            order_type=OrderType.MARKET,
        )
        front_ltp = self.ctx.get_ltp(self._front_token)
        back_ltp = self.ctx.get_ltp(self._back_token)
        exit_value = back_ltp - front_ltp
        pnl = float(exit_value - self._entry_debit) * self._quantity
        logger.info(
            f"[EXIT] strategy={self.strategy_id} reason={reason} "
            f"entry_debit={self._entry_debit:.2f} exit_value={exit_value:.2f} "
            f"pnl={pnl:+.0f}"
        )
        self._log_decision(
            "EXIT",
            leg="CALENDAR",
            mode="single_calendar",
            entry_premium=float(self._entry_debit),
            quantity=self._quantity,
            exit_reason=reason,
            outcome_pnl=pnl,
        )
        self._entered = False
        return exit_signal(self.strategy_id, [front_leg, back_leg], reason)
