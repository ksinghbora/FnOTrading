"""Base strategy abstract class — the contract all strategies implement."""

import logging
from abc import ABC, abstractmethod
from datetime import date, time
from decimal import Decimal
from typing import Any

from src.core.models import OHLC, Order, Signal, Subscription, Tick
from src.core.types import OrderSide, OrderType, StrategyState
from src.strategy.decision_logger import DecisionLogger, DecisionSnapshot
from src.strategy.params import BaseStrategyParams
from src.strategy.signals import mid_from_option, mid_from_quote
from src.utils.log_tags import Tag

logger = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """Abstract base class for all trading strategies.

    Lifecycle:
    1. __init__() — receives params, creates strategy
    2. on_start() — subscribe to instruments, load state
    3. on_tick() / on_candle() — receive market data, generate signals
    4. on_order_update() — handle fill/rejection notifications
    5. on_stop() — cleanup, save state

    Strategies never directly touch the broker. They return Signal objects
    which are validated by risk management before execution.
    """

    # ─── V5 (May 7 2026): regime-family declaration ────────────────
    # Each concrete strategy subclass overrides this to declare which
    # regime family it belongs to. Used by OrchestratorStrategy V5 to:
    #  - Pull the matching ``regime_confidence_for_<family>`` confidence
    #    when multi-family allocation decisions are made
    #  - Block correlated stacking (two premium-sellers entering on the
    #    same tick) when ``OrchestratorParams.block_correlated_families=True``
    #
    # Canonical values (matching ``RegimeDetector.regime_confidence_snapshot``):
    #  - "premium_selling"   : iron_condor, iron_butterfly, short_strangle, short_straddle
    #  - "long_vol"          : long_calendar, long_straddle
    #  - "directional_trend" : trend_daily, trend_itm, trend_debit_spread
    #  - "unknown" (default) : strategy not yet classified — orchestrator
    #                          uses neutral 0.5 confidence
    #
    # The default "unknown" preserves backward compatibility — every
    # existing strategy keeps its prior behavior until it explicitly
    # declares its family.
    regime_family: str = "unknown"

    def __init__(self, strategy_id: str, params: BaseStrategyParams):
        self.strategy_id = strategy_id
        self.params = params
        self.state = StrategyState.IDLE
        self._context: Any = None  # Set by StrategyRunner
        # ─── Decision snapshot logger (Apr 20 — was portfolio-only) ──
        # Lives on every strategy now so the 23-day chain replay and
        # live paper trading can emit one row per ENTER/EXIT for every
        # active strategy, not just portfolio_1. The Apr 20 audit found
        # decisions_2026-04-20.csv had only 2 rows total (1 ENTER + 1
        # EXIT for portfolio_1) despite ic_1, strangle_1, straddle_1,
        # and trend_1 all running — they had no logger at all. With
        # GDFL 18-month tick data arriving, we need decisions from
        # every strategy to do honest A/B attribution.
        # Replay strategy IDs end in "_replay" (set by ReplayBacktestEngine)
        # — truncate per session so a re-run doesn't pile rows on top of
        # the prior run. Live trading must keep append mode (mid-day
        # reconnect must not lose decisions written that morning).
        is_replay = strategy_id.endswith("_replay")
        self._decision_logger: DecisionLogger = DecisionLogger(
            truncate_per_session=is_replay
        )
        # Entry timestamp — set by _log_decision on ENTER, read on EXIT
        # to compute held_minutes without each strategy tracking it.
        self._decision_entry_ts: Any = None
        # Apr 29 2026 audit: snapshot of cumulative strategy charges at
        # the most recent ENTER. The EXIT row writes (current - this) as
        # the round-trip charge cost. Reset to current at each EXIT so
        # the next ENTER starts fresh. Float (not Decimal) because the
        # decision-row schema is float and rounding noise is irrelevant
        # at the ~₹100s scale we're tracking.
        self._charges_at_entry: float = 0.0
        # Apr 29 2026 Phase 1C: stable trade lifecycle id. Set on ENTER,
        # propagated through ADJUST rows, re-stamped on EXIT, then
        # cleared. Empty string outside of an active trade. Subclasses
        # that emit ADJUST rows read ``self._current_trade_id`` to keep
        # the same id across the lifecycle.
        self._current_trade_id: str = ""
        # Per-(strategy, key) dedup state for entry-skip log throttling.
        # See _log_skip_throttled below for why this lives on the base —
        # without it, every structural skip (expiry day 0DTE, VIX gate,
        # DTE hard-block, etc.) emits a fresh log line on every tick. On
        # Apr 21 expiry the unthrottled base.py "filter blocked entry"
        # log fired 19,646 times across ic_1/strangle_1/straddle_1 in
        # ~20 minutes, drowning real signal in the audit log.
        self._last_skip_log_minute: dict[str, int] = {}
        # Phase 3b Gate B state — captures VIX at first tick after 9:15 IST
        # each day, used by _check_intraday_vix_spike_filter to detect a
        # mid-session vol regime change vs. morning baseline.
        self._intraday_vix_morning: float | None = None
        self._intraday_vix_capture_date: date | None = None
        # Apr 30 2026 (sonnet's quote-fallback probe — Step 2 of the
        # multi-model audit follow-up): track how often ``_bid_ask_for``
        # serves real bid/ask vs. how often it falls back to LTP-symmetric
        # because the tick has no quote, a zero side, or a crossed
        # market. The validation harness prints (total, fallback,
        # fallback_pct) at the end of each run so operators can tell
        # whether a "realistic-fill" pass actually saw bid/ask or was
        # silently degrading to the same LTP fiction the audit removed.
        self._bid_ask_total: int = 0
        self._bid_ask_fallback: int = 0

    def set_context(self, context: "StrategyContext") -> None:
        """Inject the strategy context (called by runner, not by strategy)."""
        self._context = context

    @property
    def ctx(self) -> "StrategyContext":
        """Shortcut to strategy context."""
        if self._context is None:
            raise RuntimeError(f"Strategy {self.strategy_id} context not set")
        return self._context

    @abstractmethod
    def get_subscriptions(self) -> Subscription:
        """Declare what instruments and timeframes this strategy needs."""

    @abstractmethod
    async def on_start(self) -> None:
        """Called when strategy starts. Initialize state, load positions."""

    @abstractmethod
    async def on_tick(self, tick: Tick) -> Signal | None:
        """Called on every tick for subscribed instruments.

        Return a Signal to place orders, or None to do nothing.
        """

    def evaluate_score(self) -> int:
        """Return current 0-100 setup score WITHOUT mutating any state.

        Used by the OrchestratorStrategy to compare candidates per tick.
        Default returns 0 (strategy has no scoring rule — orchestrator
        will treat it as "never the best choice" unless every other
        candidate also returns 0). Strategies with scoring rules
        (iron_condor, iron_butterfly, short_strangle, short_straddle,
        long_calendar) should factor their scoring block into a helper
        and call it from both ``evaluate_score`` and the entry path so
        the standalone and orchestrated paths agree on the score.

        Contract:
        - MUST NOT mutate self._entered, position state, or any tracker
        - MUST be safe to call multiple times per tick
        - MUST be safe to call when self._regime, self._expiry, etc. are not yet set
        """
        return 0

    def evaluate_regime_confidence(self) -> float:
        """Return current 0.0-1.0 regime-fitness confidence.

        Complements ``evaluate_score`` (which scores the entry SETUP) by
        scoring the underlying MARKET REGIME via a strategy-family lens.
        Used by OrchestratorStrategy V5 to combine setup-quality and
        regime-fitness into a single figure of merit:

            effective_score = legacy_0_100_score × regime_confidence

        Default behaviour:
          - Lookup ``self.regime_family`` (class attr, default "unknown")
          - Pull the matching confidence from
            ``RegimeDetector.regime_confidence_snapshot(underlying)``
          - Return 0.5 (neutral) when family is "unknown" or detector
            unavailable — so a strategy with no family declaration is
            never artificially gated to 0 by a missing decoder

        Concrete strategies CAN override for finer control (e.g., a
        strategy that wants to weight CI more than VRP), but the default
        family-based dispatch is sufficient for the canonical roster.

        Contract:
        - MUST NOT mutate any state
        - MUST be safe to call multiple times per tick
        - MUST be safe to call when ctx is not yet set (returns 0.5)
        """
        if self.regime_family == "unknown":
            return 0.5
        try:
            ctx = self._context
            if ctx is None:
                return 0.5
            regime = getattr(ctx, "_regime_detector", None) or getattr(ctx, "regime_detector", None)
            if regime is None:
                return 0.5
            underlying = getattr(self.params, "underlying", "NIFTY")
            # Dispatch by family (matches RegimeDetector method names)
            if self.regime_family == "premium_selling":
                return float(regime.regime_confidence_for_premium_selling(underlying))
            if self.regime_family == "long_vol":
                return float(regime.regime_confidence_for_long_vol(underlying))
            if self.regime_family == "directional_trend":
                return float(regime.regime_confidence_for_directional_trend(underlying))
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"[{self.strategy_id}] regime_confidence lookup failed: {exc}")
        return 0.5

    async def on_candle(self, candle: OHLC) -> Signal | None:
        """Called on candle close for subscribed timeframes.

        Override if strategy uses candle-based logic. Default: no-op.
        """
        return None

    async def on_order_update(self, order: Order) -> None:
        """Called when an order for this strategy is filled/rejected.

        Override to handle execution updates. Default: no-op.
        """
        pass

    @abstractmethod
    async def on_stop(self) -> None:
        """Called on strategy shutdown. Save state, optionally close positions."""

    async def on_error(self, error: Exception) -> None:
        """Called on unhandled error.

        Default: log, disable further entries today, and emergency-close all
        open positions. Without this, an exception in `_check_adjustments` or
        similar would leave a half-built IC unmanaged until exchange
        auto-square-off (Apr 13: 5,321 wing-strike-missing warnings on IC).
        """
        logger.exception(f"Strategy {self.strategy_id} error: {error}")
        # Disable further entries today (subclasses use this flag)
        if hasattr(self, "_stopped_for_day"):
            self._stopped_for_day = True
        try:
            await self._emergency_close_positions(reason=type(error).__name__)
        except Exception as close_err:
            # Emergency close itself failing is a critical alert — log loudly,
            # don't re-raise (would mask the original error).
            logger.exception(
                f"[EMERGENCY_CLOSE] {self.strategy_id} close failed: {close_err}"
            )

    async def _emergency_close_positions(self, reason: str) -> None:
        """Force-exit all open positions for this strategy with MARKET orders.

        F1 escape hatch — this is the ONE path that intentionally sends MARKET
        for options. We accept the full bid-ask crossing cost because we're
        already in an error state and need the position flat now, not at some
        optimistic mid that may never fill. Kill-switch / on_error / unhedged-
        wing recovery all funnel here. Regular strategy exits MUST NOT use
        MARKET — use LIMIT-at-mid via make_option_leg() / _build_option_leg().
        """
        from src.core.models import Signal, SignalLeg
        from src.core.types import OrderSide, OrderType, SignalType

        positions = self.ctx.get_open_positions()
        legs = []
        for pos in positions:
            if pos.quantity == 0:
                continue
            # Reverse side to flatten: long → SELL, short → BUY
            side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
            legs.append(SignalLeg(
                tradingsymbol=pos.tradingsymbol,
                instrument_token=pos.instrument_token,
                order_side=side,
                quantity=abs(pos.quantity),
                order_type=OrderType.MARKET,
            ))
        if not legs:
            logger.info(
                f"[EMERGENCY_CLOSE] {self.strategy_id} no open positions "
                f"(reason={reason})"
            )
            return
        signal = Signal(
            strategy_id=self.strategy_id,
            signal_type=SignalType.EXIT,
            legs=legs,
            reason=f"emergency_close:{reason}",
        )
        logger.warning(
            f"[EMERGENCY_CLOSE] {self.strategy_id} closing {len(legs)} positions "
            f"(reason={reason})"
        )
        await self.ctx.place_signal(signal)

    # ─── Leg pricing helpers (F1 — LIMIT-at-mid) ─────────────────

    def _leg_mid_price_from_token(
        self, instrument_token: int, tick_size: Decimal = Decimal("0.05")
    ) -> Decimal | None:
        """Look up mid (bid+ask)/2 from the latest tick for `instrument_token`.

        Returns None when no tick is cached yet, or when either side is zero.
        Used by exit/adjust paths that only carry the token, not the full
        OptionData object. The caller MUST handle None — never fall back
        to MARKET silently.
        """
        tick = self.ctx.get_tick(instrument_token)
        if tick is None:
            return None
        return mid_from_quote(tick.bid_price, tick.ask_price, tick_size=tick_size)

    def _build_option_leg(
        self,
        tradingsymbol: str,
        instrument_token: int,
        side: OrderSide,
        quantity: int,
        *,
        opt: Any = None,
    ):
        """Build a LIMIT-at-mid SignalLeg for an options instrument.

        Resolution order:
          1. If `opt` (an OptionData) is provided, use its bid/ask.
          2. Otherwise fetch the latest Tick for `instrument_token` from
             the feed and use its bid/ask.

        Returns None when no quote is available. Strategies should treat
        None as a hard skip — don't degrade to MARKET silently, the whole
        point of F1 is to stop crossing the full spread on thin wings.
        """
        mid = None
        if opt is not None:
            mid = mid_from_option(opt)
        if mid is None:
            mid = self._leg_mid_price_from_token(instrument_token)
        if mid is None or mid <= 0:
            logger.info(
                "[FILL] bid/ask missing for %s — skipping leg "
                "(strategy=%s side=%s qty=%d token=%d)",
                tradingsymbol, self.strategy_id, side.value, quantity, instrument_token,
            )
            return None
        from src.core.models import SignalLeg
        return SignalLeg(
            tradingsymbol=tradingsymbol,
            instrument_token=instrument_token,
            order_side=side,
            quantity=quantity,
            order_type=OrderType.LIMIT,
            price=mid,
        )

    def _log_skip_throttled(
        self, key: str, message: str, *, extra: dict | None = None
    ) -> None:
        """Log an entry-skip message at most once per wall-clock minute per key.

        Dedup contract — the key partitions reasons so a *change* of reason
        surfaces immediately even within the same minute, while a sustained
        block emits one line/minute (enough for the audit log, not enough
        to drown real signal). The base class owns this so every strategy
        gets the same throttle for free; previously each subclass had its
        own copy of the helper and the base methods (`_check_expiry_day_block`,
        VIX gate) logged unthrottled — which is what produced the 19k-line
        flood on Apr 21 expiry.

        Logger resolution: we use the *subclass's* module logger so caplog
        filters keyed on `src.strategy.implementations.<x>` continue to
        work and the log line shows the strategy file, not base.py.
        """
        try:
            now = self.ctx.clock.now()
            cur_min = now.hour * 60 + now.minute
        except Exception:
            cur_min = -1
        if self._last_skip_log_minute.get(key) == cur_min:
            return
        self._last_skip_log_minute[key] = cur_min
        logging.getLogger(type(self).__module__).info(
            message, extra=extra or {}
        )

    def _check_expiry_day_block(self, underlying: str) -> str | None:
        """Block new premium-leg entries on weekly expiry day (NIFTY: Tuesday).

        Entering new IC/strangle/straddle on the same day they expire is 0DTE
        selling — the 14:30-15:15 gamma vertical can move ATM 100% in minutes,
        and ITM auto-exercise STT eats any "win". Default is conservative skip.

        Returns None if OK, or a reason string to skip.
        """
        if not getattr(self.params, "skip_entry_on_expiry_day", True):
            return None
        if not self.ctx.clock.is_expiry_day(underlying):
            return None
        self._log_skip_throttled(
            f"EXPIRY_DAY_BLOCK:{underlying}",
            "filter blocked entry: expiry day 0DTE",
            extra={
                "tag": Tag.FILTER,
                "strategy": self.strategy_id,
                "filter": "expiry_day_0dte",
                "underlying": underlying,
                "action": "BLOCK",
            },
        )
        return f"Skipping entry — {underlying} expiry today (0DTE risk)"

    # ─── Vol-scaled exit helpers ─────────────────────────────────────
    # Opt-in behind `params.vol_scaled_exits`. When off, strategies use
    # their existing hardcoded percentages and these helpers are never
    # called. When on, the helpers replace a static pct with
    #
    #     effective_pct = k * (vix/100) * sqrt(dte/365)
    #
    # clamped to [0.10, 0.60] to avoid degenerate values on expiry-day
    # VIX spikes or zero-DTE denominators. See PortfolioParams /
    # BaseStrategyParams for calibration math (VIX=15 weekly => k_sl=12.0
    # reproduces current 25% SL).

    # Clamp values are deliberate process constants — the 10%..60% band
    # covers the "economically meaningful but not catastrophic" region
    # seen across current params (10% trail minimum, 60% IC stress max).
    _VOL_SCALED_EXIT_MIN = 0.10
    _VOL_SCALED_EXIT_MAX = 0.60

    def _compute_vol_scaled_exit_pct(
        self, kind: str, dte: int, fallback_pct: float
    ) -> float:
        """Compute vol-scaled exit threshold as a percentage (e.g. 25.0 = 25%).

        Args:
            kind: One of "sl", "pt", "trail" — selects which `*_vol_k`
                multiplier to use.
            dte: Days-to-expiry for the active expiry (1 minimum — a
                sub-1 DTE is treated as 1 day to avoid sqrt(0)).
            fallback_pct: Hardcoded percentage to return when
                `vol_scaled_exits=False` OR the helper cannot compute
                a valid value (VIX unavailable, etc.). Pass the
                existing `premium_stop_loss_pct` / `trend_profit_target_pct`
                / etc. Caller semantics are preserved when vol scaling
                is off.

        Returns:
            Percentage (matches the caller's existing scale — e.g. 25.0
            not 0.25) so existing comparisons like
            `if loss_pct > self.params.stop_loss_pct` keep working
            whether the RHS is hardcoded or vol-scaled.
        """
        if not getattr(self.params, "vol_scaled_exits", False):
            return fallback_pct

        kind = kind.lower()
        if kind == "sl":
            k = float(getattr(self.params, "sl_vol_k", 12.0))
        elif kind == "pt":
            k = float(getattr(self.params, "pt_vol_k", 5.8))
        elif kind == "trail":
            k = float(getattr(self.params, "trail_vol_k", 4.8))
        else:
            raise ValueError(f"Unknown vol-scaled exit kind: {kind}")

        try:
            vix = float(self.ctx.get_vix())
        except Exception:
            vix = 0.0
        if vix <= 0:
            # No VIX signal — fall back to hardcoded to avoid silently
            # running a degenerate 0%-SL.
            return fallback_pct

        dte_safe = max(1, int(dte))
        from math import sqrt
        sigma_t = (vix / 100.0) * sqrt(dte_safe / 365.0)
        raw = k * sigma_t  # fraction
        clamped = max(self._VOL_SCALED_EXIT_MIN, min(self._VOL_SCALED_EXIT_MAX, raw))

        effective_pct = clamped * 100.0  # back to percentage scale

        logger.debug(
            "vol-scaled exit threshold computed",
            extra={
                "tag": "VOL_SCALED_EXIT",
                "strategy": self.strategy_id,
                "kind": kind,
                "k": round(k, 3),
                "vix": round(vix, 2),
                "dte": dte_safe,
                "raw_frac": round(raw, 4),
                "clamped_frac": round(clamped, 4),
                "effective_pct": round(effective_pct, 2),
                "fallback_pct": round(fallback_pct, 2),
            },
        )
        return effective_pct

    def _can_activate_trail_stop(self, decay_pct: float) -> bool:
        """Two-gate guard before trailing-stop logic fires (Apr 17 fix).

        Without these gates the trail can lock losses on opening auction
        whipsaws (premium swings 10-20% intraday on directionless days).

        Gate 1 (time): now must be at/after `trail_stop_activate_after_time`.
            Default 10:15 — past the 9:15-10:15 auction-imbalance window.
        Gate 2 (move):  premium must have decayed at least
            `trail_stop_min_decay_pct` from entry. Decay is a volatility-
            normalised proxy until intraday ATR tracking lands.

        decay_pct: positive number = premium decayed (winner). Pass the
        already-computed decay percentage from the caller; this method
        does not look at premiums itself.
        """
        # Gate 1 — time of day
        activate_after = getattr(
            self.params, "trail_stop_activate_after_time", None
        )
        if activate_after is not None:
            now_t = self.ctx.clock.now().time()
            if now_t < activate_after:
                return False
        # Gate 2 — minimum decay
        min_decay = float(getattr(self.params, "trail_stop_min_decay_pct", 0.0))
        if decay_pct < min_decay:
            return False
        return True

    # ─── Realistic-fill quote helpers (Apr 29 2026 multi-model audit) ──
    # The bookkeeping audit showed that *every* strategy's outcome_pnl was
    # using ctx.get_ltp (midpoint) rather than the bid/ask the broker
    # actually crosses. These helpers live on BaseStrategy so the four
    # remaining strategies (strangle, straddle, butterfly, calendar) can
    # share the same conversion as iron_condor's Apr 29 entry/exit fix.
    # Per-strategy ``_entry_fill_credit`` / ``_exit_fill_debit`` methods
    # call into these to compose their own leg-specific math.

    def _bid_ask_for(self, token: int) -> tuple[float, float]:
        """Return (bid, ask) for ``token`` from the latest tick.

        Falls back to (ltp, ltp) — i.e. assumes zero spread — only when
        bid/ask are unavailable. The fallback is a defensive last
        resort; it will inflate PnL the same way the old LTP path did,
        so callers should treat that as a quote-quality alarm, not a
        clean signal.

        Apr 30 2026: bumps the per-strategy ``_bid_ask_total`` /
        ``_bid_ask_fallback`` counters so the validation harness can
        print a quote-quality summary at end of run. Without this an
        operator can't tell whether a "realistic-fill" CPCV pass
        actually saw bid/ask or silently degraded to LTP for most
        legs (which would put the audit fix back to where it started).
        """
        self._bid_ask_total += 1
        tick = self.ctx.get_tick(token)
        if tick is not None:
            bid = float(tick.bid_price or 0)
            ask = float(tick.ask_price or 0)
            if bid > 0 and ask > 0 and ask >= bid:
                return bid, ask
        # Fallback path. Log at DEBUG so a verbose run can locate which
        # legs/timestamps degraded; the counter is the main signal.
        self._bid_ask_fallback += 1
        if logger.isEnabledFor(logging.DEBUG):
            reason = (
                "no tick" if tick is None
                else f"bad quote bid={float(tick.bid_price or 0)} ask={float(tick.ask_price or 0)}"
            )
            logger.debug(
                f"[{self.strategy_id}] BID_ASK_FALLBACK token={token} reason={reason}"
            )
        ltp = float(self.ctx.get_ltp(token) or 0)
        return ltp, ltp

    def get_quote_fallback_stats(self) -> dict:
        """Return the realistic-fill quote-quality counters for this run.

        ``total`` is the number of ``_bid_ask_for`` calls; ``fallback``
        is how many of those calls hit the LTP-symmetric fallback;
        ``fallback_pct`` is the percentage. Consumed by the validation
        harness to surface "X% of quote lookups degraded to LTP"
        alongside the rest of the run summary. A high percentage is a
        signal that the source feed lacked usable bid/ask for the
        traded strikes — typically the exact deep-OTM legs the
        liquidity filter is supposed to gate.
        """
        total = int(self._bid_ask_total)
        fallback = int(self._bid_ask_fallback)
        fallback_pct = (fallback / total * 100.0) if total > 0 else 0.0
        return {
            "total": total,
            "fallback": fallback,
            "fallback_pct": round(fallback_pct, 2),
        }

    @staticmethod
    def _spread_pct(bid: float, ask: float) -> float | None:
        """Bid-ask spread as a percentage of mid. None if quote is invalid."""
        if bid <= 0 or ask <= 0 or ask < bid:
            return None
        mid = (bid + ask) / 2.0
        if mid <= 0:
            return None
        return ((ask - bid) / mid) * 100.0

    def _check_strike_liquidity(self, opt: Any, leg_label: str) -> str | None:
        """Reject a candidate strike whose bid-ask spread exceeds
        ``self.params.max_spread_pct`` (% of mid). Returns ``None`` if
        the strike is liquid enough to trade, otherwise a human-readable
        reason for the entry-skip log.

        ``params.max_spread_pct <= 0`` disables the filter — kept as a
        bisection / regression-test escape hatch.

        Apr 29 2026 Phase 2: promoted from ``IronCondorStrategy`` to
        BaseStrategy. The filter operates on a generic ``OptionData``-
        shaped object (any leg with ``bid_price`` / ``ask_price`` /
        ``tradingsymbol`` attributes) so strangle / straddle / calendar
        share the same gate. ``params.max_spread_pct`` lives on
        BaseStrategyParams for the same reason.
        """
        if getattr(self.params, "max_spread_pct", 0) <= 0:
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
                f"{leg_label} {getattr(opt, 'tradingsymbol', '?')}: spread "
                f"{spread_pct:.1f}% > max {self.params.max_spread_pct}% "
                f"(bid={bid}, ask={ask})"
            )
        return None

    def _check_vix_filter(self) -> str | None:
        """Check if VIX is within the strategy's allowed band.

        Blocks entry when VIX < vix_entry_min (complacency, premium too cheap)
        or VIX > vix_entry_max (event/stress beyond strategy tolerance).
        Returns None if OK, or a reason string to skip.

        Apr 29 2026 (multi-model audit fix): VIX <= 0 now BLOCKS entry
        instead of silently allowing it. The old behaviour ("VIX feed
        unavailable, allow entry") was a foot-gun: every GDFL data gap
        would let strategies fire without a vol filter, exactly when
        operators most rely on it. Fail-closed is the safer default.
        """
        vix = self.ctx.get_vix()
        if vix <= 0:
            return "VIX data unavailable (vix<=0) — entry blocked, fail-closed"
        vix_min = getattr(self.params, "vix_entry_min", 0.0)
        vix_max = self.params.vix_entry_max
        if vix < vix_min:
            logger.debug(
                "filter blocked entry: vix below min",
                extra={
                    "tag": Tag.FILTER,
                    "strategy": self.strategy_id,
                    "filter": "vix",
                    "value": round(vix, 2),
                    "min": vix_min,
                    "result": "block",
                },
            )
            return f"VIX {vix:.1f} below min {vix_min} (complacency)"
        if vix > vix_max:
            logger.debug(
                "filter blocked entry: vix above max",
                extra={
                    "tag": Tag.FILTER,
                    "strategy": self.strategy_id,
                    "filter": "vix",
                    "value": round(vix, 2),
                    "max": vix_max,
                    "result": "block",
                },
            )
            return f"VIX {vix:.1f} exceeds max {vix_max}"
        return None

    def _capture_morning_vix_if_needed(self) -> None:
        """Capture morning-open VIX once per trading day for Gate B.

        Idempotent: only captures on the first call after 9:15 IST per
        trading day. Used by ``_check_intraday_vix_spike_filter`` to detect
        same-day vol regime changes.
        """
        now = self.ctx.clock.now()
        today = now.date()
        if self._intraday_vix_capture_date == today:
            return  # already captured today
        if now.time() < time(9, 15):
            return  # market not open yet
        vix = self.ctx.get_vix()
        if vix <= 0:
            return  # VIX feed not yet ready
        self._intraday_vix_morning = vix
        self._intraday_vix_capture_date = today

    def _check_intraday_vix_spike_filter(self) -> str | None:
        """Phase 3b Gate B — block entries on intraday VIX spike days.

        Pre-registered per reports/phase3b_research/regime_gate_proposal.md.
        Default disabled; opt-in via ``intraday_vix_spike_enabled`` param.

        Trigger: after ``intraday_vix_spike_activate_after`` time, if
        current VIX exceeds morning-open VIX by ``intraday_vix_spike_threshold_pct``,
        return a reason string. Caller treats this as a hard block.

        Returns None if the gate is disabled, not yet active, or not triggered.
        """
        if not getattr(self.params, "intraday_vix_spike_enabled", False):
            return None
        self._capture_morning_vix_if_needed()
        if self._intraday_vix_morning is None:
            return None  # morning VIX not yet captured (pre-9:15 or feed cold)
        activate_after = getattr(
            self.params, "intraday_vix_spike_activate_after", time(11, 30)
        )
        now_t = self.ctx.clock.now().time()
        if now_t < activate_after:
            return None  # gate not yet active for the day
        vix = self.ctx.get_vix()
        if vix <= 0:
            return None
        threshold_pct = float(
            getattr(self.params, "intraday_vix_spike_threshold_pct", 15.0)
        )
        ratio = vix / self._intraday_vix_morning
        threshold_ratio = 1.0 + threshold_pct / 100.0
        if ratio > threshold_ratio:
            spike_pct = (ratio - 1.0) * 100.0
            logger.debug(
                "filter blocked entry: intraday VIX spike",
                extra={
                    "tag": Tag.FILTER,
                    "strategy": self.strategy_id,
                    "filter": "intraday_vix_spike",
                    "morning_open": round(self._intraday_vix_morning, 2),
                    "current": round(vix, 2),
                    "spike_pct": round(spike_pct, 1),
                    "threshold_pct": threshold_pct,
                    "result": "block",
                },
            )
            return (
                f"intraday VIX spike: morning_open={self._intraday_vix_morning:.2f} "
                f"current={vix:.2f} (+{spike_pct:.1f}%) > threshold {threshold_pct:.0f}%"
            )
        return None

    def _get_vix_adjusted_lots(self) -> int:
        """Return quantity_lots adjusted for VIX regime.

        If VIX is above vix_reduce_above, halve the lots (minimum 1).
        """
        vix = self.ctx.get_vix()
        lots = self.params.quantity_lots
        if vix > 0 and vix > self.params.vix_reduce_above:
            lots = max(1, lots // 2)
            logger.info(
                f"[{self.strategy_id}] VIX={vix:.1f} > {self.params.vix_reduce_above}, "
                f"reducing lots to {lots}"
            )
        return lots

    def _check_pcr_filter(self, underlying: str, expiry: "date | None") -> str | None:
        """Check if Put-Call Ratio (OI) is within healthy range for premium selling.

        Returns None if OK to enter, or a reason string if entry should be skipped.
        When pcr_filter_enabled=False, always returns None but still logs.
        """
        if not expiry:
            return None
        chain = self.ctx.get_option_chain(underlying, expiry)
        if not chain or not chain.strikes:
            return None
        pcr = chain.pcr_oi
        if pcr <= 0:
            return None  # Not enough OI data yet

        in_range = self.params.pcr_oi_min <= pcr <= self.params.pcr_oi_max
        action = "PASS" if in_range else "WOULD_BLOCK"
        if self.params.pcr_filter_enabled and not in_range:
            action = "BLOCK"

        logger.info(
            "filter evaluated: pcr_oi",
            extra={
                "tag": Tag.FILTER,
                "strategy": self.strategy_id,
                "filter": "pcr_oi",
                "value": round(pcr, 2),
                "range_min": self.params.pcr_oi_min,
                "range_max": self.params.pcr_oi_max,
                "action": action,
            },
        )

        if self.params.pcr_filter_enabled and not in_range:
            return f"PCR_OI {pcr:.2f} outside range [{self.params.pcr_oi_min}-{self.params.pcr_oi_max}]"
        return None

    def _check_max_pain_filter(self, underlying: str, expiry: "date | None") -> str | None:
        """Check if spot is near max pain (favorable for premium sellers).

        Returns None if OK to enter, or a reason string if entry should be skipped.
        When max_pain_filter_enabled=False, always returns None but still logs.
        """
        if not expiry:
            return None
        chain = self.ctx.get_option_chain(underlying, expiry)
        if not chain or not chain.strikes:
            return None
        max_pain = chain.max_pain
        spot = chain.spot_price
        if max_pain <= 0 or spot <= 0:
            return None

        distance_pct = abs(float(spot) - float(max_pain)) / float(spot) * 100
        in_range = distance_pct <= self.params.max_pain_proximity_pct
        action = "PASS" if in_range else "WOULD_BLOCK"
        if self.params.max_pain_filter_enabled and not in_range:
            action = "BLOCK"

        logger.info(
            "filter evaluated: max_pain",
            extra={
                "tag": Tag.FILTER,
                "strategy": self.strategy_id,
                "filter": "max_pain",
                "max_pain": float(max_pain),
                "spot": float(spot),
                "distance_pct": round(distance_pct, 2),
                "threshold_pct": self.params.max_pain_proximity_pct,
                "action": action,
            },
        )

        if self.params.max_pain_filter_enabled and not in_range:
            return (
                f"Spot {spot} is {distance_pct:.2f}% from max pain {max_pain} "
                f"(threshold: {self.params.max_pain_proximity_pct}%)"
            )
        return None

    def _log_iv_skew(self, underlying: str, expiry: "date | None") -> None:
        """Log IV skew data at entry time for research/analysis."""
        if not expiry:
            return
        chain = self.ctx.get_option_chain(underlying, expiry)
        if not chain or not chain.strikes:
            return
        from src.options.chain_analyzer import get_iv_skew
        skew = get_iv_skew(chain)
        atm_ce_iv = skew.get("atm_iv_ce", 0)
        atm_pe_iv = skew.get("atm_iv_pe", 0)

        otm_puts = skew.get("otm_puts", [])
        otm_calls = skew.get("otm_calls", [])
        avg_put_iv = sum(p["iv"] for p in otm_puts) / len(otm_puts) if otm_puts else 0
        avg_call_iv = sum(c["iv"] for c in otm_calls) / len(otm_calls) if otm_calls else 0

        ratio = avg_put_iv / avg_call_iv if avg_call_iv > 0 else 0
        bias = "PUT_HEAVY" if ratio > 1.15 else ("CALL_HEAVY" if ratio < 0.85 else "NEUTRAL")

        logger.info(
            "iv skew snapshot at entry",
            extra={
                "tag": Tag.FILTER,
                "strategy": self.strategy_id,
                "filter": "iv_skew",
                "atm_ce_iv": round(atm_ce_iv, 3),
                "atm_pe_iv": round(atm_pe_iv, 3),
                "avg_otm_put_iv": round(avg_put_iv, 3),
                "avg_otm_call_iv": round(avg_call_iv, 3),
                "ratio": round(ratio, 2),
                "bias": bias,
            },
        )

    def _log_oi_levels(self, underlying: str, expiry: "date | None") -> None:
        """Log high-OI levels (support/resistance) at entry time for research."""
        if not expiry:
            return
        chain = self.ctx.get_option_chain(underlying, expiry)
        if not chain or not chain.strikes:
            return
        from src.options.chain_analyzer import get_high_oi_strikes
        oi_data = get_high_oi_strikes(chain, top_n=3)
        ce_levels = [f"{s['strike']}({s['oi']})" for s in oi_data.get("ce_high_oi", [])]
        pe_levels = [f"{s['strike']}({s['oi']})" for s in oi_data.get("pe_high_oi", [])]

        logger.info(
            "oi levels snapshot at entry",
            extra={
                "tag": Tag.FILTER,
                "strategy": self.strategy_id,
                "filter": "oi_levels",
                "ce_resistance": ce_levels,
                "pe_support": pe_levels,
            },
        )

    def _check_trend_filter(self, underlying: str) -> str | None:
        """Check if market is trending too strongly for premium selling.

        Uses morning range (9:15-9:30 open/close) vs current spot.
        If spot has moved > 0.5% from open, market is trending — skip entry.
        Returns None if OK, or a reason string to skip.
        """
        from src.core.types import Timeframe

        spot = self.ctx.get_spot_price(underlying)
        if spot <= 0:
            return None

        # Use 15-minute candles to check morning range
        chain_builder = self.ctx._chain_builder
        spot_token = None
        for token, name in chain_builder._spot_tokens.items():
            if name == underlying:
                spot_token = token
                break

        if not spot_token:
            return None

        candles = self.ctx.get_candles(spot_token, Timeframe.M15, limit=3)
        if not candles:
            return None

        # Use first candle's open as the session reference
        session_open = float(candles[0].open)
        if session_open <= 0:
            return None

        move_pct = abs(float(spot) - session_open) / session_open * 100
        threshold = 0.5  # Tightened from 0.7% — real data shows 0.5%+ moves lead to losses
        logger.debug(
            "filter evaluated: trend",
            extra={
                "tag": Tag.FILTER,
                "strategy": self.strategy_id,
                "filter": "trend",
                "session_open": round(session_open, 2),
                "spot": round(float(spot), 2),
                "move_pct": round(move_pct, 2),
                "threshold_pct": threshold,
                "result": "block" if move_pct > threshold else "pass",
            },
        )
        if move_pct > threshold:
            direction = "up" if float(spot) > session_open else "down"
            return (
                f"Market trending {direction} {move_pct:.2f}% from open "
                f"({session_open:.0f} -> {float(spot):.0f})"
            )
        return None

    # ─── P1.5 regime gate ─────────────────────────────────────────────
    #
    # Runtime regime classifier whose thresholds are a *verbatim* copy of
    # ``src/backtest/validation/regime.bucket_row``. The harness validation
    # report slices post-hoc decisions by those exact labels, so gating on
    # them at runtime provably removes trades from the matching bucket —
    # if we invented our own thresholds, the stratifier gate could still
    # fail even after "blocking" a regime. See
    # reports/validation/short_baseline_portfolio.md for the gate outputs.
    #
    # Keep this tied to the stratifier source file: any change there (e.g.
    # VIX band shift) MUST be mirrored here, or the two classifiers drift
    # and the regime gate stops being a meaningful measurement.

    def _move_from_open_pct(self, underlying: str) -> float | None:
        """Signed % move from the first 15-min candle's open.

        Matches the ``move_from_open_pct`` column the stratifier consumes.
        Returns None when spot or session-open is unavailable (label
        evaluation then simply skips the trending/range_bound labels).
        """
        from src.core.types import Timeframe

        spot = self.ctx.get_spot_price(underlying)
        if not spot or spot <= 0:
            return None

        chain_builder = self.ctx._chain_builder
        spot_token = None
        for token, name in chain_builder._spot_tokens.items():
            if name == underlying:
                spot_token = token
                break
        if not spot_token:
            return None

        candles = self.ctx.get_candles(spot_token, Timeframe.M15, limit=3)
        if not candles:
            return None

        session_open = float(candles[0].open)
        if session_open <= 0:
            return None

        return (float(spot) - session_open) / session_open * 100.0

    def _current_regime_labels(
        self, underlying: str, expiry: "date | None" = None
    ) -> list[str]:
        """Classify the current tick into harness-compatible regime labels.

        Thresholds are intentionally duplicated from
        ``src/backtest/validation/regime.bucket_row`` rather than imported,
        because the harness function operates on a pandas row (post-hoc
        decision log) while this helper queries live context. The *values*
        must stay in lockstep with the harness — don't tune one without
        the other.
        """
        labels: list[str] = []

        # VIX band (stratifier: >15 high, 13-15 mid, <13 low)
        vix = self.ctx.get_vix()
        if vix and vix > 0:
            if vix > 15:
                labels.append("high_vix")
            elif vix >= 13:
                labels.append("mid_vix")
            else:
                labels.append("low_vix")

        # Expiry week (dte <= 2 OR is_expiry-today)
        if expiry is not None:
            today = self.ctx.clock.now().date()
            dte = (expiry - today).days
            if dte <= 2:
                labels.append("expiry_week")

        # Event day (cached at first call to avoid per-tick CSV read)
        if not hasattr(self, "_regime_event_dates"):
            try:
                from src.backtest.validation.regime import load_event_dates
                self._regime_event_dates = load_event_dates()
            except (ImportError, OSError) as exc:
                logger.debug(
                    "[%s] event_dates load failed: %s", self.strategy_id, exc
                )
                self._regime_event_dates = {}
        today_d = self.ctx.clock.now().date()
        if today_d in self._regime_event_dates:
            labels.append("event_day")

        # Trending vs range_bound on |move_from_open_pct|
        move_pct = self._move_from_open_pct(underlying)
        if move_pct is not None:
            m = abs(move_pct)
            if m > 1.0:
                labels.append("trending")
            if m <= 0.5:
                labels.append("range_bound")

        return labels

    def _check_blocked_regime(
        self,
        underlying: str,
        expiry: "date | None" = None,
        blocked: list[str] | None = None,
    ) -> str | None:
        """Skip-reason if the current tick hits any blocked regime label.

        ``blocked`` overrides ``params.blocked_regimes`` when provided —
        used by multi-leg strategies like Portfolio that maintain per-leg
        blocklists (premium_blocked_regimes vs trend_blocked_regimes).
        Default ``None`` falls back to the single-list convention on the
        strategy params.

        Returns None when the blocklist is empty or no active label
        matches. Otherwise returns a string describing the first
        matching label and the full active-label set (useful for skip-
        log diagnosis — on a tick where multiple labels fire, you want
        to know which one triggered the block).
        """
        if blocked is None:
            blocked = list(getattr(self.params, "blocked_regimes", []) or [])
        else:
            blocked = list(blocked)
        if not blocked:
            return None

        labels = self._current_regime_labels(underlying, expiry)
        hit = [r for r in labels if r in blocked]
        if not hit:
            return None

        logger.debug(
            "filter blocked entry: regime",
            extra={
                "tag": Tag.FILTER,
                "strategy": self.strategy_id,
                "filter": "blocked_regime",
                "labels": labels,
                "blocked_hit": hit,
                "result": "block",
            },
        )
        return f"Regime {hit[0]} blocked (active: {labels})"

    def _check_expiry_rollover(self, current_expiry: "date | None", underlying: str) -> "date | None":
        """If the current expiry is in the past, roll to the next one.

        Returns the new expiry if rolled, or None if no rollover needed.
        """
        if current_expiry is None:
            return None
        today = self.ctx.clock.now().date()
        if today > current_expiry:
            new_expiry = self.ctx.next_expiry(underlying)
            logger.info(
                f"[{self.strategy_id}] Expiry rollover: {current_expiry} -> {new_expiry}"
            )
            return new_expiry
        return None

    def _log_decision(
        self,
        decision: str,
        *,
        leg: str = "",
        mode: str = "",
        rule_score: int = 0,
        threshold: int = 0,
        entry_premium: float = 0.0,
        quantity: int = 0,
        exit_reason: str = "",
        outcome_pnl: float | None = None,
        held_minutes: int | None = None,
    ) -> None:
        """Write a DecisionSnapshot row for this strategy (common fields only).

        portfolio_strategy still has its own richer `_build_snapshot` with
        the Phase A trend-score breakdown — this helper covers the bare
        minimum (timestamp, spot, vix, dte, score, threshold, entry/exit
        premium, outcome P&L, held minutes) that's enough for cross-
        strategy attribution and ML labelling. Other strategies should
        call this from their _try_entry success path and _create_exit_signal.

        Wrapped in a broad except — decision logging is observational,
        never block a trade for a logger error. Apr 20 audit: 4 of 5
        strategies wrote zero rows and we had no idea why each strategy
        skipped each tick. This fills that gap without coupling logging
        failures to trading correctness.
        """
        try:
            now = self.ctx.clock.now()
            # Best-effort spot/vix — these can fail silently if context
            # isn't fully wired (e.g. very early on_start path).
            try:
                underlying = getattr(self.params, "underlying", "")
                spot = float(self.ctx.get_spot_price(underlying)) if underlying else 0.0
            except Exception:
                spot = 0.0
            try:
                vix = float(self.ctx.get_vix())
            except Exception:
                vix = 0.0

            # DTE if the strategy exposes _expiry (most do)
            expiry = getattr(self, "_expiry", None)
            dte = (expiry - now.date()).days if expiry else 0
            is_expiry = 1 if expiry and now.date() == expiry else 0

            # Track entry/exit timing so we can fill held_minutes on EXIT
            # rows without each strategy plumbing it through.
            if decision == "ENTER":
                self._decision_entry_ts = now
            if decision == "EXIT" and held_minutes is None and self._decision_entry_ts:
                delta = now - self._decision_entry_ts
                held_minutes = int(delta.total_seconds() // 60)

            # Apr 29 2026 audit: snapshot cumulative strategy-level charges
            # at ENTER, write the entry→exit delta on EXIT. Lets downstream
            # stratifiers compute net-of-charges PnL without re-deriving
            # per-trade STT/brokerage/GST/SEBI from broker.trades. Wrapped
            # in try/except — charges are observational, never block
            # logging if the portfolio path isn't wired (e.g. early
            # on_start, unit-test fixtures with mocked context).
            charges_now: float = 0.0
            try:
                pnl = self.ctx.get_pnl()
                charges_now = float(pnl.charges) if pnl is not None else 0.0
            except Exception:
                charges_now = 0.0
            charges_for_row: float = 0.0
            if decision == "ENTER":
                self._charges_at_entry = charges_now
            elif decision == "EXIT":
                charges_for_row = max(0.0, charges_now - self._charges_at_entry)
                # Reset for the next ENTER cycle
                self._charges_at_entry = charges_now

            # Apr 29 2026 Phase 1C: stable trade lifecycle id.
            #   ENTER  → mint a fresh id (UUID4 short form)
            #   ADJUST → reuse the active id (no change)
            #   EXIT   → reuse the active id, then clear after the row
            #            is built so the next ENTER starts a new trade
            if decision == "ENTER":
                import uuid
                self._current_trade_id = uuid.uuid4().hex[:8]
            trade_id_for_row = self._current_trade_id

            snap = DecisionSnapshot(
                timestamp=now.isoformat(),
                strategy_id=self.strategy_id,
                leg=leg,
                decision=decision,
                mode=mode,
                spot=spot,
                vix=vix,
                dte=dte,
                hour=now.hour,
                minute=now.minute,
                day_of_week=now.weekday(),
                is_expiry=is_expiry,
                rule_score=rule_score,
                threshold=threshold,
                entry_premium=entry_premium,
                quantity=quantity,
                exit_reason=exit_reason,
                outcome_pnl=outcome_pnl,
                held_minutes=held_minutes,
                charges=charges_for_row,
                trade_id=trade_id_for_row,
            )
            self._decision_logger.log(snap)
            # Clear the trade id AFTER writing the EXIT row so the row
            # itself carries the id but a subsequent ENTER mints a new
            # one. ADJUST rows leave the id in place.
            if decision == "EXIT":
                self._current_trade_id = ""
        except Exception:
            logger.exception(
                f"[DECISION] {self.strategy_id} log failed (decision={decision})"
            )

    def _log_adjust_decision(
        self,
        *,
        leg: str,
        mode: str,
        side_label: str,
        side_realized_pnl: float,
        new_entry_premium: float = 0.0,
    ) -> None:
        """Emit an ADJUST decision row mid-position.

        Apr 29 2026 Phase 1C — when a strategy rolls a leg/wing, the
        closed leg(s) have realised P&L that the prior decision-log
        schema dropped on the floor (only ENTER and EXIT rows existed,
        with EXIT's outcome_pnl computed against the *post-roll*
        entry credit). This helper captures the realized P&L attributed
        to that specific roll under the same trade_id as the ENTER, so
        downstream stratifiers can sum (ENTER → ADJUSTs → EXIT) for the
        true lifecycle P&L.

        Parameters
        ----------
        leg
            Logical leg label — same convention as ENTER/EXIT
            (PREMIUM / TREND).
        mode
            Strategy mode string (e.g. iron_condor, strangle).
        side_label
            Which side was rolled (CE / PE / FRONT / etc.) — written
            into ``exit_reason`` so the CSV remains parseable without
            a new column. The naming "exit_reason for an ADJUST" is
            mildly off but keeps the schema stable per the
            "append, never reorder" convention.
        side_realized_pnl
            Realised P&L (₹) for the closed leg(s) of this adjustment.
            Positive = profit captured by the roll, negative = loss
            locked in.
        new_entry_premium
            Premium / credit / debit of the freshly-opened legs after
            the roll, recorded into ``entry_premium`` so the post-roll
            basis is auditable. 0 if the strategy didn't open new legs.
        """
        self._log_decision(
            "ADJUST",
            leg=leg,
            mode=mode,
            entry_premium=new_entry_premium,
            exit_reason=f"ADJUST {side_label}",
            outcome_pnl=side_realized_pnl,
        )

    def reset_day_state(self) -> None:
        """Reset intraday flags at start of a new trading day.

        Override in subclasses that use _entered / _stopped_for_day flags.
        Subclasses that override MUST call super().reset_day_state() so the
        skip-log dedup map is cleared — otherwise yesterday's stale minute
        keys would silently swallow this morning's first occurrence of each
        reason. (See test_portfolio_skip_log_dedup.test_dedup_state_clears_on_session_reset)
        """
        if hasattr(self, "_entered"):
            self._entered = False
        if hasattr(self, "_stopped_for_day"):
            self._stopped_for_day = False
        # Clear skip-log dedup memory so the first skip on the new session
        # logs cleanly (yesterday's "minute 630" key would still be in the
        # dict and would suppress today's 10:30 line if we didn't clear).
        self._last_skip_log_minute.clear()

    def get_state_data(self) -> dict:
        """Serialize strategy-specific state for persistence.

        Override in subclasses to save custom state (e.g., legs, adjustments).
        """
        return {}

    def load_state_data(self, data: dict) -> None:
        """Restore strategy-specific state from persistence.

        Override in subclasses to restore custom state.
        """
        pass


# Forward reference for type hint
from src.strategy.context import StrategyContext  # noqa: E402
