"""Base strategy abstract class — the contract all strategies implement."""

import logging
from abc import ABC, abstractmethod
from typing import Any

from src.core.models import OHLC, Order, Signal, Subscription, Tick
from src.core.types import StrategyState
from src.strategy.decision_logger import DecisionLogger, DecisionSnapshot
from src.strategy.params import BaseStrategyParams
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
        """Force-exit all open positions for this strategy with MARKET orders."""
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
        logger.info(
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

    def _check_vix_filter(self) -> str | None:
        """Check if VIX is within the strategy's allowed band.

        Blocks entry when VIX < vix_entry_min (complacency, premium too cheap)
        or VIX > vix_entry_max (event/stress beyond strategy tolerance).
        Returns None if OK, or a reason string to skip.
        """
        vix = self.ctx.get_vix()
        if vix <= 0:
            return None  # VIX data unavailable, allow entry
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
            )
            self._decision_logger.log(snap)
        except Exception:
            logger.exception(
                f"[DECISION] {self.strategy_id} log failed (decision={decision})"
            )

    def reset_day_state(self) -> None:
        """Reset intraday flags at start of a new trading day.

        Override in subclasses that use _entered / _stopped_for_day flags.
        """
        if hasattr(self, "_entered"):
            self._entered = False
        if hasattr(self, "_stopped_for_day"):
            self._stopped_for_day = False

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
