"""Long Straddle strategy — long-vol via long ATM CE+PE.

May 6 2026 — built post LC v2 / v2b verdict (commit e09eb9b). Long
calendar with any regime gate failed because spot moved away from
strike on most Indian post-SEBI days, killing the calendar's narrow
profit zone. Long straddle inverts the structural assumption: one
of the two legs ALWAYS goes ITM on a directional move, so "spot
leaves strike" is the source of profit, not catastrophe.

Structure:
  BUY  ATM CE  +  BUY  ATM PE  (same expiry, same strike)

Net debit position. Profits when:
  1. Spot moves materially away from strike (gamma — one leg goes ITM)
  2. Implied vol expands (positive vega on both legs)

Loses when:
  1. Spot stays near strike (theta decay on both legs)
  2. IV collapses (negative vega on both legs)

This is the "anti-LC for the same gate" experiment: LC needed range,
LS needs movement. Both want VRP<0 (cheap IV with room to expand).
If LS fires meaningfully more profitable trades than LC v2/v2b under
the same gate, the binding constraint was the calendar STRUCTURE,
not the long-vol thesis itself. If LS also fails, the verdict is
that long-vol on Indian post-SEBI options is structurally untradable
and the next move is to pivot to a different instrument class
(futures or equity stat-arb).
"""

import logging
import os
from datetime import date, time
from decimal import Decimal

from src.core.constants import LOT_SIZES
from src.core.models import OptionChain, Signal, Subscription, Tick
from src.core.types import OptionType, OrderSide, OrderType, SignalType
from src.strategy.base import BaseStrategy
from src.strategy.params import LongStraddleParams
from src.strategy.regime import RegimeDetector
from src.strategy.registry import register_strategy
from src.strategy.signals import entry_signal, exit_signal, make_leg

logger = logging.getLogger(__name__)


@register_strategy("long_straddle", LongStraddleParams)
class LongStraddleStrategy(BaseStrategy):
    """Long straddle on NIFTY (or other configurable underlying).

    Default config:
      - ATM CE + ATM PE on the front-week expiry
      - Profit target 50% (high — needs a meaningful move to overcome
        round-trip cost wall on both legs)
      - Stop loss 50% of debit (limit theta bleed on flat days)
      - Exit at exit_time (default 15:00 IST) — intraday-only

    The straddle is intentionally NOT held overnight on default config
    (entry_time 09:30, exit_time 15:00, force-close on expiry day at
    14:30). Most theta accelerates in the final 1-2 hours of expiry-day
    trading; intraday-only avoids that decay.
    """

    params: LongStraddleParams
    # V5: long ATM CE + ATM PE — long-vol family (long vega + gamma).
    regime_family: str = "long_vol"

    # ─── Realistic-fill helpers ───────────────────────────────────────
    # Long straddle is a DEBIT spread on entry (BUY both legs at ASK)
    # and CREDIT on exit (SELL both legs at BID). The spread crosses
    # are paid on entry AND exit — 4 spread crosses per round trip.

    def _entry_fill_debit(self) -> float:
        """Net debit at entry: ce_ask + pe_ask (per share)."""
        _, ce_ask = self._bid_ask_for(self._ce_token)
        _, pe_ask = self._bid_ask_for(self._pe_token)
        return ce_ask + pe_ask

    def _exit_fill_credit(self) -> float:
        """Net credit at exit: ce_bid + pe_bid (per share)."""
        ce_bid, _ = self._bid_ask_for(self._ce_token)
        pe_bid, _ = self._bid_ask_for(self._pe_token)
        return ce_bid + pe_bid

    def __init__(self, strategy_id: str, params: LongStraddleParams):
        super().__init__(strategy_id, params)
        self._entered = False
        self._stopped_for_day = False
        self._regime: RegimeDetector | None = None
        self._paper_mode: bool = os.environ.get("PAPER_TRADING", "false").lower() == "true"
        # Strike + expiry
        self._strike: float = 0
        self._expiry_dt: date | None = None
        # CE leg
        self._ce_token: int = 0
        self._ce_symbol: str = ""
        # PE leg
        self._pe_token: int = 0
        self._pe_symbol: str = ""
        # Tracking
        self._entry_debit: Decimal = Decimal("0")
        self._peak_value: Decimal = Decimal("0")
        self._lot_size: int = LOT_SIZES.get(params.underlying, 75)
        self._quantity: int = params.quantity_lots * self._lot_size

    def get_subscriptions(self) -> Subscription:
        return Subscription(instrument_tokens=[], timeframes=[])

    @property
    def _expiry(self) -> date | None:
        # Alias for base helpers expecting `_expiry`
        return self._expiry_dt

    async def on_start(self) -> None:
        self._expiry_dt = self.ctx.next_expiry(self.params.underlying)
        self._regime = RegimeDetector(
            self.ctx._feed, self.ctx._aggregator, self.ctx._chain_builder,
            clock=self.ctx.clock,
        )

        # May 6 2026: warm up the daily-close history when the v2b gate
        # is enabled. Mirrors iron_condor.py / long_calendar.py warmup
        # pattern. VRP needs 21+ daily closes to compute; without
        # warmup the gate returns "insufficient_data" perpetually
        # because the launchd daily restart resets the in-memory deque
        # every morning. Failure is non-fatal — falls back to gradual
        # in-memory accumulation.
        if getattr(self.params, "require_long_vol_regime_v2b", False):
            spot_token = self.ctx.get_spot_token(self.params.underlying)
            if spot_token is None:
                logger.warning(
                    f"[{self.strategy_id}] No spot token for {self.params.underlying} — "
                    f"v2b long-vol regime warmup skipped"
                )
            else:
                seeded = await self._regime.warmup_daily_closes(
                    self.ctx.get_historical_data,
                    self.params.underlying,
                    spot_token,
                )
                logger.info(
                    f"[{self.strategy_id}] v2b long-vol warmup: "
                    f"seeded {seeded} daily closes for {self.params.underlying}"
                )

        logger.info(
            f"[{self.strategy_id}] Started: {self.params.underlying} "
            f"expiry={self._expiry_dt} lots={self.params.quantity_lots}"
        )

    async def on_tick(self, tick: Tick) -> Signal | None:
        now = self.ctx.clock.now()

        # Capture morning VIX for any opt-in filters inherited from base
        self._capture_morning_vix_if_needed()

        # Expiry rollover — detect new expiry on Mondays after Friday expiry
        new_expiry = self._check_expiry_rollover(self._expiry_dt, self.params.underlying)
        if new_expiry:
            self._expiry_dt = new_expiry
            self._entered = False
            self._stopped_for_day = False

        # End-of-day exit
        if now.time() >= self.params.exit_time and self._entered:
            self._stopped_for_day = True
            return self._create_exit_signal("Exit time reached")

        # Expiry-day buffer — close before final-hour gamma trap
        if (
            self._entered
            and self._expiry_dt == now.date()
            and now.time() >= self.params.expiry_day_force_exit_at
        ):
            self._stopped_for_day = True
            return self._create_exit_signal(
                f"Expiry-day buffer reached {self.params.expiry_day_force_exit_at}"
            )

        if not self._entered and not self._stopped_for_day and now.time() >= self.params.entry_time:
            return await self._try_entry()

        if self._entered:
            return self._check_exit_conditions()

        return None

    async def _try_entry(self) -> Signal | None:
        """Score the setup, apply filters, build the straddle legs."""
        if self._expiry_dt is None:
            self._log_skip_throttled(
                "ENTRY_SKIP_NO_EXPIRY",
                f"[{self.strategy_id}] No expiry available",
            )
            return None

        # Expiry-day block — never open a fresh straddle on expiry day
        # (theta accelerates to ~100% of remaining premium, and any move
        # one direction is matched 1:1 by loss on the other leg, with
        # almost no time-value cushion).
        expiry_block = self._check_expiry_day_block(self.params.underlying)
        if expiry_block:
            self._log_skip_throttled(
                "ENTRY_SKIP_EXPIRY",
                f"[{self.strategy_id}] Entry skipped: {expiry_block}",
            )
            return None

        # May 6 2026: drive RegimeDetector.assess() so its
        # _maybe_capture_daily_close() side effect accumulates the
        # daily-close deque used by VRP. Without this call, LS's v2b
        # path never touches assess(), so _daily_closes stays empty
        # and the VRP gate returns "insufficient_data" forever in
        # backtests with no broker warmup. Same pattern as
        # long_calendar.py.
        if self._regime:
            self._regime.assess(self.params.underlying)

        # ─── May 6 2026: v2b regime-gate-only mode ───────────────────
        # Single-condition VRP < 0. When enabled, ALL legacy heuristic
        # filters are bypassed and entry is gated solely on the long-
        # vol detector. Same gate as LC v2b but applied to a different
        # structure (long straddle, not calendar).
        if getattr(self.params, "require_long_vol_regime_v2b", False):
            if not self._regime:
                self._log_skip_throttled(
                    "ENTRY_SKIP_REGIME_V2B_NO_DETECTOR",
                    f"[{self.strategy_id}] Entry skipped: regime detector unavailable",
                )
                return None
            ok, metrics = self._regime.is_long_vol_favorable_v2b(self.params.underlying)
            if not ok:
                self._log_skip_throttled(
                    "ENTRY_SKIP_REGIME_LV2B_GATE",
                    f"[{self.strategy_id}] Entry skipped: long-vol v2b gate "
                    f"{metrics.get('reason', '?')}",
                )
                return None
            # v2b mode: skip every legacy filter and proceed directly to chain selection.
        else:
            # ─── Legacy heuristic-filter pipeline (default behaviour) ─

            # VIX band
            vix_block = self._check_vix_filter()
            if vix_block:
                self._log_skip_throttled(
                    "ENTRY_SKIP_VIX",
                    f"[{self.strategy_id}] Entry skipped: {vix_block}",
                )
                return None

            # Phase 3b Gate B (intraday VIX spike) — opt-in
            spike_block = self._check_intraday_vix_spike_filter()
            if spike_block:
                self._log_skip_throttled(
                    "ENTRY_SKIP_VIX_SPIKE",
                    f"[{self.strategy_id}] Entry skipped: {spike_block}",
                )
                return None

            # PCR / max-pain default OFF (long-vega), but caller may override
            if self.params.pcr_filter_enabled:
                pcr_block = self._check_pcr_filter(self.params.underlying, self._expiry_dt)
                if pcr_block:
                    self._log_skip_throttled(
                        "ENTRY_SKIP_PCR",
                        f"[{self.strategy_id}] Entry skipped: {pcr_block}",
                    )
                    return None
            if self.params.max_pain_filter_enabled:
                mp_block = self._check_max_pain_filter(self.params.underlying, self._expiry_dt)
                if mp_block:
                    self._log_skip_throttled(
                        "ENTRY_SKIP_MP",
                        f"[{self.strategy_id}] Entry skipped: {mp_block}",
                    )
                    return None

        # Pull spot + chain
        spot = float(self.ctx.get_spot_price(self.params.underlying))
        if spot <= 0:
            return None
        chain = self.ctx.get_option_chain(self.params.underlying, self._expiry_dt)
        if chain is None or not chain.strikes:
            self._log_skip_throttled(
                "ENTRY_SKIP_NO_CHAIN",
                f"[{self.strategy_id}] Chain unavailable for expiry={self._expiry_dt}",
            )
            return None

        # Determine ATM strike (round to strike interval)
        strike_interval = 50 if self.params.underlying in ("NIFTY", "FINNIFTY") else 100
        offset_pts = spot * (self.params.strike_offset_pct / 100.0)
        target = spot + offset_pts
        target_strike = round(target / strike_interval) * strike_interval

        # Find both CE and PE legs at target strike
        ce_opt = self._find_leg(chain, target_strike, "CE")
        pe_opt = self._find_leg(chain, target_strike, "PE")
        if ce_opt is None or pe_opt is None:
            self._log_skip_throttled(
                "ENTRY_SKIP_NO_STRIKE",
                f"[{self.strategy_id}] Strike {target_strike} CE/PE not on chain",
            )
            return None

        # Liquidity gate — reject either leg whose bid-ask spread exceeds
        # max_spread_pct (BaseStrategyParams). Long straddle pays both
        # cross-spreads at entry AND exit, so wide quotes erode the
        # already-thin edge.
        liquidity_blocks: list[str] = []
        for opt, label in ((ce_opt, "CE"), (pe_opt, "PE")):
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

        # Build LIMIT-at-mid legs via base helper
        ce_leg = self._build_option_leg(
            ce_opt.tradingsymbol,
            ce_opt.instrument_token,
            OrderSide.BUY,  # long straddle = BUY both
            self._quantity,
            opt=ce_opt,
        )
        pe_leg = self._build_option_leg(
            pe_opt.tradingsymbol,
            pe_opt.instrument_token,
            OrderSide.BUY,
            self._quantity,
            opt=pe_opt,
        )
        if ce_leg is None or pe_leg is None:
            self._log_skip_throttled(
                "ENTRY_SKIP_PRICING",
                f"[{self.strategy_id}] STRADDLE BLOCKED: bid/ask missing on "
                f"CE or PE leg",
            )
            return None

        # Commit tokens before realistic-fill calculation reads bid/ask.
        self._ce_token = ce_opt.instrument_token
        self._pe_token = pe_opt.instrument_token
        realistic_debit = self._entry_fill_debit()
        net_debit = Decimal(str(round(realistic_debit, 2))) if realistic_debit > 0 else (
            ce_leg.price + pe_leg.price  # fallback when bid/ask missing
        )
        # Sanity check: an ATM straddle should cost something. If both
        # legs are zero or negative the chain is malformed — refuse.
        if net_debit <= Decimal("5"):
            self._log_skip_throttled(
                "ENTRY_SKIP_DEBIT",
                f"[{self.strategy_id}] Net debit {net_debit:.2f} <= 5 — "
                f"malformed chain (ce={ce_leg.price} pe={pe_leg.price})",
            )
            return None

        self._strike = target_strike
        self._ce_symbol = ce_opt.tradingsymbol
        self._pe_symbol = pe_opt.tradingsymbol
        self._entry_debit = net_debit
        self._peak_value = net_debit
        self._entered = True

        logger.info(
            f"[ENTRY] strategy={self.strategy_id} type=long_straddle "
            f"underlying={self.params.underlying} spot={spot:.2f} "
            f"strike={self._strike} "
            f"ce={self._ce_symbol}@{ce_leg.price} "
            f"pe={self._pe_symbol}@{pe_leg.price} "
            f"net_debit={net_debit:.2f} qty={self._quantity}"
        )
        self._log_decision(
            "ENTER",
            leg="STRADDLE",
            mode="long_straddle",
            entry_premium=float(net_debit),
            quantity=self._quantity,
        )

        return entry_signal(
            self.strategy_id,
            [ce_leg, pe_leg],
            f"Long straddle @ {self._strike}, debit={net_debit:.2f}",
        )

    @staticmethod
    def _find_leg(chain: OptionChain, strike: float, leg_type: str):
        """Return the OptionData (CE or PE) at the given strike, or None."""
        for entry in chain.strikes:
            if float(entry.strike) == strike:
                return entry.ce if leg_type == "CE" else entry.pe
        return None

    def _check_exit_conditions(self) -> Signal | None:
        """Continuous exit checks: profit target, stop loss."""
        if not self._entered:
            return None
        ce_ltp = self.ctx.get_ltp(self._ce_token)
        pe_ltp = self.ctx.get_ltp(self._pe_token)
        if ce_ltp <= 0 or pe_ltp <= 0:
            return None  # Pricing not available, hold position

        current_value_f = self._exit_fill_credit()
        current_value = Decimal(str(round(current_value_f, 2)))
        if current_value > self._peak_value:
            self._peak_value = current_value

        if self._entry_debit <= 0:
            return None
        change_pct = float((current_value - self._entry_debit) / self._entry_debit * 100)

        # Profit target
        if change_pct >= self.params.profit_target_pct:
            return self._create_exit_signal(
                f"Profit target: straddle up {change_pct:.1f}% "
                f"(entry={self._entry_debit:.2f} now={current_value:.2f})"
            )

        # Stop loss
        if change_pct <= -self.params.stop_loss_pct:
            return self._create_exit_signal(
                f"Stop loss: straddle down {abs(change_pct):.1f}% "
                f"(entry={self._entry_debit:.2f} now={current_value:.2f})"
            )

        # NOTE: long straddle does NOT have an "underlying moved too far"
        # stop — directional movement is the SOURCE of profit, opposite
        # of LC's "spot must stay near strike" requirement. The straddle
        # rides the move all the way to exit_time or profit-target trigger.

        return None

    def evaluate_score(self) -> int:
        """Score 0-100 for orchestrator comparison. Same approach as LC:
        rule-based on entry-filter eligibility, with a small VIX-fit bonus.
        """
        try:
            if self._expiry_dt is None:
                return 0
            if self._check_expiry_day_block(self.params.underlying):
                return 0
            if self._check_vix_filter():
                return 0
            vix = self.ctx.get_vix()
            if vix <= 0:
                return 0
            band = max(1.0, self.params.vix_entry_max - self.params.vix_entry_min)
            band_pos = (vix - self.params.vix_entry_min) / band
            # Best when VIX is at lower-third of band (vol expansion most likely)
            fit = max(0, 20 - int(abs(band_pos - 0.33) * 40))
            return 60 + fit
        except Exception:
            return 0

    async def on_stop(self) -> None:
        if self._entered:
            logger.info(f"[{self.strategy_id}] Stopping with open straddle position")

    def reset_day_state(self) -> None:
        """Reset intraday flags at start of new trading day.

        Long straddle is INTRADAY — both legs close before exit_time,
        and a fresh straddle is opened the next morning if the gate
        permits. Reset _entered so a new day can attempt an entry.
        """
        self._entered = False
        self._stopped_for_day = False

    def _create_exit_signal(self, reason: str) -> Signal:
        """Close both legs of the straddle — sell the bought CE and PE."""
        self._stopped_for_day = True
        ce_leg = make_leg(
            self._ce_symbol,
            self._ce_token,
            OrderSide.SELL,  # sell the bought CE
            self._quantity,
            order_type=OrderType.MARKET,
        )
        pe_leg = make_leg(
            self._pe_symbol,
            self._pe_token,
            OrderSide.SELL,  # sell the bought PE
            self._quantity,
            order_type=OrderType.MARKET,
        )
        # Realistic-fill exit: MARKET orders cross the spread, so use
        # ce_bid + pe_bid (selling at bid), not LTP midpoint.
        exit_value = Decimal(str(round(self._exit_fill_credit(), 2)))
        pnl = float(exit_value - self._entry_debit) * self._quantity
        logger.info(
            f"[EXIT] strategy={self.strategy_id} reason={reason} "
            f"entry_debit={self._entry_debit:.2f} exit_value={exit_value:.2f} "
            f"pnl={pnl:+.0f}"
        )
        self._log_decision(
            "EXIT",
            leg="STRADDLE",
            mode="long_straddle",
            entry_premium=float(self._entry_debit),
            quantity=self._quantity,
            exit_reason=reason,
            outcome_pnl=pnl,
        )
        self._entered = False
        return exit_signal(self.strategy_id, [ce_leg, pe_leg], reason)
