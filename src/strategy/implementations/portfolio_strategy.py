"""Unified Portfolio Strategy — two independent legs running in parallel.

Premium leg (IC/strangle): enters early on range-bound signal, profits from theta.
Trend leg (debit spread): enters on confirmed breakout, profits from direction.

Both legs run independently — they don't compete or block each other.
On a trending day: IC loses (capped by wings), debit spread profits → net positive.
On a range day: IC profits from theta, trend never triggers.

This is truly complementary — they hedge each other naturally.
"""

import logging
from datetime import date, datetime, time
from decimal import Decimal

from src.core.constants import LOT_SIZES
from src.core.models import Signal, SignalLeg, Subscription, Tick
from src.core.structured_logger import get_structured_logger
from src.core.types import OrderSide, OrderType, Timeframe
from src.options.chain_analyzer import get_high_oi_strikes
from src.strategy.base import BaseStrategy
from src.strategy.indicators import BreakoutSignal, momentum_breakout, oi_breakout_confirm
from src.strategy.params import PortfolioParams
from src.strategy.regime import MarketRegime, RegimeDetector
from src.strategy.registry import register_strategy
from src.advisor.confluence import (
    apply_confluence,
    load_day_bias,
    reset_confluence_log_dedup,
)
from src.advisor.models import DayBias
from src.strategy.decision_logger import DecisionLogger, DecisionSnapshot
from src.strategy.event_calendar import EventCalendar
from src.strategy.implementations import portfolio_strikes as _strikes
from src.strategy.implementations.portfolio_pricing import (
    find_available_wing_strike,
    resolve_option_price,
)
from src.strategy.implementations.portfolio_scoring import (
    ScoreBreakdown,
    compute_iv_rank,
    iv_rank_shadow_adj,
    load_iv_rank_baseline,
    score_premium_selling,
    score_trend_following_breakdown,
)
from src.strategy.signals import entry_signal, exit_signal
from src.utils.log_tags import Tag

logger = logging.getLogger(__name__)


# ─── Portfolio Strategy ───────────────────────────────────────────────

@register_strategy("portfolio", PortfolioParams)
class PortfolioStrategy(BaseStrategy):
    """Two independent legs: premium selling + trend following.

    Lifecycle per day:
    1. 9:15-9:30: Collect morning data
    2. 9:30+: Evaluate premium leg → enter IC/strangle if score ≥ threshold
    3. 10:00+: Evaluate trend leg → enter debit spread on breakout (independent)
    4. Both legs managed independently with own exit rules
    5. 15:15: Time exit any open positions
    6. Portfolio risk cap: exit both if combined loss > max
    """

    params: PortfolioParams

    def __init__(self, strategy_id: str, params: PortfolioParams):
        super().__init__(strategy_id, params)

        # Regime detector
        self._regime_detector: RegimeDetector | None = None
        self._expiry: date | None = None
        self._lot_size: int = LOT_SIZES.get(params.underlying, 75)
        self._base_quantity: int = params.quantity_lots * self._lot_size
        self._prem_quantity: int = self._base_quantity  # Set at premium entry
        self._trend_quantity: int = self._base_quantity  # Set at trend entry

        # ─── Premium leg state ─────────────────────────────────
        self._prem_entered = False
        self._prem_stopped = False
        self._prem_mode = "idle"  # "strangle" or "iron_condor"
        self._prem_score: int = 0
        self._short_ce_token: int = 0
        self._short_ce_symbol: str = ""
        self._short_pe_token: int = 0
        self._short_pe_symbol: str = ""
        self._long_ce_token: int = 0
        self._long_ce_symbol: str = ""
        self._long_pe_token: int = 0
        self._long_pe_symbol: str = ""
        self._entry_premium: Decimal = Decimal("0")
        self._peak_premium: Decimal = Decimal("0")
        self._prem_fill_pending: bool = False

        # ─── Trend leg state ──────────────────────────────────
        self._trend_entered = False
        self._trend_stopped = False
        self._trend_score: int = 0
        # Phase A logging cache (Apr 18 — score_validation_plan.md):
        # populated alongside self._trend_score every tick so _build_snapshot
        # can attach the per-factor breakdown + inputs to TREND decision rows
        # without re-running the score function.
        self._trend_breakdown: ScoreBreakdown | None = None
        self._trend_inputs: dict = {}
        self._trend_buy_token: int = 0
        self._trend_buy_symbol: str = ""
        self._trend_sell_token: int = 0
        self._trend_sell_symbol: str = ""
        self._trend_direction: str = ""
        self._entry_debit: Decimal = Decimal("0")
        self._max_spread_value: Decimal = Decimal("0")
        self._peak_spread_value: Decimal = Decimal("0")
        # Trend fill-pending mirrors _prem_fill_pending — entry_debit was
        # captured from chain quotes; once the OMS reports actual fills we
        # reconcile so SL/PT math is anchored to the price we really paid.
        # Pure structural fix; survived the Apr 18 partial-revert because
        # it doesn't depend on any statistical claim — kept as defensive.
        self._trend_fill_pending: bool = False

        # ─── Portfolio-level tracking ─────────────────────────
        self._day_pnl: float = 0.0

        # ─── Entry tracking (for attribution on exit) ──────────
        self._prem_entry_time: datetime | None = None
        self._prem_entry_spot: float = 0.0
        self._prem_entry_vix: float = 0.0
        self._prem_entry_greeks: dict = {}  # net {delta, gamma, theta, vega}

        self._trend_entry_time: datetime | None = None
        self._trend_entry_spot: float = 0.0
        self._trend_entry_vix: float = 0.0
        self._trend_entry_greeks: dict = {}

        # ─── AI Advisor confluence ─────────────────────────────────
        self._day_bias: DayBias | None = None
        self._confluence_enabled: bool = False
        self._confluence_weight: float = 1.0

        # ─── Event calendar (P1 #11 hard block) ─────────────────
        # Loaded in on_start so the CSV read doesn't happen inside __init__
        # (keeps pickle/import-time side effects at zero). None when the
        # hard-block flag is off so downstream code can cheap-check is None.
        self._event_calendar: EventCalendar | None = None
        # Friday square-off dedup — fire the log once per session, not per
        # tick from 14:55 to 15:15.
        self._friday_squareoff_logged: bool = False

        # ─── Day-level leg P&L tracking ─────────────────────────
        self._prem_realized_pnl: float = 0.0
        self._trend_realized_pnl: float = 0.0
        self._prem_trades_today: int = 0
        self._trend_trades_today: int = 0
        self._last_monitor_minute: int = -1  # Combined unrealized P&L

        # ─── IV Rank baseline (52-week VIX) — loaded in on_start ──
        # Used in shadow mode only — score is logged-but-not-applied so we
        # can correlate IV-Rank-low entries with outcomes before promoting
        # to a hard filter. Ported from trend-improvements cd21a10.
        self._iv_rank_52w_high: float = 0.0
        self._iv_rank_52w_low: float = 0.0

        # ─── VIX direction history (for trend scoring confirmation) ──
        # Rolling buffer of (timestamp, vix) sampled every 5 min. Used by
        # _evaluate_trend to detect rising vs falling VIX at entry time —
        # see score_trend_following's vix_prev branch. Cap at 12 readings
        # (=60 min lookback) so memory stays bounded.
        self._vix_history: list[tuple[datetime, float]] = []

        # ─── BankNifty morning range (for trend confirmation) ──
        # Built once per session from the first 3×M5 candles (9:15-9:30).
        # _evaluate_trend compares current BN spot to this range to gate
        # trend entries against sector divergence. Zero = not yet built.
        self._bn_morning_high: float = 0.0
        self._bn_morning_low: float = 0.0
        # Sticky sentinel — set once when BANKNIFTY isn't subscribed for
        # this deployment so we stop scanning _spot_tokens on every tick.
        self._bn_unavailable: bool = False
        # Throttle for IV_RANK_SHADOW log so it fires once per 5-min
        # boundary rather than once per tick within the boundary window.
        self._last_iv_rank_log_minute: int = -1

        # ─── Decision snapshot logger (ML data collection) ────
        # Replay strategy IDs end in "_replay" (set by ReplayBacktestEngine
        # at scripts/replay_23days.py). For replay we truncate the per-day
        # CSV on first write so re-running the same window doesn't pile
        # duplicate rows on top of prior runs — the Apr 17 file showed 6×
        # duplication from successive replays (Apr 18 2026 audit). Live
        # trading must keep append mode so a mid-day reconnect doesn't
        # lose decisions already written that morning.
        is_replay = strategy_id.endswith("_replay")
        self._decision_logger = DecisionLogger(truncate_per_session=is_replay)
        self._last_skip_minute: int = -1  # throttle SKIP logs to 1/5min

        # ─── Stale-tick sanity check (Apr 18 2026) ─────────────
        # Reject impossibly large spot moves between ticks. Concrete trigger:
        # the 13-day chain replay showed Apr 17 entered a TREND debit_spread
        # at spot=22505 followed 28 minutes later by an IC entry at spot=24268
        # — a phantom 7.8% move from corrupted recorder snapshots. Real NIFTY
        # has never moved 2% in a minute without circuit-breakers halting
        # trade, so any tick exceeding that ceiling is treated as bad data
        # (broker reconnect cache, stale ChainBuilder snapshot, partial Kite
        # WebSocket frame). On_tick refuses to advance state on such ticks.
        # Live and replay both pay this cost — the strategy code is shared.
        self._last_sane_spot: float = 0.0
        self._last_sane_spot_ts: datetime | None = None
        # Throttle the warn log so a sustained bad-data window doesn't spam.
        self._last_stale_log_minute: int = -1
        self._stale_ticks_today: int = 0
        # NOTE: `self._last_skip_log_minute` and `_log_skip_throttled` now
        # live on BaseStrategy (centralised so base.py's _check_expiry_day_block
        # and every other strategy share the same throttle). Calls below
        # (PREMIUM_EXPIRY / PREMIUM_VIX_LOW / PREMIUM_VIX_HIGH) use the
        # inherited helper.

        # ─── Paper trading: shadow blocking (log but don't block) ──
        # force=False because strategy __init__ is not a process boundary —
        # if a caller (test harness, A/B script) has pre-set os.environ,
        # respect it. Production main() still calls load_env() at startup
        # with the default force=True. See scripts/auto_auth.load_env
        # docstring for the full rationale.
        import os
        from scripts.auto_auth import load_env
        load_env(force=False)
        self._paper_mode = os.environ.get("PAPER_TRADING", "false").lower() == "true"

    def get_subscriptions(self) -> Subscription:
        return Subscription(instrument_tokens=[], timeframes=[])

    async def on_start(self) -> None:
        self._expiry = self.ctx.next_expiry(self.params.underlying)
        self._regime_detector = RegimeDetector(
            self.ctx._feed, self.ctx._aggregator, self.ctx._chain_builder
        )
        # IV Rank baseline — precomputed once from 6-month VIX CSV. Empty
        # tuple is safe; compute_iv_rank() returns None and the shadow
        # logger emits "iv_rank=unavailable" without affecting any score.
        self._iv_rank_52w_high, self._iv_rank_52w_low = load_iv_rank_baseline()
        if self._iv_rank_52w_high > 0:
            logger.info(
                f"[{self.strategy_id}] IV Rank baseline loaded: "
                f"52w_high={self._iv_rank_52w_high:.1f} 52w_low={self._iv_rank_52w_low:.1f}"
            )
        else:
            logger.warning(
                f"[{self.strategy_id}] IV Rank baseline unavailable — shadow logging disabled"
            )

        # Load AI advisor day bias (if available)
        self._load_day_bias()

        # Load event calendar when the hard-block flag is on. We keep the
        # instance None when disabled so the per-tick check is a single
        # attribute compare rather than a CSV-in-memory walk.
        if (
            getattr(self.params, "event_day_hard_block_enabled", False)
            and not getattr(self.params, "event_day_soft_penalty_only", False)
        ):
            try:
                self._event_calendar = EventCalendar(
                    csv_path=getattr(
                        self.params, "event_calendar_path", "data/event_days.csv",
                    ),
                )
            except Exception as exc:
                logger.warning(
                    f"[{self.strategy_id}] Event calendar load failed — "
                    f"falling back to soft-penalty path only: {exc}"
                )
                self._event_calendar = None

        logger.info(
            f"[{self.strategy_id}] Portfolio strategy started: "
            f"underlying={self.params.underlying} expiry={self._expiry} "
            f"premium_threshold={self.params.signal_threshold} "
            f"trend_threshold={self.params.trend_signal_threshold} "
            f"advisor={'active' if self._confluence_enabled else 'shadow'} "
            f"day_bias={'loaded' if self._day_bias else 'none'} "
            f"event_cal={'loaded' if self._event_calendar else 'off'} "
            f"friday_squareoff={'on' if getattr(self.params, 'friday_premium_squareoff_enabled', False) else 'off'}"
        )

    async def on_tick(self, tick: Tick) -> Signal | None:
        now = self.ctx.clock.now()

        # ─── Stale-tick / impossible-move guard ───────────────
        # Read spot once at the top of the tick. If the move from the last
        # accepted spot exceeds SANITY_MAX_MOVE_PCT_PER_MINUTE pro-rated by
        # elapsed seconds, refuse to advance any state and bail. Protects
        # against:
        #   • Live: Kite WebSocket reconnects that replay a stale cached LTP
        #     on a different ChainBuilder generation
        #   • Replay: chain-recorder corruption (Apr 17 2026: snapshots
        #     toggled between spot=22505 and spot=24278 within 28 min)
        # First tick of a session is always accepted (no baseline to compare).
        # Stop/exit logic still runs on subsequent good ticks — this only
        # rejects the bad tick itself, not the strategy's response cycle.
        SANITY_MAX_MOVE_PCT_PER_MINUTE = 2.0
        try:
            spot_now_dec = self.ctx.get_spot_price(self.params.underlying)
            spot_now = float(spot_now_dec) if spot_now_dec else 0.0
        except Exception:
            spot_now = 0.0
        if spot_now > 0:
            if self._last_sane_spot > 0 and self._last_sane_spot_ts is not None:
                dt_s = (now - self._last_sane_spot_ts).total_seconds()
                # Only police adjacent ticks; large gaps (overnight, market
                # halts, lunch on certain segments) are not data corruption.
                if 0 < dt_s <= 120:
                    move_pct = abs(spot_now - self._last_sane_spot) / self._last_sane_spot * 100
                    # Two-tier ceiling (Apr 20 fix). The per-second rate
                    # (2%/min ⇒ 0.033%/s) is correct for sustained moves but
                    # collapses to ~zero at sub-second tick cadence — and
                    # real bid/ask wiggle of 0.01% then trips on every tick.
                    # Live data on Apr 20 fired this guard 363 times/day at
                    # dt=sub-second / move=0.01-0.05%, blocking ~6 minutes
                    # of decision opportunities. Floor allowed at 0.10%
                    # (24pts at NIFTY 24000) — a single one-second 0.10%
                    # jump is normal market noise; the 2%/min ceiling still
                    # catches the sustained-corruption pattern (7.8% / 28min
                    # from the d522ce1 commit message: that pattern needs
                    # 0.28%/min sustained, way over both bounds).
                    allowed = max(
                        SANITY_MAX_MOVE_PCT_PER_MINUTE * (dt_s / 60.0),
                        0.10,
                    )
                    if move_pct > allowed:
                        self._stale_ticks_today += 1
                        cur_min = now.hour * 60 + now.minute
                        if self._last_stale_log_minute != cur_min:
                            self._last_stale_log_minute = cur_min
                            logger.warning(
                                f"[{self.strategy_id}] [STALE_TICK] rejecting spot={spot_now:.2f} "
                                f"prev={self._last_sane_spot:.2f} dt={dt_s:.0f}s "
                                f"move={move_pct:.2f}% > allowed={allowed:.2f}% "
                                f"(stale_today={self._stale_ticks_today})"
                            )
                        return None
            self._last_sane_spot = spot_now
            self._last_sane_spot_ts = now

        # Expiry rollover
        new_expiry = self._check_expiry_rollover(self._expiry, self.params.underlying)
        if new_expiry:
            self._expiry = new_expiry

        # Sample VIX every 5 minutes for direction detection at trend entry.
        # Bounded to 12 readings (~60 min lookback). Cheap — VIX is a single
        # ltp lookup. The `second < 10` clamp avoids double-sampling when
        # multiple ticks arrive in the same wall-clock minute. Dedup uses
        # full timestamp delta (≥60s gap required) rather than minute
        # equality — robust if cap or spacing ever changes.
        if now.minute % 5 == 0 and now.second < 10:
            vix_now = self.ctx.get_vix()
            if vix_now > 0:
                if not self._vix_history or (now - self._vix_history[-1][0]).total_seconds() >= 60:
                    self._vix_history.append((now, vix_now))
                    if len(self._vix_history) > 12:
                        self._vix_history.pop(0)

        # Build BankNifty morning range (9:15-9:30) from M5 candles. Once
        # built (>0), we never rebuild — the morning range is an immutable
        # session-anchor used by score_trend_following's BN confirmation.
        # Reset to 0 happens in reset_session() at session boundary.
        if not self._bn_morning_high:
            self._build_banknifty_morning_range()

        # Expiry-day force-exit (Apr 18 tech audit). The param
        # `expiry_day_force_exit_at` (default 14:30) was already defined on
        # BaseStrategyParams but never wired — entries were blocked on expiry
        # day, but anything carried in from yesterday or a missed gate kept
        # running into the 15:15 normal exit_time, eating the 14:30→15:15
        # gamma-vertical window. Hard exit at the configured time on expiry
        # day prevents 0DTE auto-exercise STT and gamma blow-ups. Pure
        # structural fix; survived the Apr 18 partial-revert.
        if (
            self._expiry
            and now.date() == self._expiry
            and now.time() >= self.params.expiry_day_force_exit_at
        ):
            if self._prem_entered:
                return self._exit_premium("Expiry-day force-exit")
            if self._trend_entered:
                return self._exit_trend("Expiry-day force-exit")
            # No positions — fall through.

        # ─── Friday 14:55 premium square-off (P1 #12) ──────────────
        # Force-flat all premium positions (strangle / IC / straddle) just
        # before 15:00 on Fridays. Weekend gap risk — event-driven Monday-
        # open jumps can move NIFTY >1% overnight (FOMC decisions announced
        # 23:30 IST Wed/Thu, RBI emergency actions, geopolitical shocks).
        # Minute-cadence backtests cannot model this cleanly, so we lean
        # on a hard time gate. Trend debit spreads are exempt because the
        # risk is directional (capped at debit paid), not gap-vulnerable.
        if (
            getattr(self.params, "friday_premium_squareoff_enabled", False)
            and EventCalendar.is_friday_for_premium(now.date())
            and now.time() >= getattr(self.params, "friday_squareoff_time", time(14, 55))
            and self._prem_entered
        ):
            if not self._friday_squareoff_logged:
                logger.info(
                    f"[{self.strategy_id}] [FRIDAY_SQUAREOFF] "
                    f"force-flat premium at {now.time().isoformat()} "
                    f"(mode={self._prem_mode})"
                )
                self._friday_squareoff_logged = True
            return self._exit_premium("Friday 14:55 square-off (weekend gap risk)")

        # Time exit — close all open legs
        if now.time() >= self.params.exit_time:
            if self._prem_entered:
                return self._exit_premium("Time exit")
            if self._trend_entered:
                return self._exit_trend("Time exit")
            return None

        # Not yet entry time
        if now.time() < self.params.entry_time:
            return None

        # ─── Premium leg ──────────────────────────────────────
        # Evaluate and enter (9:30+), manage if entered
        if self._prem_entered:
            signal = self._check_premium_exit()
            if signal:
                return signal
        elif not self._prem_stopped:
            # Paper mode: allow entries until 14:00 for data collection
            # Live mode: only 9:30-11:00
            prem_cutoff = time(14, 0) if self._paper_mode else time(11, 0)
            if now.time() < prem_cutoff:
                if now.time() >= time(11, 0) and self._paper_mode:
                    if not hasattr(self, '_late_entry_logged'):
                        logger.info(f"[{self.strategy_id}] [SHADOW_BLOCK] PREMIUM entry after 11:00 — entering anyway (paper mode)")
                        self._late_entry_logged = True
                signal = self._evaluate_premium(now)
                if signal:
                    return signal

        # ─── Trend leg (independent) ──────────────────────────
        # Evaluate from 10:00+, manage if entered
        if self._trend_entered:
            signal = self._check_trend_exit()
            if signal:
                return signal
        elif not self._trend_stopped and now.time() >= time(10, 0):
            # Paper mode: allow until 14:00 for data collection
            # Live mode: only 10:00-10:30 — late-morning breakouts in high-VIX
            # whipsaw too often (validated in 23-day chain replay, Apr 2026)
            trend_cutoff = time(14, 0) if self._paper_mode else time(10, 30)
            if now.time() < trend_cutoff:
                # Check every 5 minutes, not every tick
                cur_min = now.hour * 60 + now.minute
                if cur_min % 5 == 0 and getattr(self, '_last_trend_eval_min', -1) != cur_min:
                    self._last_trend_eval_min = cur_min
                    signal = self._evaluate_trend(now)
                    if signal:
                        return signal

        # ─── Periodic monitoring (every 5 min for theta/gamma calibration data) ──
        cur_min = now.hour * 60 + now.minute
        if (self._prem_entered or self._trend_entered) and cur_min % 5 == 0:
            if self._last_monitor_minute != cur_min:
                self._last_monitor_minute = cur_min
                self._log_position_monitor(now)

        return None

    # ─── Premium Leg Evaluation ───────────────────────────────

    def _evaluate_premium(self, now) -> Signal | None:
        """Score and enter premium selling if conditions are strong."""
        spot = float(self.ctx.get_spot_price(self.params.underlying))
        vix = self.ctx.get_vix()
        if spot <= 0 or vix <= 0:
            return None

        # ─── Event-day hard block (P1 #11) ────────────────────────
        # The legacy soft-penalty (see ~line 550 below) only reduces the
        # entry score by -5/-15/-25. That has never prevented the 3-5
        # blow-up days/year where short premium loses 5-10× daily expected
        # P&L on RBI/Fed/Budget shock moves. When the hard-block flag is on
        # and the calendar has a HARD_BLOCK entry for today, we skip the
        # premium leg entirely. Trend leg is NOT blocked — directional
        # debit spreads actually benefit from event-day volatility.
        if self._event_calendar is not None:
            hard, event_type = self._event_calendar.is_hard_blocked(now.date())
            if hard:
                self._log_skip_throttled(
                    f"EVENT_BLOCK:{event_type}",
                    f"[{self.strategy_id}] [EVENT_BLOCK] PREMIUM blocked — "
                    f"{event_type} on {now.date().isoformat()}",
                    extra={
                        "tag": Tag.FILTER,
                        "strategy": self.strategy_id,
                        "filter": "event_day_hard_block",
                        "event_type": event_type,
                        "action": "BLOCK",
                    },
                )
                return None

        # Per-day cap (Apr 18 2026 chain-replay diagnosis): without this,
        # the premium leg re-entered 14× on 2026-04-17 hour 11 when each
        # entry hit the -29.9% stop. Trend leg already has the same cap.
        if self._prem_trades_today >= self.params.premium_max_trades_per_day:
            if now.minute % 10 == 0:
                logger.info(
                    f"[{self.strategy_id}] PREMIUM blocked: per-day cap reached "
                    f"({self._prem_trades_today}/{self.params.premium_max_trades_per_day})"
                )
            return None

        # Hour-of-day gate (Apr 18 2026 chain-replay diagnosis): hours 11-12
        # win 4-12% with avg -₹415 to -₹531 across 69 trades; hours 13-14
        # win 100% with avg +₹255 to +₹349 across 23 trades. Block bad hours
        # rather than tune SL%/PT% (which would be overfitting to 16 days).
        if now.hour in self.params.premium_blocked_hours:
            if now.minute % 15 == 0:
                logger.info(
                    f"[{self.strategy_id}] PREMIUM blocked: hour={now.hour} "
                    f"in blocked_hours={self.params.premium_blocked_hours}"
                )
            return None

        regime = self._regime_detector.assess(self.params.underlying)

        # Regime-block paths run on every tick once a sustained regime is
        # detected — throttle to 1 line/min/key. The base.py
        # _log_skip_throttled helper handles the per-minute keying.
        if regime.regime == MarketRegime.EXTREME_VOL:
            if not self._paper_mode:
                self._prem_stopped = True
                logger.info(f"[{self.strategy_id}] PREMIUM SIT OUT: {regime.reason}")
                return None
            else:
                self._log_skip_throttled(
                    "SHADOW_BLOCK_REGIME_EXTREME",
                    f"[{self.strategy_id}] [SHADOW_BLOCK] PREMIUM would sit out: {regime.reason}",
                )

        if regime.regime == MarketRegime.CONFLICTED:
            if not self._paper_mode:
                logger.info(f"[{self.strategy_id}] PREMIUM REDUCED: regime conflict — {regime.reason}")
                self._prem_quantity = max(self._lot_size, self._base_quantity // 4)
            else:
                self._log_skip_throttled(
                    "SHADOW_BLOCK_REGIME_CONFLICT",
                    f"[{self.strategy_id}] [SHADOW_BLOCK] REGIME CONFLICT — would reduce to 25%: {regime.reason}",
                )

        if regime.regime == MarketRegime.CHOPPY:
            if not self._paper_mode:
                logger.info(f"[{self.strategy_id}] PREMIUM REDUCED: {regime.reason}")
                self._prem_quantity = max(self._lot_size, self._base_quantity // 2)
            else:
                self._log_skip_throttled(
                    "SHADOW_BLOCK_REGIME_CHOP",
                    f"[{self.strategy_id}] [SHADOW_BLOCK] CHOP detected — would reduce size: chop_score={regime.chop_score:.2f}",
                )

        # Hard filters (PCR + max-pain) — Apr 18 2026 wiring fix.
        # Pre-fix portfolio_strategy ignored these even though the params
        # default to enabled. Now gated behind `portfolio_filters_enabled`
        # so we can A/B replay before flipping the default. In paper mode
        # the filters log as [SHADOW_BLOCK] (mirrors iron_condor pattern)
        # so we still see what they WOULD have blocked.
        if self.params.portfolio_filters_enabled and self._expiry:
            pcr_block = self._check_pcr_filter(self.params.underlying, self._expiry)
            if pcr_block:
                if self._paper_mode:
                    self._log_skip_throttled(
                        "SHADOW_BLOCK_PREMIUM_PCR",
                        f"[{self.strategy_id}] [SHADOW_BLOCK] PREMIUM {pcr_block}",
                    )
                else:
                    logger.info(f"[{self.strategy_id}] PREMIUM blocked: {pcr_block}")
                    return None

            mp_block = self._check_max_pain_filter(self.params.underlying, self._expiry)
            if mp_block:
                if self._paper_mode:
                    self._log_skip_throttled(
                        "SHADOW_BLOCK_PREMIUM_MP",
                        f"[{self.strategy_id}] [SHADOW_BLOCK] PREMIUM {mp_block}",
                    )
                else:
                    logger.info(f"[{self.strategy_id}] PREMIUM blocked: {mp_block}")
                    return None

        chain = self.ctx.get_option_chain(self.params.underlying, self._expiry)
        pcr_oi = chain.pcr_oi if chain else 1.0
        dte = (self._expiry - self.ctx.clock.now().date()).days if self._expiry else 0
        is_expiry = self.ctx.clock.now().date() == self._expiry

        # Event calendar check — penalize premium selling on event days
        event_penalty = 0
        try:
            import json
            from pathlib import Path
            cal_path = Path("data/economic_calendar.json")
            if cal_path.exists():
                events = json.loads(cal_path.read_text())
                if isinstance(events, list):
                    today_str = now.date().isoformat()
                    tomorrow_str = (now.date() + __import__('datetime').timedelta(days=1)).isoformat()
                    for ev in events:
                        ev_date = ev.get("date", "")
                        if ev_date in (today_str, tomorrow_str):
                            impact = ev.get("impact", "").upper()
                            event_name = ev.get("event", "unknown")
                            if impact == "EXTREME":
                                event_penalty = -25
                            elif impact == "HIGH":
                                event_penalty = -15
                            elif impact == "MEDIUM":
                                event_penalty = -5
                            if event_penalty:
                                logger.info(
                                    f"[{self.strategy_id}] EVENT: {event_name} ({impact}) "
                                    f"penalty={event_penalty:+d}"
                                )
                                break
        except Exception:
            pass

        # Weekly cycle awareness (NIFTY expiry = Tuesday)
        # Best entry: Wed-Fri (fresh contracts). Risk zone: Mon PM + Tue (gamma).
        day_of_week = now.weekday()  # 0=Mon, 1=Tue
        weekly_penalty = 0
        if dte <= 1 and day_of_week == 0 and now.time() >= time(14, 0):
            # Monday afternoon with DTE=1 — gamma zone
            weekly_penalty = -15
            if not self._paper_mode:
                self._log_skip_throttled(
                    "WEEKLY_CYCLE_GAMMA",
                    f"[{self.strategy_id}] WEEKLY CYCLE: Mon PM DTE=1 — high gamma risk",
                )
            else:
                self._log_skip_throttled(
                    "SHADOW_BLOCK_WEEKLY_CYCLE",
                    f"[{self.strategy_id}] [SHADOW_BLOCK] WEEKLY CYCLE: would penalize -15 (Mon PM gamma)",
                )

        # IC mode when VIX >= strangle_vix_max (default 12) — wings protect
        will_use_ic = vix >= self.params.strangle_vix_max

        self._prem_score, reasons = score_premium_selling(
            vix=vix,
            morning_range_pct=regime.morning_range_pct,
            move_from_open_pct=regime.move_from_open_pct,
            pcr_oi=pcr_oi,
            is_expiry_day=is_expiry,
            dte=dte,
            ic_mode=will_use_ic,
        )

        # Apply weekly cycle + event penalties
        total_penalty = weekly_penalty + event_penalty
        if total_penalty:
            self._prem_score = max(0, self._prem_score + total_penalty)
            if weekly_penalty:
                reasons.append(f"weekly_cycle({weekly_penalty:+d})")
            if event_penalty:
                reasons.append(f"event({event_penalty:+d})")

        # Apply AI confluence adjustment. dedup_minute throttles the
        # IGNORED/shadow log lines to one-per-minute-per-gate so a
        # low-confidence morning doesn't flood the audit log (Apr 21
        # produced ~2,600 identical IGNORED(low_confidence) lines).
        rule_score = self._prem_score
        self._prem_score, _ = apply_confluence(
            self._prem_score, self._day_bias, "premium",
            enabled=self._confluence_enabled, weight=self._confluence_weight,
            dedup_minute=now.hour * 60 + now.minute,
        )

        is_phase1 = now.time() < time(10, 0)
        threshold = self.params.phase1_threshold if is_phase1 else 65

        # IV Rank shadow logging — compute hypothetical adj, do NOT apply.
        # Logged once per 5-min boundary (dedup'd via _last_iv_rank_log_minute)
        # so we can correlate IV-Rank-low entries with outcomes before
        # promoting to a hard filter. Without dedup this fires every tick
        # within the 5-min window (dozens to hundreds of identical lines).
        if now.minute % 5 == 0 and now.minute != self._last_iv_rank_log_minute:
            iv_rank = compute_iv_rank(vix, self._iv_rank_52w_high, self._iv_rank_52w_low)
            if iv_rank is not None:
                iv_adj, iv_reason = iv_rank_shadow_adj(iv_rank)
                shadow_score = max(0, self._prem_score + iv_adj)
                would_block = shadow_score < threshold and self._prem_score >= threshold
                logger.info(
                    f"[{self.strategy_id}] [IV_RANK_SHADOW] {iv_reason} "
                    f"would_adj={iv_adj:+d} actual_score={self._prem_score} "
                    f"shadow_score={shadow_score} "
                    f"{'WOULD_BLOCK' if would_block else 'no_block'}"
                )
            self._last_iv_rank_log_minute = now.minute

        if now.minute % 5 == 0:
            phase = "P1" if is_phase1 else "P2"
            ai_tag = f" ai_adj={self._prem_score - rule_score:+d}" if self._day_bias else ""
            logger.info(
                f"[{self.strategy_id}] PREMIUM {phase}: "
                f"score={self._prem_score}/100 [{', '.join(reasons)}] "
                f"threshold={threshold} VIX={vix:.1f}{ai_tag}"
            )

        ai_adj = self._prem_score - rule_score

        would_block = self._prem_score < threshold
        if would_block and self._paper_mode:
            logger.info(
                f"[{self.strategy_id}] [SHADOW_BLOCK] PREMIUM score={self._prem_score} < threshold={threshold} "
                f"— entering anyway (paper mode)"
            )

        if self._prem_score >= threshold or (would_block and self._paper_mode):
            signal = self._enter_premium(vix)
            if signal:
                self._decision_logger.log(self._build_snapshot(
                    "PREMIUM", "ENTER", mode=self._prem_mode,
                    rule_score=rule_score, ai_adj=ai_adj,
                    final_score=self._prem_score, threshold=threshold,
                    entry_premium=float(self._entry_premium), quantity=self._prem_quantity,
                    shadow_blocked=would_block,
                ))
                return signal
        elif now.minute % 5 == 0 and self._last_skip_minute != now.hour * 60 + now.minute:
            # Log SKIP once per 5 minutes (not every tick)
            self._last_skip_minute = now.hour * 60 + now.minute
            self._decision_logger.log(self._build_snapshot(
                "PREMIUM", "SKIP",
                rule_score=rule_score, ai_adj=ai_adj,
                final_score=self._prem_score, threshold=threshold,
            ))

        return None

    def _enter_premium(self, vix: float) -> Signal | None:
        """Route by Indian VIX band: no-trade <13, strangle 13-16, IC 16-22, no-trade >22."""
        # Expiry-day 0DTE block — premium leg never enters when today == expiry
        expiry_block = self._check_expiry_day_block(self.params.underlying)
        if expiry_block:
            self._log_skip_throttled(
                "PREMIUM_EXPIRY",
                f"[{self.strategy_id}] PREMIUM skipped: {expiry_block}",
            )
            return None
        if vix < self.params.strangle_vix_min:
            self._log_skip_throttled(
                "PREMIUM_VIX_LOW",
                f"[{self.strategy_id}] [VIX_GATE] PREMIUM blocked: VIX={vix:.1f} "
                f"< strangle_vix_min={self.params.strangle_vix_min} (complacency)",
            )
            return None
        if vix > self.params.ic_vix_max:
            self._log_skip_throttled(
                "PREMIUM_VIX_HIGH",
                f"[{self.strategy_id}] [VIX_GATE] PREMIUM blocked: VIX={vix:.1f} "
                f"> ic_vix_max={self.params.ic_vix_max} (event/crash zone)",
            )
            return None
        if vix < self.params.strangle_vix_max:
            return self._enter_strangle(vix)
        # VIX in IC band 16-22 — wings handle expansion; fall back to strangle if IC unavailable
        result = self._enter_iron_condor(vix)
        if result:
            return result
        return self._enter_strangle(vix)

    # ─── Trend Leg Evaluation ─────────────────────────────────

    def _evaluate_trend(self, now) -> Signal | None:
        """Score and enter trend following on confirmed breakout."""
        # Block trend entry on expiry day — near-zero DTE debit spreads have
        # massive gamma, negligible extrinsic value, and terrible risk/reward
        if self._expiry and now.date() == self._expiry:
            if not self._paper_mode:
                return None
            else:
                self._log_skip_throttled(
                    "SHADOW_BLOCK_TREND_EXPIRY",
                    f"[{self.strategy_id}] [SHADOW_BLOCK] TREND blocked on expiry day — entering anyway (paper mode)",
                )

        spot = float(self.ctx.get_spot_price(self.params.underlying))
        vix = self.ctx.get_vix()
        if spot <= 0 or vix <= 0:
            return None

        if vix < self.params.trend_vix_min:
            if not self._paper_mode:
                self._log_skip_throttled(
                    "TREND_VIX_LOW",
                    f"[{self.strategy_id}] [VIX_GATE] TREND blocked: VIX={vix:.1f} "
                    f"< trend_vix_min={self.params.trend_vix_min} (no vol, breakouts whipsaw)",
                )
                return None
            else:
                self._log_skip_throttled(
                    "SHADOW_BLOCK_TREND_VIX",
                    f"[{self.strategy_id}] [SHADOW_BLOCK] TREND VIX={vix:.1f} "
                    f"< trend_vix_min={self.params.trend_vix_min} — entering anyway (paper mode)",
                )

        breakout, oi_confirmed, trend_duration = self._assess_trend(spot)

        # Hard gate (Apr 2026): require ≥45min sustained — 30min "sustained"
        # breakouts whipsaw too often in high VIX (4 of 6 trend entries hit
        # -23% stop in 23-day chain replay). The score was passing them at
        # 30min via score_trend_following's +25 bonus; this gates that out.
        if breakout.direction and trend_duration < 45 and not self._paper_mode:
            if now.minute % 10 == 0:
                logger.info(
                    f"[{self.strategy_id}] TREND gated: sustained={trend_duration}min < 45min "
                    f"(breakout={breakout.direction} {breakout.strength:.2f}%)"
                )
            return None

        # VIX direction + BankNifty confirmation — both are no-ops when
        # the inputs aren't ready (vix_prev=0 or bn_confirming=None), so
        # behavior degrades to the original 4-factor scoring on cold start
        # or in NIFTY-only deployments.
        vix_prev = self._get_vix_prev_for_trend(lookback_minutes=20)
        bn_confirming = (
            self._get_banknifty_confirming(breakout.direction)
            if breakout.direction else None
        )
        self._trend_breakdown = score_trend_following_breakdown(
            breakout=breakout,
            oi_confirmed=oi_confirmed,
            trend_duration_minutes=trend_duration,
            vix=vix,
            vix_prev=vix_prev,
            banknifty_confirming=bn_confirming,
        )
        self._trend_score = self._trend_breakdown.score
        reasons = self._trend_breakdown.reasons
        # Cache inputs so _build_snapshot can emit them on the decision row
        # without re-deriving — Phase A of score_validation_plan.md.
        self._trend_inputs = {
            "breakout_strength": breakout.strength,
            "oi_confirmed": oi_confirmed,
            "trend_duration_minutes": trend_duration,
            "vix_prev": vix_prev,
            "banknifty_confirming": bn_confirming,
            "bn_data_available": not self._bn_unavailable,
        }

        # Apply AI confluence adjustment (see premium-leg call for the
        # dedup_minute rationale — same per-tick spam class).
        rule_score = self._trend_score
        self._trend_score, _ = apply_confluence(
            self._trend_score, self._day_bias, "trend",
            enabled=self._confluence_enabled, weight=self._confluence_weight,
            dedup_minute=now.hour * 60 + now.minute,
        )

        if now.minute % 10 == 0:
            ai_tag = f" ai_adj={self._trend_score - rule_score:+d}" if self._day_bias else ""
            logger.info(
                f"[{self.strategy_id}] TREND: "
                f"score={self._trend_score}/100 [{', '.join(reasons)}]{ai_tag}"
            )

        ai_adj = self._trend_score - rule_score
        # Use trend-specific threshold (default 50) — the trend score function
        # was rebalanced Apr 18 (max base 100 → 90), so the shared 60
        # threshold became materially restrictive. See params.py for the
        # rationale and scripts/analyze_score_rebalance_impact.py for the data.
        trend_threshold = self.params.trend_signal_threshold

        trend_would_block = self._trend_score < trend_threshold
        if trend_would_block and self._paper_mode:
            logger.info(
                f"[{self.strategy_id}] [SHADOW_BLOCK] TREND score={self._trend_score} < threshold={trend_threshold} "
                f"— entering anyway (paper mode)"
            )

        if self._trend_score >= trend_threshold or (trend_would_block and self._paper_mode and breakout.direction):
            signal = self._enter_trend(breakout, spot)
            if signal:
                self._decision_logger.log(self._build_snapshot(
                    "TREND", "ENTER", mode="debit_spread",
                    rule_score=rule_score, ai_adj=ai_adj,
                    final_score=self._trend_score, threshold=trend_threshold,
                    entry_premium=float(self._entry_debit), quantity=self._trend_quantity,
                    shadow_blocked=trend_would_block,
                ))
                return signal

        return None

    def _assess_trend(self, spot: float) -> tuple[BreakoutSignal, bool, int]:
        """Gather breakout, OI confirmation, and trend duration."""
        spot_token = self._find_spot_token()
        no_breakout = BreakoutSignal(
            direction=None, strength=0.0, breakout_level=0.0,
            morning_high=0.0, morning_low=0.0,
        )

        if not spot_token:
            return no_breakout, False, 0

        candles = self.ctx.get_candles(spot_token, Timeframe.M5, limit=50)
        if len(candles) < 4:
            return no_breakout, False, 0

        breakout = momentum_breakout(
            candles,
            morning_candles=3,
            confirmation_pct=self.params.breakout_confirmation_pct,
        )

        if not breakout.direction:
            return breakout, False, 0

        # OI confirmation
        oi_confirmed = False
        chain = self.ctx.get_option_chain(self.params.underlying, self._expiry)
        if chain and chain.strikes:
            high_oi = get_high_oi_strikes(chain)
            oi_confirmed = oi_breakout_confirm(high_oi, spot, breakout.direction)

        # Trend duration
        trend_mins = 0
        direction = breakout.direction
        for c in reversed(candles[3:]):
            if direction == "UP" and float(c.close) > float(c.open):
                trend_mins += 5
            elif direction == "DOWN" and float(c.close) < float(c.open):
                trend_mins += 5
            else:
                break

        return breakout, oi_confirmed, trend_mins

    # ─── Premium Entry ────────────────────────────────────────

    def _enter_strangle(self, vix: float) -> Signal | None:
        """Enter short strangle at delta-based strikes."""
        chain = self.ctx.get_option_chain(self.params.underlying, self._expiry)
        if not chain or not chain.strikes:
            logger.warning(f"[{self.strategy_id}] STRANGLE BLOCKED: no option chain (strikes={len(chain.strikes) if chain else 0})")
            return None

        qty = self._base_quantity
        if vix > 15:
            qty = max(self._lot_size, qty // 2)

        best_ce, best_pe = self._find_oi_validated_strikes(
            chain, self.params.premium_call_delta, self.params.premium_put_delta
        )
        if not best_ce or not best_ce.ce or not best_pe or not best_pe.pe:
            logger.warning(f"[{self.strategy_id}] STRANGLE BLOCKED: no valid strikes (ce={best_ce is not None} pe={best_pe is not None})")
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

        # Min-credit guard (Apr 18 2026): on 2026-04-08 a strangle entered
        # with effective premium ~₹0.30 because one leg's LTP was stale; the
        # SL math then reported "premium up 26282.1%" (₹76,875 loss on a
        # ₹10L pool). IC has had this guard since launch (line below at
        # _enter_iron_condor's "credit too low" check). Anything below
        # premium_min_entry_credit is treated as a stale tick — refuse entry.
        min_credit = Decimal(str(self.params.premium_min_entry_credit))
        if self._entry_premium < min_credit:
            logger.warning(
                f"[{self.strategy_id}] STRANGLE BLOCKED: entry premium "
                f"{self._entry_premium} < min_credit {min_credit} "
                f"(ce_ltp={ce_ltp} pe_ltp={pe_ltp}) — likely stale tick"
            )
            # Roll back the strike-token assignments we just made above so a
            # subsequent entry attempt this minute starts clean.
            self._short_ce_token = 0
            self._short_pe_token = 0
            self._short_ce_symbol = ""
            self._short_pe_symbol = ""
            self._entry_premium = Decimal("0")
            return None

        self._peak_premium = self._entry_premium
        self._prem_entered = True
        self._prem_mode = "strangle"
        self._prem_fill_pending = True  # Will reconcile from actual fills
        self._prem_quantity = qty
        self._prem_entry_time = self.ctx.clock.now()
        self._prem_entry_spot = float(self.ctx.get_spot_price(self.params.underlying))
        self._prem_entry_vix = vix
        self._prem_entry_greeks = self._capture_leg_greeks(self._get_premium_token_signs(), self._prem_quantity)
        self._prem_trades_today += 1

        g = self._prem_entry_greeks
        logger.info(
            f"[ENTRY] strategy={self.strategy_id} leg=PREMIUM mode=STRANGLE "
            f"CE@{float(best_ce.strike)} PE@{float(best_pe.strike)} "
            f"premium={self._entry_premium} qty={qty} "
            f"score={self._prem_score} VIX={vix:.1f}"
        )
        logger.info(
            "premium leg entry: strangle",
            extra={
                "tag": Tag.ENTRY_QUALITY,
                "strategy": self.strategy_id,
                "leg": "PREMIUM",
                "mode": "STRANGLE",
                "net_delta": round(g.get("delta", 0), 2),
                "net_gamma": round(g.get("gamma", 0), 5),
                "net_theta": round(g.get("theta", 0), 2),
                "net_vega": round(g.get("vega", 0), 2),
                "spot": round(self._prem_entry_spot, 2),
                "vix": round(vix, 2),
                "premium_per_lot": round(float(self._entry_premium), 2),
            },
        )

        get_structured_logger().log(
            "ENTRY", strategy_id=self.strategy_id, leg="PREMIUM", mode="STRANGLE",
            ce_strike=float(best_ce.strike), pe_strike=float(best_pe.strike),
            premium=float(self._entry_premium), qty=qty, score=self._prem_score,
            vix=vix, spot=self._prem_entry_spot,
            delta=g.get("delta", 0), gamma=g.get("gamma", 0),
            theta=g.get("theta", 0), vega=g.get("vega", 0),
        )

        # F1: LIMIT-at-mid. If either leg can't be priced, abort entry —
        # a half-built strangle isn't worth the asymmetric risk.
        ce_leg = self._build_option_leg(
            self._short_ce_symbol, self._short_ce_token, OrderSide.SELL, qty, opt=best_ce.ce,
        )
        pe_leg = self._build_option_leg(
            self._short_pe_symbol, self._short_pe_token, OrderSide.SELL, qty, opt=best_pe.pe,
        )
        if ce_leg is None or pe_leg is None:
            logger.warning(
                f"[{self.strategy_id}] STRANGLE BLOCKED: could not price one or both legs"
            )
            self._prem_entered = False
            return None
        return entry_signal(self.strategy_id, [ce_leg, pe_leg],
            f"Portfolio premium strangle: score={self._prem_score}")

    def _enter_iron_condor(self, vix: float) -> Signal | None:
        """Enter iron condor with delta shorts + fixed wings."""
        chain = self.ctx.get_option_chain(self.params.underlying, self._expiry)
        if not chain or not chain.strikes:
            logger.warning(f"[{self.strategy_id}] IC BLOCKED: no option chain (strikes={len(chain.strikes) if chain else 0})")
            return None

        step = 50 if self.params.underlying in ("NIFTY", "FINNIFTY") else 100

        qty = self._base_quantity
        if vix > 18:
            qty = max(self._lot_size, qty // 2)

        # Low VIX: wider deltas to get further OTM with fatter credit
        if vix < 14:
            target_ce_delta, target_pe_delta = 0.10, -0.10
        else:
            target_ce_delta = self.params.ic_short_call_delta
            target_pe_delta = self.params.ic_short_put_delta

        best_ce, best_pe = self._find_oi_validated_strikes(chain, target_ce_delta, target_pe_delta)
        if not best_ce or not best_ce.ce or not best_pe or not best_pe.pe:
            logger.warning(f"[{self.strategy_id}] IC BLOCKED: no valid strikes (ce={best_ce is not None} pe={best_pe is not None})")
            return None

        desired_wing_offset = self.params.ic_wing_width_strikes * step

        # Clamp wings inward (Apr 2026 fix): with 8-strike wings (400pts on
        # NIFTY), the desired wing often lands outside the recorded chain's
        # strike range. find_available_wing_strike walks inward from the
        # desired offset until it finds a strike that exists AND has a
        # priceable leg. A narrower-than-target IC is better than no IC.
        long_ce_entry, ce_wing_offset = find_available_wing_strike(
            chain, float(best_ce.strike), desired_wing_offset, direction=+1, opt_attr="ce", strike_step=step,
        )
        long_pe_entry, pe_wing_offset = find_available_wing_strike(
            chain, float(best_pe.strike), desired_wing_offset, direction=-1, opt_attr="pe", strike_step=step,
        )

        if not long_ce_entry or not long_pe_entry:
            logger.warning(
                f"[{self.strategy_id}] IC BLOCKED: no available wings within {desired_wing_offset}pts "
                f"(short CE@{float(best_ce.strike)} found={long_ce_entry is not None}, "
                f"short PE@{float(best_pe.strike)} found={long_pe_entry is not None})"
            )
            return None  # Don't fallback — caller handles it

        # Use the *narrower* of the two wings for symmetric reporting; keeps
        # max-loss math conservative (assumes both wings the worst-case width).
        wing_offset = min(ce_wing_offset, pe_wing_offset)
        if ce_wing_offset != desired_wing_offset or pe_wing_offset != desired_wing_offset:
            logger.info(
                f"[{self.strategy_id}] IC WING CLAMPED: desired={desired_wing_offset}pts "
                f"actual CE={ce_wing_offset}pts PE={pe_wing_offset}pts"
            )

        self._short_ce_token = best_ce.ce.instrument_token
        self._short_ce_symbol = best_ce.ce.tradingsymbol
        self._short_pe_token = best_pe.pe.instrument_token
        self._short_pe_symbol = best_pe.pe.tradingsymbol
        self._long_ce_token = long_ce_entry.ce.instrument_token
        self._long_ce_symbol = long_ce_entry.ce.tradingsymbol
        self._long_pe_token = long_pe_entry.pe.instrument_token
        self._long_pe_symbol = long_pe_entry.pe.tradingsymbol

        short_prem = self.ctx.get_ltp(self._short_ce_token) + self.ctx.get_ltp(self._short_pe_token)
        long_prem = self.ctx.get_ltp(self._long_ce_token) + self.ctx.get_ltp(self._long_pe_token)
        self._entry_premium = short_prem - long_prem
        self._peak_premium = self._entry_premium

        # Minimum credit check — Apr 18 mirror of strangle's
        # premium_min_entry_credit rollback. Without resetting tokens/symbols/
        # premium, a subsequent _check_premium_exit would see _prem_entered=False
        # (good) but the leg symbols still populated — and a future ENTER would
        # skip overwriting them in some code paths. Belt-and-suspenders: clear
        # everything. Pure structural fix; survived the partial-revert.
        min_credit = Decimal(str(self.params.ic_min_entry_credit))
        if self._entry_premium < min_credit:
            logger.warning(
                f"[{self.strategy_id}] IC BLOCKED: credit={self._entry_premium} "
                f"< ic_min_entry_credit={min_credit} (likely stale wing fills)"
            )
            self._short_ce_token = 0
            self._short_pe_token = 0
            self._long_ce_token = 0
            self._long_pe_token = 0
            self._short_ce_symbol = ""
            self._short_pe_symbol = ""
            self._long_ce_symbol = ""
            self._long_pe_symbol = ""
            self._entry_premium = Decimal("0")
            self._peak_premium = Decimal("0")
            return None

        self._prem_entered = True
        self._prem_mode = "iron_condor"
        self._prem_fill_pending = True  # Will reconcile from actual fills
        self._prem_quantity = qty
        self._prem_entry_time = self.ctx.clock.now()
        self._prem_entry_spot = float(self.ctx.get_spot_price(self.params.underlying))
        self._prem_entry_vix = vix
        self._prem_entry_greeks = self._capture_leg_greeks(self._get_premium_token_signs(), self._prem_quantity)
        self._prem_trades_today += 1

        g = self._prem_entry_greeks
        logger.info(
            f"[ENTRY] strategy={self.strategy_id} leg=PREMIUM mode=IRON_CONDOR "
            f"short CE@{float(best_ce.strike)} PE@{float(best_pe.strike)} "
            f"wings ±{wing_offset}pts credit={self._entry_premium} qty={qty} "
            f"score={self._prem_score} VIX={vix:.1f}"
        )
        logger.info(
            "premium leg entry: iron condor",
            extra={
                "tag": Tag.ENTRY_QUALITY,
                "strategy": self.strategy_id,
                "leg": "PREMIUM",
                "mode": "IRON_CONDOR",
                "net_delta": round(g.get("delta", 0), 2),
                "net_gamma": round(g.get("gamma", 0), 5),
                "net_theta": round(g.get("theta", 0), 2),
                "net_vega": round(g.get("vega", 0), 2),
                "spot": round(self._prem_entry_spot, 2),
                "vix": round(vix, 2),
                "credit_per_lot": round(float(self._entry_premium), 2),
                "wing_width_pts": wing_offset,
            },
        )

        get_structured_logger().log(
            "ENTRY", strategy_id=self.strategy_id, leg="PREMIUM", mode="IRON_CONDOR",
            ce_strike=float(best_ce.strike), pe_strike=float(best_pe.strike),
            premium=float(self._entry_premium), qty=qty, score=self._prem_score,
            vix=vix, spot=self._prem_entry_spot, wing_offset=wing_offset,
            delta=g.get("delta", 0), gamma=g.get("gamma", 0),
            theta=g.get("theta", 0), vega=g.get("vega", 0),
        )

        # F1: LIMIT-at-mid. All 4 legs must price — an iron condor with a
        # missing wing is a naked short, so abort on any pricing miss.
        short_ce_leg = self._build_option_leg(
            self._short_ce_symbol, self._short_ce_token, OrderSide.SELL, qty, opt=best_ce.ce,
        )
        short_pe_leg = self._build_option_leg(
            self._short_pe_symbol, self._short_pe_token, OrderSide.SELL, qty, opt=best_pe.pe,
        )
        long_ce_leg = self._build_option_leg(
            self._long_ce_symbol, self._long_ce_token, OrderSide.BUY, qty, opt=long_ce_entry.ce,
        )
        long_pe_leg = self._build_option_leg(
            self._long_pe_symbol, self._long_pe_token, OrderSide.BUY, qty, opt=long_pe_entry.pe,
        )
        if any(leg is None for leg in (short_ce_leg, short_pe_leg, long_ce_leg, long_pe_leg)):
            logger.warning(
                f"[{self.strategy_id}] IC BLOCKED: could not price all 4 legs"
            )
            self._prem_entered = False
            return None
        return entry_signal(self.strategy_id,
            [short_ce_leg, short_pe_leg, long_ce_leg, long_pe_leg],
            f"Portfolio premium IC: score={self._prem_score}")

    # ─── Trend Entry ──────────────────────────────────────────

    def _enter_trend(self, breakout: BreakoutSignal, spot: float) -> Signal | None:
        """Enter debit spread on confirmed breakout."""
        chain = self.ctx.get_option_chain(self.params.underlying, self._expiry)
        if not chain or not chain.strikes:
            logger.warning(f"[{self.strategy_id}] TREND BLOCKED: no option chain (strikes={len(chain.strikes) if chain else 0})")
            return None

        step = 50 if self.params.underlying in ("NIFTY", "FINNIFTY") else 100
        width = self.params.trend_spread_width_strikes * step
        atm = float(chain.atm_strike)

        if breakout.direction == "UP":
            buy_strike = atm + step
            sell_strike = buy_strike + width
            opt_attr = "ce"
        else:
            buy_strike = atm - step
            sell_strike = buy_strike - width
            opt_attr = "pe"

        buy_entry = sell_entry = None
        for entry in chain.strikes:
            s = float(entry.strike)
            if s == buy_strike:
                buy_entry = entry
            elif s == sell_strike:
                sell_entry = entry

        if not buy_entry or not sell_entry:
            logger.warning(
                f"[{self.strategy_id}] TREND BLOCKED: strikes not in chain "
                f"(buy@{buy_strike}={buy_entry is not None} sell@{sell_strike}={sell_entry is not None} "
                f"chain_range={float(chain.strikes[0].strike)}-{float(chain.strikes[-1].strike)})"
            )
            return None

        buy_opt = getattr(buy_entry, opt_attr)
        sell_opt = getattr(sell_entry, opt_attr)
        if not buy_opt or not sell_opt:
            logger.warning(f"[{self.strategy_id}] TREND BLOCKED: option data missing for {opt_attr} legs")
            return None

        self._trend_buy_token = buy_opt.instrument_token
        self._trend_buy_symbol = buy_opt.tradingsymbol
        self._trend_sell_token = sell_opt.instrument_token
        self._trend_sell_symbol = sell_opt.tradingsymbol
        self._trend_direction = breakout.direction

        # Resolve fillable prices with bid/ask fallback (Apr 2026 fix):
        # ITM legs often have stale LTP=0 in recorded chain when no trade
        # printed in that minute, but real exchanges quote bid/ask continuously.
        # resolve_option_price tries LTP → mid → side-aware quote.
        buy_price = resolve_option_price(buy_opt, "BUY")
        sell_price = resolve_option_price(sell_opt, "SELL")

        if buy_price is None or sell_price is None:
            logger.warning(
                f"[{self.strategy_id}] TREND BLOCKED: no quotes "
                f"(buy@{buy_strike} ltp={buy_opt.ltp} bid={buy_opt.bid_price} ask={buy_opt.ask_price}; "
                f"sell@{sell_strike} ltp={sell_opt.ltp} bid={sell_opt.bid_price} ask={sell_opt.ask_price})"
            )
            return None

        self._entry_debit = buy_price - sell_price
        self._max_spread_value = Decimal(str(abs(width)))
        self._peak_spread_value = self._entry_debit

        if self._entry_debit <= 0:
            # Genuine inversion (buy leg cheaper than sell leg) — would be
            # arbitrage. Don't enter; this means our spread direction is wrong
            # for the breakout, or both quotes are noise.
            logger.warning(
                f"[{self.strategy_id}] TREND BLOCKED: debit non-positive "
                f"(buy={buy_price} sell={sell_price} debit={self._entry_debit})"
            )
            return None

        self._trend_entered = True
        self._trend_fill_pending = True  # Reconcile entry_debit from real fills
        self._trend_quantity = self._base_quantity
        self._trend_entry_time = self.ctx.clock.now()
        self._trend_entry_spot = spot
        self._trend_entry_vix = self.ctx.get_vix()
        self._trend_entry_greeks = self._capture_leg_greeks(self._get_trend_token_signs(), self._trend_quantity)
        self._trend_trades_today += 1

        spread_type = "Bull Call" if breakout.direction == "UP" else "Bear Put"
        g = self._trend_entry_greeks
        logger.info(
            f"[ENTRY] strategy={self.strategy_id} leg=TREND "
            f"{spread_type} buy@{buy_strike} sell@{sell_strike} "
            f"debit={self._entry_debit} max={self._max_spread_value} "
            f"qty={self._trend_quantity} score={self._trend_score}"
        )
        logger.info(
            "trend leg entry: debit spread",
            extra={
                "tag": Tag.ENTRY_QUALITY,
                "strategy": self.strategy_id,
                "leg": "TREND",
                "direction": breakout.direction,
                "net_delta": round(g.get("delta", 0), 2),
                "net_gamma": round(g.get("gamma", 0), 5),
                "net_theta": round(g.get("theta", 0), 2),
                "net_vega": round(g.get("vega", 0), 2),
                "spot": round(spot, 2),
                "vix": round(self._trend_entry_vix, 2),
                "debit": round(float(self._entry_debit), 2),
                "max_profit": round(float(self._max_spread_value - self._entry_debit), 2),
            },
        )

        get_structured_logger().log(
            "ENTRY", strategy_id=self.strategy_id, leg="TREND", mode=spread_type.upper().replace(" ", "_"),
            direction=breakout.direction, buy_strike=buy_strike, sell_strike=sell_strike,
            debit=float(self._entry_debit), max_spread=float(self._max_spread_value),
            qty=self._trend_quantity, score=self._trend_score,
            spot=spot, vix=self._trend_entry_vix,
            delta=g.get("delta", 0), gamma=g.get("gamma", 0),
            theta=g.get("theta", 0), vega=g.get("vega", 0),
        )

        # F1: LIMIT-at-mid for both spread legs. Fall back to the already-
        # resolved buy/sell prices (via resolve_option_price LTP→mid→quote)
        # when bid/ask are absent, since the trend-leg ITM option often has
        # zero bid/ask in the recorded chain but a valid LTP.
        buy_leg = self._build_option_leg(
            self._trend_buy_symbol, self._trend_buy_token, OrderSide.BUY, self._trend_quantity, opt=buy_opt,
        )
        sell_leg = self._build_option_leg(
            self._trend_sell_symbol, self._trend_sell_token, OrderSide.SELL, self._trend_quantity, opt=sell_opt,
        )
        # Fallback to the resolve_option_price-derived prices (LTP cascade)
        # when a pure-mid wasn't available — trend legs are the ones with
        # the stalest bid/ask, and the existing resolve already honoured
        # side-aware quoting. This keeps trend entries trading on recorded
        # data where bid/ask are blank but LTP is good.
        if buy_leg is None and buy_price is not None:
            buy_leg = SignalLeg(
                tradingsymbol=self._trend_buy_symbol,
                instrument_token=self._trend_buy_token,
                order_side=OrderSide.BUY,
                quantity=self._trend_quantity,
                order_type=OrderType.LIMIT,
                price=Decimal(str(buy_price)),
            )
        if sell_leg is None and sell_price is not None:
            sell_leg = SignalLeg(
                tradingsymbol=self._trend_sell_symbol,
                instrument_token=self._trend_sell_token,
                order_side=OrderSide.SELL,
                quantity=self._trend_quantity,
                order_type=OrderType.LIMIT,
                price=Decimal(str(sell_price)),
            )
        if buy_leg is None or sell_leg is None:
            logger.warning(
                f"[{self.strategy_id}] TREND BLOCKED: could not price spread legs"
            )
            self._trend_entered = False
            return None
        return entry_signal(self.strategy_id, [buy_leg, sell_leg],
            f"Portfolio trend {breakout.direction}: score={self._trend_score}")

    # ─── Premium Exit ─────────────────────────────────────────

    def _check_premium_exit(self) -> Signal | None:
        """Monitor premium selling position."""
        if self._entry_premium <= 0:
            return None

        # Reconcile entry premium from actual fill prices (portfolio positions)
        if getattr(self, '_prem_fill_pending', False):
            positions = self.ctx.get_positions()
            if positions:
                fill_premium = Decimal("0")
                for pos in positions:
                    if pos.instrument_token == self._short_ce_token:
                        fill_premium += abs(pos.average_price)
                    elif pos.instrument_token == self._short_pe_token:
                        fill_premium += abs(pos.average_price)
                    elif self._prem_mode == "iron_condor":
                        if pos.instrument_token in (self._long_ce_token, self._long_pe_token):
                            fill_premium -= abs(pos.average_price)
                if fill_premium > 0 and fill_premium != self._entry_premium:
                    logger.info(
                        f"[{self.strategy_id}] PREMIUM fill reconciled: "
                        f"chain={float(self._entry_premium):.2f} -> fill={float(fill_premium):.2f}"
                    )
                    self._entry_premium = fill_premium
                    self._peak_premium = fill_premium
                    self._prem_fill_pending = False

        # Weekly time exit — close before expiry gamma zone
        # NIFTY expiry = Tuesday. Close by Monday 2:30 PM if DTE <= 1.
        now = self.ctx.clock.now()
        dte = (self._expiry - now.date()).days if self._expiry else 99
        if dte <= 1 and now.weekday() == 0 and now.time() >= time(14, 30):
            if not self._paper_mode:
                return self._exit_premium("Weekly time exit: Mon 2:30 PM, DTE=1 (gamma risk)")
            else:
                self._log_skip_throttled(
                    "SHADOW_BLOCK_WEEKLY_TIME_EXIT",
                    f"[{self.strategy_id}] [SHADOW_BLOCK] Would exit: weekly time stop Mon 2:30 PM",
                )

        short_cost = self.ctx.get_ltp(self._short_ce_token) + self.ctx.get_ltp(self._short_pe_token)
        if self._prem_mode == "iron_condor" and self._long_ce_token:
            long_val = self.ctx.get_ltp(self._long_ce_token) + self.ctx.get_ltp(self._long_pe_token)
            current_cost = short_cost - long_val
        else:
            current_cost = short_cost

        change_pct = float((current_cost - self._entry_premium) / self._entry_premium * 100)

        # Gamma-aware stop tightening
        sl_multiplier = 1.0
        gamma_tightened = False
        gamma_threshold = self.params.gamma_exit_threshold
        if gamma_threshold > 0:
            gamma_exp = self._get_gamma_exposure()
            is_expiry = self.ctx.clock.now().date() == self._expiry
            if is_expiry:
                gamma_exp *= self.params.gamma_expiry_multiplier
            if gamma_exp > gamma_threshold:
                sl_multiplier = 0.5
                gamma_tightened = True

        # Theta efficiency exit — diminishing returns, rising risk
        now = self.ctx.clock.now()
        if self.params.theta_gamma_min_ratio > 0 and now.minute % 5 == 0:
            cur_greeks = self._capture_leg_greeks(self._get_premium_token_signs(), self._prem_quantity)
            if abs(cur_greeks["gamma"]) > 1e-6:
                theta_gamma = abs(cur_greeks["theta"]) / abs(cur_greeks["gamma"])
                if theta_gamma < self.params.theta_gamma_min_ratio:
                    logger.info(
                        f"[THETA_EFF] strategy={self.strategy_id} "
                        f"theta={cur_greeks['theta']:.4f} gamma={cur_greeks['gamma']:.6f} "
                        f"ratio={theta_gamma:.1f} threshold={self.params.theta_gamma_min_ratio}"
                    )
                    return self._exit_premium(
                        f"Theta efficiency: ratio {theta_gamma:.1f} < {self.params.theta_gamma_min_ratio}"
                    )

        # Resolve exit thresholds — vol-scaled when opt-in, hardcoded otherwise.
        # When `vol_scaled_exits=False`, each helper returns the fallback
        # verbatim so behavior is unchanged by default. The IC high-VIX widen
        # and the gamma-tightening sl_multiplier are layered on top, same as
        # before — vol-scaling replaces the *base* threshold, not the modifiers.
        dte_prem = (
            (self._expiry - self.ctx.clock.now().date()).days
            if self._expiry else 7
        )

        # Profit target
        pt_fallback = (
            self.params.ic_profit_target_pct if self._prem_mode == "iron_condor"
            else self.params.premium_profit_target_pct
        )
        pt_pct = self._compute_vol_scaled_exit_pct("pt", dte_prem, fallback_pct=pt_fallback)
        if change_pct < 0 and abs(change_pct) >= pt_pct:
            return self._exit_premium(f"Profit target: premium decayed {abs(change_pct):.1f}%")

        # Stop loss (gamma-tightened, VIX-scaled for IC)
        if self._prem_mode == "iron_condor":
            base_sl = self._compute_vol_scaled_exit_pct(
                "sl", dte_prem, fallback_pct=self.params.ic_stop_loss_pct
            )
            # IC in high VIX: premiums are fatter so noise is larger — widen stop
            vix_now = self.ctx.get_vix()
            if vix_now > 20:
                base_sl = min(base_sl * 1.5, 80.0)  # 40% → 60%, capped at 80%
            sl_pct = base_sl * sl_multiplier
        else:
            base_sl = self._compute_vol_scaled_exit_pct(
                "sl", dte_prem, fallback_pct=self.params.premium_stop_loss_pct
            )
            sl_pct = base_sl * sl_multiplier
        if change_pct > sl_pct:
            tag = " (gamma-tightened)" if gamma_tightened else ""
            return self._exit_premium(f"Stop loss: premium up {change_pct:.1f}%{tag}")

        # Trailing stop — lock in gains after premium decays meaningfully
        trail_pct_base = self._compute_vol_scaled_exit_pct(
            "trail", dte_prem, fallback_pct=self.params.premium_trail_stop_pct
        )
        if trail_pct_base > 0:
            if current_cost < self._peak_premium:
                self._peak_premium = current_cost

            # Only trail after premium has decayed enough to confirm a real winner
            # Strangle: trail after 10% decay with 15% bounce
            # IC: trail after 25% decay with 20% bounce (wider — 4-leg net fluctuates more)
            decay_pct = float((self._entry_premium - self._peak_premium) / self._entry_premium * 100)
            if self._prem_mode == "iron_condor":
                min_decay_to_trail = 25.0  # Only trail after IC has decayed 25%
                trail_bounce = 20.0 * sl_multiplier
            else:
                min_decay_to_trail = 10.0
                trail_bounce = trail_pct_base * sl_multiplier

            if (
                decay_pct >= min_decay_to_trail
                and self._peak_premium > 0
                and self._can_activate_trail_stop(decay_pct)
            ):
                bounce = float((current_cost - self._peak_premium) / self._entry_premium * 100)
                if bounce > trail_bounce:
                    tag = " (gamma-tightened)" if gamma_tightened else ""
                    return self._exit_premium(f"Trail stop: bounced {bounce:.1f}% after {decay_pct:.0f}% decay{tag}")

        return None

    def _exit_premium(self, reason: str) -> Signal:
        """Close premium leg with P&L attribution."""
        # Compute P&L before clearing state
        short_cost = float(self.ctx.get_ltp(self._short_ce_token) + self.ctx.get_ltp(self._short_pe_token))
        if self._prem_mode == "iron_condor" and self._long_ce_token:
            long_val = float(self.ctx.get_ltp(self._long_ce_token) + self.ctx.get_ltp(self._long_pe_token))
            exit_cost = short_cost - long_val
        else:
            exit_cost = short_cost
        pnl = (float(self._entry_premium) - exit_cost) * self._prem_quantity
        self._prem_realized_pnl += pnl

        # F1: LIMIT-at-mid on exit too. We look up the current tick for each
        # token (feed is already cached). If any leg can't be priced, we
        # still send MARKET for THAT leg as a safety net — exiting is
        # time-critical (SL/trail already triggered), and leaving a position
        # open because a wing went 0-bid is worse than crossing the spread.
        # This is narrowly scoped: only the missing-quote exit leg degrades.
        def _exit_leg(sym: str, tok: int, side: OrderSide) -> SignalLeg:
            leg = self._build_option_leg(sym, tok, side, self._prem_quantity)
            if leg is not None:
                return leg
            logger.warning(
                f"[{self.strategy_id}] EXIT fallback to MARKET for {sym} "
                f"— no bid/ask available, exit path must not stall"
            )
            return SignalLeg(
                tradingsymbol=sym,
                instrument_token=tok,
                order_side=side,
                quantity=self._prem_quantity,
                order_type=OrderType.MARKET,
            )
        legs = [
            _exit_leg(self._short_ce_symbol, self._short_ce_token, OrderSide.BUY),
            _exit_leg(self._short_pe_symbol, self._short_pe_token, OrderSide.BUY),
        ]
        if self._prem_mode == "iron_condor" and self._long_ce_token:
            legs.append(_exit_leg(self._long_ce_symbol, self._long_ce_token, OrderSide.SELL))
            legs.append(_exit_leg(self._long_pe_symbol, self._long_pe_token, OrderSide.SELL))

        logger.info(
            f"[EXIT] strategy={self.strategy_id} leg=PREMIUM mode={self._prem_mode.upper()} "
            f"reason={reason} pnl={pnl:+,.0f} "
            f"entry_prem={float(self._entry_premium):.2f} exit_cost={exit_cost:.2f}"
        )

        held_secs = (self.ctx.clock.now() - self._prem_entry_time).total_seconds() if self._prem_entry_time else 0
        get_structured_logger().log(
            "EXIT", strategy_id=self.strategy_id, leg="PREMIUM", mode=self._prem_mode.upper(),
            reason=reason, pnl=round(pnl, 2), entry_premium=float(self._entry_premium),
            exit_cost=exit_cost, qty=self._prem_quantity,
            held_minutes=round(held_secs / 60, 1),
            spot=float(self.ctx.get_spot_price(self.params.underlying)),
            vix=self.ctx.get_vix(),
        )

        self._log_exit_attribution("PREMIUM", self._prem_mode.upper(), reason, pnl)

        held_mins = int((self.ctx.clock.now() - self._prem_entry_time).total_seconds() / 60) if self._prem_entry_time else 0
        # Apr 20 audit: chain-vs-trade reconciliation showed the EXIT row
        # was leaving entry_premium=0.0 and quantity=0 because callers
        # only passed exit-side fields. Pass the entry context too so the
        # ML pipeline can compute P&L/share = outcome_pnl / quantity and
        # premium-decay% = (entry_premium - implied_exit) / entry_premium
        # directly from a single CSV row, without joining back to ENTER.
        self._decision_logger.log(self._build_snapshot(
            "PREMIUM", "EXIT", mode=self._prem_mode,
            entry_premium=float(self._entry_premium),
            quantity=self._prem_quantity,
            exit_reason=reason, outcome_pnl=round(pnl, 2), held_minutes=held_mins,
        ))

        self._prem_entered = False
        self._prem_stopped = True  # No re-entry same day — data shows re-entries are net negative
        self._prem_fill_pending = False
        return exit_signal(self.strategy_id, legs, reason)

    # ─── Trend Exit ───────────────────────────────────────────

    def _check_trend_exit(self) -> Signal | None:
        """Monitor trend position (debit spread)."""
        if self._entry_debit <= 0:
            return None

        # Reconcile entry debit from actual fills (Apr 18 structural fix).
        # Mirrors _check_premium_exit's _prem_fill_pending block. Buy leg
        # average_price is positive (we paid), sell leg average_price
        # represents credit received — net debit = abs(buy) - abs(sell).
        # Pure structural fix; survived the partial-revert.
        if getattr(self, '_trend_fill_pending', False):
            positions = self.ctx.get_positions()
            if positions:
                buy_fill = Decimal("0")
                sell_fill = Decimal("0")
                for pos in positions:
                    if pos.instrument_token == self._trend_buy_token:
                        buy_fill = abs(pos.average_price)
                    elif pos.instrument_token == self._trend_sell_token:
                        sell_fill = abs(pos.average_price)
                if buy_fill > 0 and sell_fill > 0:
                    fill_debit = buy_fill - sell_fill
                    if fill_debit > 0 and fill_debit != self._entry_debit:
                        logger.info(
                            f"[{self.strategy_id}] TREND fill reconciled: "
                            f"chain={float(self._entry_debit):.2f} -> fill={float(fill_debit):.2f}"
                        )
                        self._entry_debit = fill_debit
                        self._peak_spread_value = fill_debit
                    self._trend_fill_pending = False

        buy_ltp = self.ctx.get_ltp(self._trend_buy_token)
        sell_ltp = self.ctx.get_ltp(self._trend_sell_token)
        current_value = buy_ltp - sell_ltp

        if current_value > self._peak_spread_value:
            self._peak_spread_value = current_value

        # Resolve vol-scaled exits (opt-in) — see _compute_vol_scaled_exit_pct.
        dte_trend = (
            (self._expiry - self.ctx.clock.now().date()).days
            if self._expiry else 7
        )
        trend_pt_pct = self._compute_vol_scaled_exit_pct(
            "pt", dte_trend, fallback_pct=self.params.trend_profit_target_pct
        )
        trend_sl_pct = self._compute_vol_scaled_exit_pct(
            "sl", dte_trend, fallback_pct=self.params.trend_stop_loss_pct
        )
        trend_trail_pct = self._compute_vol_scaled_exit_pct(
            "trail", dte_trend, fallback_pct=self.params.trend_trailing_stop_pct
        )

        # Profit target
        if self._max_spread_value > 0:
            value_pct = float(current_value / self._max_spread_value * 100)
            if value_pct >= trend_pt_pct:
                return self._exit_trend(f"Trend profit: spread at {value_pct:.1f}% of max")

        # Stop loss
        if self._entry_debit > 0:
            loss_pct = float((self._entry_debit - current_value) / self._entry_debit * 100)
            if loss_pct >= trend_sl_pct:
                return self._exit_trend(f"Trend stop: lost {loss_pct:.1f}%")

        # Trailing stop — activates once spread reaches 30% of max profit
        # This prevents giving back large unrealized gains (e.g. Monday's +517 → -1527)
        if trend_trail_pct > 0 and self._max_spread_value > 0:
            max_profit = self._max_spread_value - self._entry_debit
            if max_profit > 0:
                current_profit = current_value - self._entry_debit
                if float(current_profit / max_profit * 100) >= 30:
                    if self._peak_spread_value > 0:
                        pullback = float(
                            (self._peak_spread_value - current_value) / self._peak_spread_value * 100
                        )
                        if pullback >= trend_trail_pct:
                            return self._exit_trend(f"Trend trail: pullback {pullback:.1f}%")

        return None

    def _exit_trend(self, reason: str) -> Signal:
        """Close trend leg with P&L attribution."""
        buy_ltp = float(self.ctx.get_ltp(self._trend_buy_token))
        sell_ltp = float(self.ctx.get_ltp(self._trend_sell_token))
        exit_value = buy_ltp - sell_ltp
        pnl = (exit_value - float(self._entry_debit)) * self._trend_quantity
        self._trend_realized_pnl += pnl

        # F1: LIMIT-at-mid on exit. Same fallback policy as premium exit —
        # degrade to MARKET per-leg only when bid/ask isn't available, rather
        # than block the whole exit.
        def _trend_exit_leg(sym: str, tok: int, side: OrderSide) -> SignalLeg:
            leg = self._build_option_leg(sym, tok, side, self._trend_quantity)
            if leg is not None:
                return leg
            logger.warning(
                f"[{self.strategy_id}] TREND EXIT fallback to MARKET for {sym} "
                f"— no bid/ask available"
            )
            return SignalLeg(
                tradingsymbol=sym,
                instrument_token=tok,
                order_side=side,
                quantity=self._trend_quantity,
                order_type=OrderType.MARKET,
            )
        legs = [
            _trend_exit_leg(self._trend_buy_symbol, self._trend_buy_token, OrderSide.SELL),
            _trend_exit_leg(self._trend_sell_symbol, self._trend_sell_token, OrderSide.BUY),
        ]

        logger.info(
            f"[EXIT] strategy={self.strategy_id} leg=TREND dir={self._trend_direction} "
            f"reason={reason} pnl={pnl:+,.0f} "
            f"entry_debit={float(self._entry_debit):.2f} exit_value={exit_value:.2f}"
        )

        held_secs = (self.ctx.clock.now() - self._trend_entry_time).total_seconds() if self._trend_entry_time else 0
        get_structured_logger().log(
            "EXIT", strategy_id=self.strategy_id, leg="TREND", direction=self._trend_direction,
            reason=reason, pnl=round(pnl, 2), entry_debit=float(self._entry_debit),
            exit_value=exit_value, qty=self._trend_quantity,
            held_minutes=round(held_secs / 60, 1),
            spot=float(self.ctx.get_spot_price(self.params.underlying)),
            vix=self.ctx.get_vix(),
        )

        self._log_exit_attribution("TREND", self._trend_direction, reason, pnl)

        held_mins = int((self.ctx.clock.now() - self._trend_entry_time).total_seconds() / 60) if self._trend_entry_time else 0
        # Mirror the PREMIUM exit fix (Apr 20): pass entry_debit + quantity
        # so an EXIT row alone is enough to derive P&L/share and decay%.
        self._decision_logger.log(self._build_snapshot(
            "TREND", "EXIT", mode="debit_spread",
            entry_premium=float(self._entry_debit),
            quantity=self._trend_quantity,
            exit_reason=reason, outcome_pnl=round(pnl, 2), held_minutes=held_mins,
        ))

        self._trend_entered = False
        self._trend_stopped = True
        self._trend_fill_pending = False
        return exit_signal(self.strategy_id, legs, reason)

    # ─── Logging & Attribution ─────────────────────────────────

    def _capture_leg_greeks(self, token_signs: dict[int, int], qty: int | None = None) -> dict:
        """Capture net position Greeks for a set of legs.

        Args:
            token_signs: {instrument_token: sign} where -1=short, +1=long
            qty: Position quantity. If None, uses base quantity.

        Returns:
            Net position Greeks: {delta, gamma, theta, vega} (Greeks × qty × sign)
        """
        chain = self.ctx.get_option_chain(self.params.underlying, self._expiry)
        if not chain:
            return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

        if qty is None:
            qty = self._base_quantity
        net = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

        for entry in chain.strikes:
            for opt in (entry.ce, entry.pe):
                if opt and opt.instrument_token in token_signs:
                    s = token_signs[opt.instrument_token]
                    net["delta"] += s * opt.greeks.delta * qty
                    net["gamma"] += s * opt.greeks.gamma * qty
                    net["theta"] += s * opt.greeks.theta * qty
                    net["vega"] += s * opt.greeks.vega * qty

        return net

    def _get_premium_token_signs(self) -> dict[int, int]:
        """Token → sign mapping for premium leg."""
        signs: dict[int, int] = {
            self._short_ce_token: -1,
            self._short_pe_token: -1,
        }
        if self._long_ce_token:
            signs[self._long_ce_token] = +1
        if self._long_pe_token:
            signs[self._long_pe_token] = +1
        return signs

    def _get_trend_token_signs(self) -> dict[int, int]:
        """Token → sign mapping for trend leg."""
        return {
            self._trend_buy_token: +1,
            self._trend_sell_token: -1,
        }

    def _log_position_monitor(self, now: datetime) -> None:
        """Log position health every 30 minutes — key diagnostic for paper trading."""
        spot = float(self.ctx.get_spot_price(self.params.underlying))
        vix = self.ctx.get_vix()

        if self._prem_entered and self._prem_entry_time:
            held_min = (now - self._prem_entry_time).total_seconds() / 60
            g = self._capture_leg_greeks(self._get_premium_token_signs(), self._prem_quantity)

            # Current premium cost
            short_cost = float(self.ctx.get_ltp(self._short_ce_token) + self.ctx.get_ltp(self._short_pe_token))
            if self._prem_mode == "iron_condor" and self._long_ce_token:
                long_val = float(self.ctx.get_ltp(self._long_ce_token) + self.ctx.get_ltp(self._long_pe_token))
                current_cost = short_cost - long_val
            else:
                current_cost = short_cost

            ep = float(self._entry_premium)
            change_pct = (current_cost - ep) / ep * 100 if ep > 0 else 0
            unrealized = (ep - current_cost) * self._prem_quantity
            gamma_exp = self._get_gamma_exposure()

            # Theta efficiency ratio
            theta_gamma = abs(g["theta"] / g["gamma"]) if abs(g["gamma"]) > 1e-6 else 999

            logger.info(
                "premium leg monitor",
                extra={
                    "tag": Tag.MONITOR,
                    "strategy": self.strategy_id,
                    "leg": "PREMIUM",
                    "mode": self._prem_mode.upper(),
                    "held_minutes": round(held_min, 1),
                    "pnl": round(unrealized, 2),
                    "change_pct": round(change_pct, 2),
                    "delta": round(g["delta"], 2),
                    "gamma": round(g["gamma"], 5),
                    "theta": round(g["theta"], 2),
                    "vega": round(g["vega"], 2),
                    "gamma_exposure": round(gamma_exp, 1),
                    "gamma_threshold": self.params.gamma_exit_threshold,
                    "theta_efficiency": round(theta_gamma, 2),
                    "spot": round(spot, 2),
                    "vix": round(vix, 2),
                },
            )

            get_structured_logger().log(
                "MONITOR", strategy_id=self.strategy_id, leg="PREMIUM",
                mode=self._prem_mode.upper(), held_minutes=round(held_min, 1),
                unrealized_pnl=round(unrealized, 2), change_pct=round(change_pct, 1),
                entry_premium=ep, current_cost=current_cost,
                delta=round(g["delta"], 2), gamma=round(g["gamma"], 5),
                theta=round(g["theta"], 2), vega=round(g["vega"], 2),
                gamma_exposure=round(gamma_exp, 1),
                gamma_threshold=self.params.gamma_exit_threshold,
                theta_gamma_ratio=round(theta_gamma, 2),
                spot=spot, vix=vix,
            )

        if self._trend_entered and self._trend_entry_time:
            held_min = (now - self._trend_entry_time).total_seconds() / 60
            g = self._capture_leg_greeks(self._get_trend_token_signs(), self._trend_quantity)

            buy_ltp = float(self.ctx.get_ltp(self._trend_buy_token))
            sell_ltp = float(self.ctx.get_ltp(self._trend_sell_token))
            current_value = buy_ltp - sell_ltp
            unrealized = (current_value - float(self._entry_debit)) * self._trend_quantity

            logger.info(
                "trend leg monitor",
                extra={
                    "tag": Tag.MONITOR,
                    "strategy": self.strategy_id,
                    "leg": "TREND",
                    "direction": self._trend_direction,
                    "held_minutes": round(held_min, 1),
                    "pnl": round(unrealized, 2),
                    "spread_value": round(current_value, 2),
                    "max_spread": round(float(self._max_spread_value), 2),
                    "delta": round(g["delta"], 2),
                    "gamma": round(g["gamma"], 5),
                    "theta": round(g["theta"], 2),
                    "vega": round(g["vega"], 2),
                    "spot": round(spot, 2),
                    "vix": round(vix, 2),
                },
            )

            get_structured_logger().log(
                "MONITOR", strategy_id=self.strategy_id, leg="TREND",
                direction=self._trend_direction, held_minutes=round(held_min, 1),
                unrealized_pnl=round(unrealized, 2),
                entry_debit=float(self._entry_debit), current_value=current_value,
                max_spread=float(self._max_spread_value),
                delta=round(g["delta"], 2), gamma=round(g["gamma"], 5),
                theta=round(g["theta"], 2), vega=round(g["vega"], 2),
                spot=spot, vix=vix,
            )

    def _log_exit_attribution(self, leg: str, mode: str, reason: str, actual_pnl: float) -> None:
        """Log Greeks P&L attribution on exit — decompose into delta/gamma/theta/vega."""
        if leg == "PREMIUM":
            entry_greeks = self._prem_entry_greeks
            entry_spot = self._prem_entry_spot
            entry_vix = self._prem_entry_vix
            entry_time = self._prem_entry_time
        else:
            entry_greeks = self._trend_entry_greeks
            entry_spot = self._trend_entry_spot
            entry_vix = self._trend_entry_vix
            entry_time = self._trend_entry_time

        if not entry_greeks or not entry_time:
            return

        now = self.ctx.clock.now()
        spot = float(self.ctx.get_spot_price(self.params.underlying))
        vix = self.ctx.get_vix()

        ds = spot - entry_spot  # Spot change
        dt_days = (now - entry_time).total_seconds() / 86400  # Time in days
        d_vix = vix - entry_vix  # VIX change (positive = VIX went up)

        g = entry_greeks
        delta_pnl = g.get("delta", 0) * ds
        gamma_pnl = 0.5 * g.get("gamma", 0) * ds * ds
        theta_pnl = g.get("theta", 0) * dt_days
        vega_pnl = g.get("vega", 0) * d_vix
        explained = delta_pnl + gamma_pnl + theta_pnl + vega_pnl
        residual = actual_pnl - explained

        # Find dominant factor
        components = {
            "DELTA": delta_pnl,
            "GAMMA": gamma_pnl,
            "THETA": theta_pnl,
            "VEGA": vega_pnl,
        }
        dominant = max(components, key=lambda k: abs(components[k]))
        dominant_pct = abs(components[dominant]) / abs(actual_pnl) * 100 if abs(actual_pnl) > 0.01 else 0

        logger.info(
            "exit attribution",
            extra={
                "tag": Tag.ATTRIBUTION,
                "strategy": self.strategy_id,
                "leg": leg,
                "mode": mode,
                "actual_pnl": round(actual_pnl, 2),
                "spot_change": round(ds, 2),
                "vix_change": round(d_vix, 2),
                "held_hours": round(dt_days * 24, 2),
                "delta_pnl": round(delta_pnl, 2),
                "gamma_pnl": round(gamma_pnl, 2),
                "theta_pnl": round(theta_pnl, 2),
                "vega_pnl": round(vega_pnl, 2),
                "residual": round(residual, 2),
                "dominant": dominant,
                "dominant_pct": round(dominant_pct, 1),
            },
        )

        get_structured_logger().log(
            "ATTRIBUTION", strategy_id=self.strategy_id, leg=leg, mode=mode,
            reason=reason, actual_pnl=round(actual_pnl, 2),
            spot_change=round(ds, 2), vix_change=round(d_vix, 2),
            held_hours=round(dt_days * 24, 2),
            delta_pnl=round(delta_pnl, 2), gamma_pnl=round(gamma_pnl, 2),
            theta_pnl=round(theta_pnl, 2), vega_pnl=round(vega_pnl, 2),
            explained_pnl=round(explained, 2), residual_pnl=round(residual, 2),
            dominant_factor=dominant, dominant_pct=round(dominant_pct, 1),
        )

    # ─── Helpers ──────────────────────────────────────────────

    # ─── Trend-confirmation helpers (ported from trend-improvements) ──

    def _build_banknifty_morning_range(self) -> None:
        """Populate self._bn_morning_high/_low from BN's first 3 M5 candles.

        No-op when BANKNIFTY isn't subscribed (e.g. NIFTY-only deployments),
        when fewer than 3 candles have completed yet, OR when the earliest
        candle in the in-memory aggregator buffer isn't actually the morning
        open (the mid-day-restart guard — without this, restarting the
        process at 13:00 would seed a bogus 13:00-13:15 "morning range"
        that poisons every subsequent _get_banknifty_confirming() call).
        """
        bn_token = self._find_bn_spot_token()
        if not bn_token:
            return
        bn_candles = self.ctx.get_candles(bn_token, Timeframe.M5, limit=10)
        if len(bn_candles) < 3:
            return
        morning = bn_candles[:3]
        # Mid-day restart guard: M5 candles bucket on 5-min boundaries
        # starting 9:15. The first morning candle's timestamp must be ≤ 9:20
        # (allowing a one-bucket slop). Any later first-candle means the
        # in-memory buffer doesn't include the real open and we should
        # skip — better no signal than a bogus afternoon "morning range".
        first_ts = morning[0].timestamp
        if first_ts.time() > time(9, 20):
            if self.ctx.clock.now().minute == 0:  # log once per hour
                logger.warning(
                    f"[{self.strategy_id}] BN morning range skipped — earliest "
                    f"buffered candle is {first_ts.time()}, after 9:20 "
                    f"(mid-day restart? aggregator buffer doesn't include open)"
                )
            return
        self._bn_morning_high = max(float(c.high) for c in morning)
        self._bn_morning_low = min(float(c.low) for c in morning)
        logger.info(
            f"[{self.strategy_id}] BankNifty morning range built: "
            f"high={self._bn_morning_high:.1f} low={self._bn_morning_low:.1f}"
        )

    def _find_bn_spot_token(self) -> int | None:
        """Resolve BANKNIFTY's spot token via the chain builder's registry.

        Returns None when BN isn't part of this deployment. Sets a sticky
        _bn_unavailable sentinel on first failed lookup so on_tick stops
        scanning the registry every tick in NIFTY-only deployments.
        """
        if self._bn_unavailable:
            return None
        cb = getattr(self.ctx, "_chain_builder", None)
        spot_tokens = getattr(cb, "_spot_tokens", None) if cb else None
        if not spot_tokens:
            self._bn_unavailable = True
            return None
        for token, name in spot_tokens.items():
            if name == "BANKNIFTY":
                return token
        self._bn_unavailable = True  # subscribed but no BANKNIFTY
        return None

    def _get_banknifty_confirming(self, direction: str) -> bool | None:
        """Return True/False/None for trend-direction confirmation by BN.

        - True  → BN spot is above its morning high (UP) or below its low (DOWN)
        - False → BN spot is on the opposite side (sector divergence)
        - None  → BN range not built yet, or BN spot unavailable
        """
        if not self._bn_morning_high:
            return None
        bn_token = self._find_bn_spot_token()
        if not bn_token:
            return None
        bn_spot = float(self.ctx._chain_builder.get_spot_price("BANKNIFTY") or 0.0)
        if bn_spot <= 0:
            return None
        if direction == "UP":
            return bn_spot > self._bn_morning_high
        elif direction == "DOWN":
            return bn_spot < self._bn_morning_low
        return None

    def _get_vix_prev_for_trend(self, lookback_minutes: int = 20) -> float:
        """Return the VIX reading from ~lookback_minutes ago, or 0.0.

        Walks the rolling history and returns the closest sample at least
        `lookback_minutes` old. Returns 0.0 when no sample is far enough
        back yet — score_trend_following's vix_prev branch treats 0.0 as
        "skip the VIX-direction term" so behavior degrades gracefully.
        """
        if len(self._vix_history) < 2:
            return 0.0
        now = self.ctx.clock.now()
        for ts, vix in reversed(self._vix_history):
            if (now - ts).total_seconds() >= lookback_minutes * 60:
                return vix
        return 0.0

    def _load_day_bias(self) -> None:
        """Load AI advisor DayBias from file + config flags."""
        try:
            from src.config import Settings
            settings = Settings()
            self._confluence_enabled = settings.advisor_confluence_enabled
            self._confluence_weight = settings.advisor_confluence_weight
        except Exception:
            self._confluence_enabled = False
            self._confluence_weight = 1.0

        # In replay/backtest with BACKFILL_DAY_BIAS_DIR set, load the
        # bias for the simulated calendar date so each day in the replay
        # window gets its own pre-generated bias. Production (no env var)
        # ignores `as_of` and reads the single live `data/day_bias.json`.
        try:
            as_of_date = self.ctx.clock.now().date()
        except Exception:
            as_of_date = None
        self._day_bias = load_day_bias(as_of=as_of_date)
        if self._day_bias:
            mode = "active" if self._confluence_enabled else "shadow"
            logger.info(
                f"[{self.strategy_id}] DayBias loaded ({mode}): "
                f"risk={self._day_bias.risk_level} conf={self._day_bias.confidence:.2f} "
                f"prem_adj={self._day_bias.premium_score_adj:+d} "
                f"trend_adj={self._day_bias.trend_score_adj:+d}"
            )

    def _build_snapshot(
        self, leg: str, decision: str, *, mode: str = "",
        rule_score: int = 0, ai_adj: int = 0, final_score: int = 0,
        threshold: int = 0, entry_premium: float = 0.0, quantity: int = 0,
        exit_reason: str = "", outcome_pnl: float | None = None,
        held_minutes: int | None = None, shadow_blocked: bool = False,
    ) -> DecisionSnapshot:
        """Build a DecisionSnapshot from current market state."""
        now = self.ctx.clock.now()
        spot = float(self.ctx.get_spot_price(self.params.underlying))
        vix = self.ctx.get_vix()

        # Option chain features
        chain = self.ctx.get_option_chain(self.params.underlying, self._expiry)
        pcr_oi = chain.pcr_oi if chain else 0.0
        max_pain = float(chain.max_pain) if chain else 0.0
        max_pain_dist = abs(spot - max_pain) / spot * 100 if spot > 0 and max_pain > 0 else 0.0

        # IV skew
        iv_skew_ratio = 0.0
        if chain and chain.strikes:
            from src.options.chain_analyzer import get_iv_skew
            skew = get_iv_skew(chain)
            otm_puts = skew.get("otm_puts", [])
            otm_calls = skew.get("otm_calls", [])
            avg_put_iv = sum(p["iv"] for p in otm_puts) / len(otm_puts) if otm_puts else 0
            avg_call_iv = sum(c["iv"] for c in otm_calls) / len(otm_calls) if otm_calls else 0
            iv_skew_ratio = avg_put_iv / avg_call_iv if avg_call_iv > 0 else 0

        # Regime — 2D model
        regime_str = ""
        vol_regime_str = ""
        action_regime_str = ""
        range_score = 0.0
        chop_score = 0.0
        trend_score = 0.0
        regime_conf = 0.0
        regime_conflict = False
        move_efficiency = 0.0
        move_from_open = 0.0
        morning_range = 0.0

        if self._regime_detector:
            regime = self._regime_detector.assess(self.params.underlying)
            regime_str = regime.regime.value if regime.regime else ""
            vol_regime_str = regime.vol_regime.value if hasattr(regime, 'vol_regime') else ""
            action_regime_str = regime.action_regime.value if hasattr(regime, 'action_regime') else ""
            if hasattr(regime, 'action_scores') and regime.action_scores:
                range_score = regime.action_scores.range_bound
                chop_score = regime.action_scores.choppy
                trend_score = regime.action_scores.trending
                regime_conf = regime.action_scores.confidence
                regime_conflict = regime.action_scores.is_conflicted
            move_efficiency = regime.move_efficiency if hasattr(regime, 'move_efficiency') else 0.0
            move_from_open = regime.move_from_open_pct
            morning_range = regime.morning_range_pct

        dte = (self._expiry - now.date()).days if self._expiry else 0
        is_expiry = 1 if self._expiry and now.date() == self._expiry else 0

        # Options microstructure from chain
        atm_iv = 0.0
        iv_skew_ce_pe = 0.0
        total_oi_ce = 0
        total_oi_pe = 0
        bid_ask_spread_atm = 0.0
        if chain and chain.strikes:
            for entry in chain.strikes:
                if entry.ce:
                    total_oi_ce += entry.ce.oi
                    if abs(float(entry.strike) - spot) < 100:  # Near ATM
                        atm_iv = entry.ce.greeks.iv if entry.ce.greeks else 0
                        bid_ask_spread_atm = float(entry.ce.ask_price - entry.ce.bid_price) if entry.ce.ask_price > 0 else 0
                if entry.pe:
                    total_oi_pe += entry.pe.oi
                    if abs(float(entry.strike) - spot) < 100 and atm_iv > 0:
                        pe_iv = entry.pe.greeks.iv if entry.pe.greeks else 0
                        iv_skew_ce_pe = atm_iv / pe_iv if pe_iv > 0 else 0

        # Session range from candles
        session_range_pct = 0.0
        spot_token = None
        for t, n in self.ctx._chain_builder._spot_tokens.items():
            if n == self.params.underlying:
                spot_token = t
                break
        if spot_token:
            candles = self.ctx._aggregator.get_completed_candles(spot_token, Timeframe.M5, limit=20)
            if candles:
                highs = [float(c.high) for c in candles]
                lows = [float(c.low) for c in candles]
                s_range = max(highs) - min(lows)
                session_range_pct = s_range / spot * 100 if spot > 0 else 0

        # Phase A trend-score breakdown — populated only for TREND rows where
        # _evaluate_trend ran this tick and cached the breakdown + inputs.
        # PREMIUM rows (and TREND rows that got logged before _evaluate_trend
        # ran, e.g. early SKIP) leave these at dataclass defaults.
        trend_kwargs: dict = {}
        if leg == "TREND" and self._trend_breakdown is not None:
            bd = self._trend_breakdown
            ti = self._trend_inputs
            trend_kwargs = {
                "breakout_strength": round(ti.get("breakout_strength", 0.0), 3),
                "oi_confirmed": ti.get("oi_confirmed", False),
                "trend_duration_minutes": ti.get("trend_duration_minutes", 0),
                "vix_prev": round(ti.get("vix_prev", 0.0), 2),
                "banknifty_confirming": ti.get("banknifty_confirming"),
                "bn_data_available": ti.get("bn_data_available", False),
                "score_f1_breakout": bd.f1_breakout,
                "score_f2_oi": bd.f2_oi,
                "score_f3_duration": bd.f3_duration,
                "score_f4_vix_level": bd.f4_vix_level,
                "score_f5_vix_dir": bd.f5_vix_dir,
                "score_f6_banknifty": bd.f6_banknifty,
                "score_clamp_hit": bd.clamp_hit,
                "trend_signal_threshold": self.params.trend_signal_threshold,
            }

        return DecisionSnapshot(
            timestamp=now.isoformat(),
            strategy_id=self.strategy_id,
            leg=leg,
            decision=decision,
            mode=mode or self._prem_mode,
            spot=spot,
            vix=vix,
            pcr_oi=pcr_oi,
            max_pain=max_pain,
            max_pain_dist_pct=round(max_pain_dist, 2),
            iv_skew_ratio=round(iv_skew_ratio, 3),
            move_from_open_pct=round(move_from_open, 3),
            morning_range_pct=round(morning_range, 3),
            dte=dte,
            hour=now.hour,
            minute=now.minute,
            day_of_week=now.weekday(),
            is_expiry=is_expiry,
            rule_score=rule_score,
            ai_adj=ai_adj,
            final_score=final_score,
            threshold=threshold,
            regime=regime_str,
            vol_regime=vol_regime_str,
            action_regime=action_regime_str,
            range_bound_score=round(range_score, 3),
            choppy_score=round(chop_score, 3),
            trending_score=round(trend_score, 3),
            regime_confidence=round(regime_conf, 3),
            regime_conflicted=regime_conflict,
            move_efficiency=round(move_efficiency, 3),
            session_range_pct=round(session_range_pct, 3),
            atm_iv=round(atm_iv, 4),
            iv_skew_ce_pe=round(iv_skew_ce_pe, 3),
            total_oi_ce=total_oi_ce,
            total_oi_pe=total_oi_pe,
            bid_ask_spread_atm=round(bid_ask_spread_atm, 2),
            prev_trade_pnl=round(self._prem_realized_pnl + self._trend_realized_pnl, 2),
            trades_today=self._prem_trades_today + self._trend_trades_today,
            entry_premium=entry_premium,
            quantity=quantity,
            exit_reason=exit_reason,
            outcome_pnl=outcome_pnl,
            held_minutes=held_minutes,
            shadow_blocked=shadow_blocked,
            **trend_kwargs,
        )

    def _get_gamma_exposure(self) -> float:
        """Compute net gamma exposure of short premium position."""
        chain = self.ctx.get_option_chain(self.params.underlying, self._expiry)
        if not chain or not chain.strikes:
            return 0.0
        spot = float(self.ctx.get_spot_price(self.params.underlying))
        if spot <= 0:
            return 0.0

        gamma_ce = gamma_pe = 0.0
        for entry in chain.strikes:
            if entry.ce and entry.ce.instrument_token == self._short_ce_token:
                gamma_ce = entry.ce.greeks.gamma
            if entry.pe and entry.pe.instrument_token == self._short_pe_token:
                gamma_pe = entry.pe.greeks.gamma
            if self._long_ce_token and entry.ce and entry.ce.instrument_token == self._long_ce_token:
                gamma_ce -= entry.ce.greeks.gamma
            if self._long_pe_token and entry.pe and entry.pe.instrument_token == self._long_pe_token:
                gamma_pe -= entry.pe.greeks.gamma

        net_gamma = abs(gamma_ce) + abs(gamma_pe)
        return net_gamma * self._lot_size * spot * 0.01

    def _find_delta_strikes(self, chain, target_ce_delta, target_pe_delta):
        """Thin wrapper around portfolio_strikes.find_delta_strikes."""
        return _strikes.find_delta_strikes(chain, target_ce_delta, target_pe_delta)

    def _find_oi_validated_strikes(self, chain, target_ce_delta, target_pe_delta):
        """Thin wrapper around portfolio_strikes.find_oi_validated_strikes.

        The class method form is kept so callers (and any unit tests that
        patch it) keep working. All real logic lives in the module function.
        """
        return _strikes.find_oi_validated_strikes(
            chain,
            target_ce_delta,
            target_pe_delta,
            vix=self.ctx.get_vix(),
            expiry=self._expiry,
            today=self.ctx.clock.now().date(),
            log_prefix=f"[{self.strategy_id}] ",
        )

    def _find_spot_token(self) -> int | None:
        """Thin wrapper around portfolio_strikes.find_spot_token."""
        return _strikes.find_spot_token(self.ctx._chain_builder, self.params.underlying)

    # ─── Lifecycle ────────────────────────────────────────────

    async def on_stop(self) -> None:
        active = []
        if self._prem_entered:
            active.append(f"premium({self._prem_mode})")
        if self._trend_entered:
            active.append(f"trend({self._trend_direction})")
        if active:
            logger.info(f"[{self.strategy_id}] Stopping with open: {', '.join(active)}")

    def reset_day_state(self) -> None:
        # Log day summary before resetting
        if self._prem_trades_today > 0 or self._trend_trades_today > 0:
            total = self._prem_realized_pnl + self._trend_realized_pnl
            prem_pct = (self._prem_realized_pnl / total * 100) if abs(total) > 0.01 else 0
            trend_pct = (self._trend_realized_pnl / total * 100) if abs(total) > 0.01 else 0
            logger.info(
                "day summary",
                extra={
                    "tag": Tag.DAY_SUMMARY,
                    "strategy": self.strategy_id,
                    "total_pnl": round(total, 2),
                    "premium_pnl": round(self._prem_realized_pnl, 2),
                    "premium_pct": round(prem_pct, 1),
                    "premium_trades": self._prem_trades_today,
                    "trend_pnl": round(self._trend_realized_pnl, 2),
                    "trend_pct": round(trend_pct, 1),
                    "trend_trades": self._trend_trades_today,
                },
            )

            get_structured_logger().log(
                "DAY_SUMMARY", strategy_id=self.strategy_id,
                total_pnl=round(total, 2),
                premium_pnl=round(self._prem_realized_pnl, 2),
                premium_trades=self._prem_trades_today,
                trend_pnl=round(self._trend_realized_pnl, 2),
                trend_trades=self._trend_trades_today,
            )

        self._prem_entered = False
        self._prem_stopped = False
        self._prem_mode = "idle"
        self._prem_score = 0
        self._peak_premium = Decimal("0")
        self._prem_fill_pending = False
        self._trend_entered = False
        self._trend_stopped = False
        self._trend_score = 0
        self._trend_breakdown = None  # Phase A logging cache
        self._trend_inputs = {}
        self._peak_spread_value = Decimal("0")
        self._trend_fill_pending = False
        self._day_pnl = 0.0

        # Reload AI day bias for new day
        self._load_day_bias()

        # Reset tracking state
        self._prem_entry_time = None
        self._prem_entry_spot = 0.0
        self._prem_entry_vix = 0.0
        self._prem_entry_greeks = {}
        self._trend_entry_time = None
        self._trend_entry_spot = 0.0
        self._trend_entry_vix = 0.0
        self._trend_entry_greeks = {}
        self._prem_realized_pnl = 0.0
        self._trend_realized_pnl = 0.0
        self._prem_trades_today = 0
        self._trend_trades_today = 0
        self._last_monitor_minute = -1
        # Friday square-off fires once per session; reset the dedup flag so
        # tomorrow's log is clean (matters especially on Fri→Mon reset).
        self._friday_squareoff_logged = False

        # Reset per-session signal context (Apr 18 trend-improvements port):
        # VIX history is a 60-min lookback buffer for the rising/falling
        # confirmation in score_trend_following — yesterday's tail readings
        # have nothing to say about today's open. Same logic for the
        # BankNifty morning range (built fresh from the first 3 5-min
        # candles each day). The BN-unavailable sentinel could in theory
        # stay sticky across sessions, but resetting it keeps the strategy
        # tolerant of mid-deployment subscription changes. Log throttle
        # also resets so first 5-min boundary tomorrow logs cleanly.
        self._vix_history.clear()
        self._bn_morning_high = 0.0
        self._bn_morning_low = 0.0
        self._bn_unavailable = False
        self._last_iv_rank_log_minute = -1

        # Stale-tick guard: clear cross-session baseline so the first tick
        # tomorrow is always accepted (gap-up/gap-down is not corruption).
        # Day-counter resets so the warn log is per-session.
        self._last_sane_spot = 0.0
        self._last_sane_spot_ts = None
        self._last_stale_log_minute = -1
        self._stale_ticks_today = 0
        self._last_skip_log_minute.clear()
        # Confluence log dedup is process-global (module state) — clear so
        # the first decision tomorrow logs cleanly even if the daemon
        # spans midnight without a process restart.
        reset_confluence_log_dedup()

        if self._regime_detector:
            self._regime_detector.reset_session()

    def get_state_data(self) -> dict:
        return {
            "premium_mode": self._prem_mode,
            "premium_entered": self._prem_entered,
            "trend_entered": self._trend_entered,
            "premium_score": self._prem_score,
            "trend_score": self._trend_score,
            "entry_premium": str(self._entry_premium),
            "entry_debit": str(self._entry_debit),
        }

    def get_diagnostics(self) -> dict:
        """Live diagnostics for dashboard — per-leg state, Greeks, P&L."""
        now = self.ctx.clock.now()
        spot = float(self.ctx.get_spot_price(self.params.underlying))
        vix = self.ctx.get_vix()

        # ── Premium leg ──────────────────────────────────────
        prem: dict = {
            "active": self._prem_entered,
            "stopped": self._prem_stopped,
            "mode": self._prem_mode,
            "score": self._prem_score,
            "entry_premium": float(self._entry_premium),
            "current_premium": 0.0,
            "unrealized_pnl": 0.0,
            "greeks": {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0},
            "gamma_exposure": 0.0,
            "gamma_threshold": self.params.gamma_exit_threshold,
            "theta_efficiency": 0.0,
            "held_minutes": 0,
            "symbols": {"short_ce": self._short_ce_symbol, "short_pe": self._short_pe_symbol,
                        "long_ce": self._long_ce_symbol, "long_pe": self._long_pe_symbol},
        }
        if self._prem_entered:
            short_cost = float(
                self.ctx.get_ltp(self._short_ce_token) + self.ctx.get_ltp(self._short_pe_token)
            )
            if self._prem_mode == "iron_condor" and self._long_ce_token:
                long_val = float(
                    self.ctx.get_ltp(self._long_ce_token) + self.ctx.get_ltp(self._long_pe_token)
                )
                current_cost = short_cost - long_val
            else:
                current_cost = short_cost

            ep = float(self._entry_premium)
            prem["current_premium"] = round(current_cost, 2)
            prem["unrealized_pnl"] = round((ep - current_cost) * self._prem_quantity, 0)
            prem["gamma_exposure"] = round(self._get_gamma_exposure(), 1)

            g = self._capture_leg_greeks(self._get_premium_token_signs(), self._prem_quantity)
            prem["greeks"] = {k: round(v, 4) for k, v in g.items()}
            prem["theta_efficiency"] = (
                round(abs(g["theta"] / g["gamma"]), 1)
                if abs(g["gamma"]) > 1e-6 else 999.0
            )
            if self._prem_entry_time:
                prem["held_minutes"] = int((now - self._prem_entry_time).total_seconds() / 60)

        # ── Trend leg ────────────────────────────────────────
        trend: dict = {
            "active": self._trend_entered,
            "stopped": self._trend_stopped,
            "direction": self._trend_direction,
            "score": self._trend_score,
            "entry_debit": float(self._entry_debit),
            "current_value": 0.0,
            "max_spread": float(self._max_spread_value),
            "unrealized_pnl": 0.0,
            "greeks": {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0},
            "held_minutes": 0,
            "symbols": {"buy": self._trend_buy_symbol, "sell": self._trend_sell_symbol},
        }
        if self._trend_entered:
            buy_ltp = float(self.ctx.get_ltp(self._trend_buy_token))
            sell_ltp = float(self.ctx.get_ltp(self._trend_sell_token))
            current_value = buy_ltp - sell_ltp
            trend["current_value"] = round(current_value, 2)
            trend["unrealized_pnl"] = round(
                (current_value - float(self._entry_debit)) * self._trend_quantity, 0
            )
            g = self._capture_leg_greeks(self._get_trend_token_signs(), self._trend_quantity)
            trend["greeks"] = {k: round(v, 4) for k, v in g.items()}
            if self._trend_entry_time:
                trend["held_minutes"] = int((now - self._trend_entry_time).total_seconds() / 60)

        # ── Day summary ──────────────────────────────────────
        prem_unreal = prem["unrealized_pnl"]
        trend_unreal = trend["unrealized_pnl"]
        day: dict = {
            "premium_realized": round(self._prem_realized_pnl, 0),
            "trend_realized": round(self._trend_realized_pnl, 0),
            "premium_unrealized": prem_unreal,
            "trend_unrealized": trend_unreal,
            "total_pnl": round(
                self._prem_realized_pnl + self._trend_realized_pnl + prem_unreal + trend_unreal, 0
            ),
            "premium_trades": self._prem_trades_today,
            "trend_trades": self._trend_trades_today,
        }

        return {
            "premium": prem,
            "trend": trend,
            "day": day,
            "spot": round(spot, 2),
            "vix": round(vix, 2),
            "timestamp": now.isoformat(),
        }

    def load_state_data(self, data: dict) -> None:
        self._prem_mode = data.get("premium_mode", "idle")
        self._prem_entered = data.get("premium_entered", False)
        self._trend_entered = data.get("trend_entered", False)
        self._prem_score = data.get("premium_score", 0)
        self._trend_score = data.get("trend_score", 0)
        self._entry_premium = Decimal(data.get("entry_premium", "0"))
        self._entry_debit = Decimal(data.get("entry_debit", "0"))
