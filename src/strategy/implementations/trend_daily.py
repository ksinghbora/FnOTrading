"""Trend Daily strategy — multi-day Donchian breakout on NIFTY.

May 7 2026 — first trend variant in the post-SEBI research arc to
produce positive net PnL on a 360-day train+val smoke (commit b34aefe).
Intraday Donchian variants (1-min/5-min/15-min/30-min) all sat below
the cost wall; the daily timeframe amortizes round-trip cost across
multi-day holds (~22 days average).

Structure:
  Entry long:  daily close > 20-day high of last 21 daily closes
  Entry short: symmetric on 20-day low
  Filters:     VIX (daily mean) in [12, 22], ATR(14)/spot ≥ 0.5%
  Exit:        ATR(14) trailing stop at 2× ATR
               OR reverse on opposite breakout
               OR max-hold N days (default 30)

Decision check fires ONCE per trading day at ``decision_time`` (default
15:25 IST — five minutes before close so market-on-close orders can be
placed). Multi-day position: ``_entered`` persists across days;
``reset_day_state`` is a no-op for the entry flag.

Cost basis target: NIFTY current-month futures (1bp round-trip
slippage). The strategy can be wired to trade futures or to a
futures-equivalent vehicle. The spot-index is cash-settled and not
directly tradeable; see PIVOT_DESIGN_trend_futures.md for the
execution-leg discussion.
"""

from __future__ import annotations

import logging
import os
from collections import deque
from datetime import date, time, timedelta
from decimal import Decimal

from src.core.constants import LOT_SIZES, INDIA_VIX_TOKEN
from src.core.models import Signal, SignalLeg, Subscription, Tick
from src.core.types import OrderSide, OrderType, SignalType
from src.strategy.base import BaseStrategy
from src.strategy.params import TrendDailyParams
from src.strategy.registry import register_strategy
from src.strategy.signals import entry_signal, exit_signal, make_leg

logger = logging.getLogger(__name__)


@register_strategy("trend_daily", TrendDailyParams)
class TrendDailyStrategy(BaseStrategy):
    """Daily Donchian trend on NIFTY (or other configurable underlying).

    Default config: 20-day Donchian, 14-period ATR, VIX 12-22 band,
    decision check at 15:25 IST, 1-lot NIFTY.
    """

    params: TrendDailyParams
    # V5: multi-day Donchian directional trend on NIFTY futures.
    regime_family: str = "directional_trend"

    def __init__(self, strategy_id: str, params: TrendDailyParams):
        super().__init__(strategy_id, params)
        self._lot_size: int = LOT_SIZES.get(params.underlying, 75)
        self._quantity: int = params.quantity_lots * self._lot_size
        self._paper_mode: bool = os.environ.get("PAPER_TRADING", "false").lower() == "true"

        # Daily-bar buffer — store last (donchian_lookback + atr_period + 5) days for safety
        buf_size = params.donchian_lookback + params.atr_period + 5
        self._daily_bars: deque = deque(maxlen=buf_size)
        # VIX rolling mean — use last 20 daily means for stability vs intraday spikes
        self._daily_vix: deque = deque(maxlen=buf_size)

        # Today's running OHLC (built from ticks)
        self._today_date: date | None = None
        self._today_open: float = 0.0
        self._today_high: float = 0.0
        self._today_low: float = 0.0
        self._today_close: float = 0.0
        # VIX accumulator (mean over the day)
        self._today_vix_sum: float = 0.0
        self._today_vix_count: int = 0

        # Decision-fired flag (one decision per day)
        self._decision_fired_today: bool = False

        # Position state
        self._position: int = 0  # +1 long, -1 short, 0 flat
        self._entry_px: float = 0.0
        self._entry_date: date | None = None
        self._entry_atr: float = 0.0
        self._peak_favorable: float = 0.0
        self._days_held: int = 0

        # Spot token resolved at on_start
        self._spot_token: int = 0

    def get_subscriptions(self) -> Subscription:
        return Subscription(instrument_tokens=[], timeframes=[])

    async def on_start(self) -> None:
        # Resolve spot token via the chain builder reverse-lookup.
        spot_token = self.ctx.get_spot_token(self.params.underlying)
        if spot_token is None:
            logger.warning(
                f"[{self.strategy_id}] No spot token for {self.params.underlying} — "
                f"trend_daily will not fire"
            )
        else:
            self._spot_token = spot_token

        # May 7 2026: warm up daily-bar history from broker historical
        # data so the strategy can fire from day 1 post-restart. The
        # signal needs (donchian_lookback + 1) = 21+ daily closes to
        # compute Donchian bands. Without warmup the buffer accumulates
        # one bar per trading day and signal goes live in ~21 days.
        if self._spot_token:
            await self._warmup_daily_bars()

        logger.info(
            f"[{self.strategy_id}] Started: {self.params.underlying} "
            f"lots={self.params.quantity_lots} "
            f"donchian={self.params.donchian_lookback}d atr={self.params.atr_period}d "
            f"vix=[{self.params.vix_entry_min}, {self.params.vix_entry_max}] "
            f"max_hold={self.params.max_hold_days}d "
            f"buf_size={len(self._daily_bars)}/{self._daily_bars.maxlen}"
        )

    async def _warmup_daily_bars(self) -> None:
        """Seed the daily-bar buffer from broker historical data.

        Fetches ~50 calendar days of daily bars (covers 30+ trading
        days) so the buffer is full at strategy start. Failure is
        non-fatal — the buffer accumulates from live ticks instead.
        """
        from datetime import datetime as _dt
        try:
            now = self.ctx.clock.now() if hasattr(self.ctx, "clock") else _dt.now()
            from_date = now - timedelta(days=60)
            spot_bars = await self.ctx.get_historical_data(
                self._spot_token, from_date, now, "day",
            )
            vix_bars = await self.ctx.get_historical_data(
                INDIA_VIX_TOKEN, from_date, now, "day",
            )
        except Exception as e:
            logger.warning(f"[{self.strategy_id}] daily-bar warmup failed: {e}")
            return

        # Index VIX by date for join
        vix_by_date: dict[date, float] = {}
        for vb in vix_bars or []:
            try:
                d = vb["date"]
                if hasattr(d, "date"):
                    d = d.date()
                vix_by_date[d] = float(vb["close"])
            except (KeyError, TypeError, ValueError):
                continue

        seeded = 0
        for sb in (spot_bars or [])[:-1]:  # exclude today (still in progress)
            try:
                d = sb["date"]
                if hasattr(d, "date"):
                    d = d.date()
                bar = {
                    "date": d,
                    "open": float(sb["open"]),
                    "high": float(sb["high"]),
                    "low": float(sb["low"]),
                    "close": float(sb["close"]),
                }
                self._daily_bars.append(bar)
                if d in vix_by_date:
                    self._daily_vix.append(vix_by_date[d])
                seeded += 1
            except (KeyError, TypeError, ValueError):
                continue

        logger.info(
            f"[{self.strategy_id}] daily-bar warmup: seeded {seeded} bars "
            f"(buf={len(self._daily_bars)}, vix_buf={len(self._daily_vix)})"
        )

    async def on_tick(self, tick: Tick) -> Signal | None:
        # We only care about the spot-index tick stream + VIX
        token = tick.instrument_token
        if token != self._spot_token and token != INDIA_VIX_TOKEN:
            return None

        now = self.ctx.clock.now()
        today = now.date()

        # ─── Day rollover: finalise yesterday's bar ───────────────────
        if self._today_date is None:
            self._today_date = today
        elif today != self._today_date:
            # New day — push yesterday's bar to the buffer
            if self._today_close > 0:
                self._daily_bars.append({
                    "date": self._today_date,
                    "open": self._today_open,
                    "high": self._today_high,
                    "low": self._today_low,
                    "close": self._today_close,
                })
                if self._today_vix_count > 0:
                    daily_vix = self._today_vix_sum / self._today_vix_count
                    self._daily_vix.append(daily_vix)
            # Reset today's accumulator
            self._today_date = today
            self._today_open = 0.0
            self._today_high = 0.0
            self._today_low = 0.0
            self._today_close = 0.0
            self._today_vix_sum = 0.0
            self._today_vix_count = 0
            self._decision_fired_today = False
            # Multi-day hold counter increments per new day
            if self._position != 0:
                self._days_held += 1

        # ─── Update today's running OHLC ──────────────────────────────
        ltp = float(tick.ltp) if tick.ltp else 0.0
        if ltp <= 0:
            return None

        if token == INDIA_VIX_TOKEN:
            # Accumulate VIX into today's mean
            self._today_vix_sum += ltp
            self._today_vix_count += 1
            return None

        # Spot tick
        if self._today_open == 0.0:
            self._today_open = ltp
            self._today_high = ltp
            self._today_low = ltp
        else:
            if ltp > self._today_high:
                self._today_high = ltp
            if ltp < self._today_low:
                self._today_low = ltp
        self._today_close = ltp

        # ─── Decision check fires once per day at decision_time ───────
        if self._decision_fired_today:
            return None
        if now.time() < self.params.decision_time:
            return None

        self._decision_fired_today = True
        return self._evaluate()

    def _evaluate(self) -> Signal | None:
        """Evaluate entry/exit using yesterday's-and-earlier daily bars
        plus today's running close as the latest 'close'."""
        # Need at least donchian_lookback + 1 historical bars + today.
        n_bars = len(self._daily_bars)
        n_required = self.params.donchian_lookback + 1
        if n_bars < n_required:
            self._log_skip_throttled(
                "ENTRY_SKIP_WARMUP",
                f"[{self.strategy_id}] Warming up: {n_bars}/{n_required} daily bars",
            )
            return None

        # Donchian: last `donchian_lookback` bars from buffer (excluding today)
        recent = list(self._daily_bars)[-self.params.donchian_lookback:]
        donchian_high = max(b["high"] for b in recent)
        donchian_low = min(b["low"] for b in recent)

        # ATR(period) on the buffer
        atr = self._compute_atr(self.params.atr_period)
        if atr is None or atr <= 0:
            return None

        # Today's running close (used as the latest "close")
        close = self._today_close
        atr_pct = atr / close * 100 if close > 0 else 0.0

        # Mean VIX today (informational and used as the gate)
        if self._today_vix_count > 0:
            vix_today = self._today_vix_sum / self._today_vix_count
        elif self._daily_vix:
            vix_today = self._daily_vix[-1]
        else:
            vix_today = self.ctx.get_vix() or 0.0

        # ─── Exit checks (priority over entry on the same evaluation) ─
        if self._position != 0:
            # Hard max-hold
            if self._days_held >= self.params.max_hold_days:
                return self._create_exit_signal("max_hold")

            if self._position > 0:
                if close > self._peak_favorable:
                    self._peak_favorable = close
                trail = self._peak_favorable - self.params.atr_stop_mult * self._entry_atr
                if close <= trail:
                    return self._create_exit_signal(
                        f"trail_stop ({close:.2f} <= {trail:.2f})"
                    )
                # Reverse on opposite breakout
                if close < donchian_low:
                    return self._create_exit_signal(
                        f"reverse_short (close {close:.2f} < donchian_low {donchian_low:.2f})"
                    )
            else:  # short
                if close < self._peak_favorable:
                    self._peak_favorable = close
                trail = self._peak_favorable + self.params.atr_stop_mult * self._entry_atr
                if close >= trail:
                    return self._create_exit_signal(
                        f"trail_stop ({close:.2f} >= {trail:.2f})"
                    )
                if close > donchian_high:
                    return self._create_exit_signal(
                        f"reverse_long (close {close:.2f} > donchian_high {donchian_high:.2f})"
                    )

        # ─── Entry checks (only when flat) ────────────────────────────
        if self._position != 0:
            return None

        # Filter: VIX band
        if vix_today < self.params.vix_entry_min or vix_today > self.params.vix_entry_max:
            self._log_skip_throttled(
                "ENTRY_SKIP_VIX",
                f"[{self.strategy_id}] Entry skipped: VIX {vix_today:.1f} outside "
                f"[{self.params.vix_entry_min}, {self.params.vix_entry_max}]",
            )
            return None

        # Filter: ATR floor
        if atr_pct < self.params.atr_floor_pct:
            self._log_skip_throttled(
                "ENTRY_SKIP_ATR",
                f"[{self.strategy_id}] Entry skipped: ATR%/spot {atr_pct:.3f}% < "
                f"floor {self.params.atr_floor_pct}%",
            )
            return None

        # Long breakout
        if close > donchian_high:
            return self._create_entry_signal(side=+1, close=close, atr=atr)
        # Short breakout
        if close < donchian_low:
            return self._create_entry_signal(side=-1, close=close, atr=atr)

        return None

    def _compute_atr(self, period: int) -> float | None:
        """Wilder ATR on the daily-bar buffer.

        Returns None if buffer has fewer than period+1 bars.
        """
        n = len(self._daily_bars)
        if n < period + 1:
            return None
        bars = list(self._daily_bars)
        # True range for each bar except the first (no prev_close)
        trs: list[float] = []
        for i in range(1, n):
            prev_close = bars[i - 1]["close"]
            tr = max(
                bars[i]["high"] - bars[i]["low"],
                abs(bars[i]["high"] - prev_close),
                abs(bars[i]["low"] - prev_close),
            )
            trs.append(tr)
        # Wilder's smoothing: simple alpha = 1/period EWM
        alpha = 1.0 / period
        atr = trs[0]
        for t in trs[1:]:
            atr = alpha * t + (1 - alpha) * atr
        return atr

    def _create_entry_signal(self, side: int, close: float, atr: float) -> Signal:
        """Build a futures-side BUY/SELL signal for the trend leg.

        Note: the underlying tradingsymbol resolution for futures is
        deferred — for v1 we record the entry symbolically and let the
        execution layer pick the current-month futures token. In live
        mode this requires a futures-instrument resolver that we'll
        add when the strategy is wired into main.py.
        """
        self._position = side
        self._entry_px = close
        self._entry_date = self._today_date
        self._entry_atr = atr
        self._peak_favorable = close
        self._days_held = 0

        side_label = "LONG" if side > 0 else "SHORT"
        order_side = OrderSide.BUY if side > 0 else OrderSide.SELL
        # For now use the underlying spot token / symbol as a placeholder.
        # In live deployment this needs to be replaced with the current-
        # month NIFTY futures token + tradingsymbol.
        symbol = f"{self.params.underlying}-FUT"
        leg = make_leg(
            symbol,
            self._spot_token,  # placeholder — replace with futures token
            order_side,
            self._quantity,
            order_type=OrderType.MARKET,
        )
        logger.info(
            f"[ENTRY] strategy={self.strategy_id} type=trend_daily "
            f"side={side_label} underlying={self.params.underlying} "
            f"close={close:.2f} entry_atr={atr:.2f} "
            f"qty={self._quantity}"
        )
        self._log_decision(
            "ENTER",
            leg=side_label,
            mode="trend_daily",
            entry_premium=close,
            quantity=self._quantity,
        )
        return entry_signal(
            self.strategy_id,
            [leg],
            f"Trend Daily {side_label} @ {close:.2f}, atr={atr:.2f}",
        )

    def _create_exit_signal(self, reason: str) -> Signal:
        """Reverse the position at market."""
        side = self._position
        side_label = "LONG" if side > 0 else "SHORT"
        # Exit = opposite-side order
        exit_side = OrderSide.SELL if side > 0 else OrderSide.BUY
        symbol = f"{self.params.underlying}-FUT"
        leg = make_leg(
            symbol,
            self._spot_token,  # placeholder
            exit_side,
            self._quantity,
            order_type=OrderType.MARKET,
        )
        pnl_pts = (self._today_close - self._entry_px) * side
        pnl = pnl_pts * self._quantity
        logger.info(
            f"[EXIT] strategy={self.strategy_id} reason={reason} "
            f"side={side_label} entry_px={self._entry_px:.2f} "
            f"exit_px={self._today_close:.2f} days_held={self._days_held} "
            f"pnl={pnl:+.0f}"
        )
        self._log_decision(
            "EXIT",
            leg=side_label,
            mode="trend_daily",
            entry_premium=self._entry_px,
            quantity=self._quantity,
            exit_reason=reason,
            outcome_pnl=pnl,
        )
        # Reset position state
        self._position = 0
        self._entry_px = 0.0
        self._entry_date = None
        self._entry_atr = 0.0
        self._peak_favorable = 0.0
        self._days_held = 0
        return exit_signal(self.strategy_id, [leg], reason)

    def evaluate_score(self) -> int:
        """Score for orchestrator — 0 if not eligible, 60+VIX-fit otherwise."""
        try:
            if len(self._daily_bars) < self.params.donchian_lookback + 1:
                return 0
            vix = self.ctx.get_vix()
            if vix <= 0 or vix < self.params.vix_entry_min or vix > self.params.vix_entry_max:
                return 0
            band = max(1.0, self.params.vix_entry_max - self.params.vix_entry_min)
            band_pos = (vix - self.params.vix_entry_min) / band
            # Best near band middle (where momentum is most likely)
            fit = max(0, 20 - int(abs(band_pos - 0.5) * 40))
            return 60 + fit
        except Exception:
            return 0

    async def on_stop(self) -> None:
        if self._position != 0:
            logger.info(
                f"[{self.strategy_id}] Stopping with open position: "
                f"side={self._position} entry={self._entry_px:.2f} "
                f"days_held={self._days_held}"
            )

    def reset_day_state(self) -> None:
        """Reset INTRADAY state at start of new trading day.

        Critical: trend_daily is a MULTI-DAY strategy. Like
        long_calendar, _entered/_position must NOT reset daily — that
        would close + reopen positions every morning. Only the per-day
        skip-log dedup and the decision-fired flag reset. Position
        state and the days-held counter persist; they tick over via
        the day-rollover logic in on_tick.
        """
        self._last_skip_log_minute.clear()
        self._decision_fired_today = False
