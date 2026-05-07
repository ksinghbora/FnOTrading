"""Trend ITM strategy — Donchian breakout, single-leg deep-ITM CE/PE.

May 1 2026 pivot from the premium-selling family. The post-SEBI
cross-strategy validation (reports/standalone_post_sebi/SUMMARY.md)
showed every premium-seller in the roster (iron_condor, short_strangle,
short_straddle, long_calendar) loses 3-8 Sharpe on the regime-clean
173-day post-SEBI corpus, with MC permutation p-values ≥ 0.9997 (worse
than random). Cost-at-zero gave Sharpe 0 across the board — i.e., the
losses aren't cost-driven, they're entry-driven (wrong-regime entries).

Trend-following inverts the bet. Profits on the breakouts that
destroyed our IC, and on trending regimes where premium-sellers bled
most. Single execution leg vs IC's 4 = 4× cost reduction.

Mechanics
---------

The signal lives on **spot** (already in the GDFL corpus). Each on_tick
spot update lands in a 1-min OHLC ring buffer. When the bar closes we
check:

  Long  entry:  close > 20-bar high + breakout_confirmation_pts
  Short entry:  close < 20-bar low  − breakout_confirmation_pts

Both sides additionally require:
  - VIX in [vix_entry_min, vix_entry_max]
  - ATR(14) ≥ atr_floor_pct_of_spot × spot
  - now between entry_time and last_entry_time

Execution: the GDFL corpus has no futures ticks, so we use a deep-ITM
single-leg option as a futures proxy. Long bias → BUY a CE strike
``itm_offset_pts`` BELOW spot (delta ~0.95). Short bias → BUY a PE
strike ``itm_offset_pts`` ABOVE spot (delta ~-0.95). Single leg, BUY
at LIMIT-mid via the base ``_build_option_leg`` helper.

Exit:
  - Trailing stop on spot: close beyond peak_favorable_spot ± 2 × ATR
  - Hard time stop at exit_time (square off intraday)
  - Premium hard SL: option premium drops more than stop_loss_pct from entry
  - Premium hard PT: option premium gains more than profit_target_pct

What v1 deliberately does NOT do
--------------------------------

- No multi-timeframe confirmation. One signal, one set of params.
- No vol-targeting position sizing. Fixed lot count.
- No overnight positions. exit_time hard squares off.
- No portfolio overlay or orchestrator integration. Standalone only.
- No ML / regime-conditional gating beyond the simple VIX band.

The point of v1 is to validate the simplest tractable trend primitive
on regime-clean post-SEBI NIFTY before adding any sophistication.
Validation criteria are pre-registered in
``reports/standalone_post_sebi/PIVOT_DESIGN_trend_futures.md``.
"""
from __future__ import annotations

import logging
import os
from collections import deque
from datetime import date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING

from src.core.constants import LOT_SIZES
from src.core.models import Signal, Subscription, Tick
from src.core.types import OrderSide
from src.strategy.base import BaseStrategy
from src.strategy.params import TrendITMParams
from src.strategy.registry import register_strategy
from src.strategy.signals import entry_signal, exit_signal

if TYPE_CHECKING:
    from src.core.models import OptionChain, OptionChainEntry

logger = logging.getLogger(__name__)


# Public for tests — number of bars we maintain in the ring buffer.
# Need lookback + 1 for Donchian, period + 1 for ATR(Wilder).
def _required_bars(lookback: int, atr_period: int) -> int:
    return max(lookback + 1, atr_period + 1)


@register_strategy("trend_itm", TrendITMParams)
class TrendITMStrategy(BaseStrategy):
    """Donchian-breakout trend-following on NIFTY spot, executed via
    deep-ITM single-leg CE (long bias) or PE (short bias)."""

    params: TrendITMParams
    # V5: deep-ITM single-leg directional trend (futures proxy).
    regime_family: str = "directional_trend"

    def __init__(self, strategy_id: str, params: TrendITMParams):
        super().__init__(strategy_id, params)

        # ─── Position state ──────────────────────────────────────
        self._entered: bool = False
        self._side: str = ""                # "long" / "short" — populated on entry
        self._entry_token: int = 0
        self._entry_symbol: str = ""
        self._entry_strike: float = 0.0
        self._entry_option_type: str = ""   # "CE" or "PE"
        self._entry_premium: Decimal = Decimal("0")
        self._entry_spot: float = 0.0
        self._peak_favorable_spot: float = 0.0
        self._stopped_for_day: bool = False
        self._trades_today: int = 0
        self._last_trade_date: date | None = None
        # v2 (May 1 2026): minimum hold period state. Trail-stop is
        # gated until ``self._entry_time + min_hold_minutes``. Set on
        # ENTER, cleared on EXIT.
        self._entry_datetime: datetime | None = None

        # ─── 1-min OHLC ring buffer on spot ──────────────────────
        # Each completed bar is (bar_close_minute_iso, open, high, low, close).
        # Bar in progress lives in self._current_bar; finalised on minute roll.
        self._bars: deque = deque(maxlen=_required_bars(
            params.donchian_lookback, params.atr_period
        ))
        self._current_bar: dict | None = None    # keys: minute, open, high, low, close
        # ATR(Wilder) state — running smoothed value, computed from completed bars
        self._atr: float = 0.0

        # ─── Misc ────────────────────────────────────────────────
        self._lot_size: int = LOT_SIZES.get(params.underlying, 75)
        self._quantity: int = params.quantity_lots * self._lot_size
        self._paper_mode: bool = (
            os.environ.get("PAPER_TRADING", "false").lower() == "true"
        )
        self._expiry: date | None = None  # Used by base helpers (DTE log etc.)

    # ─── Lifecycle ──────────────────────────────────────────────────

    def get_subscriptions(self) -> Subscription:
        return Subscription(instrument_tokens=[], timeframes=[])

    async def on_start(self) -> None:
        # Pick the nearest weekly expiry just like premium-sellers do —
        # the option leg lifetime is one trading day max (square-off
        # intraday) so weekly is fine; fewer DTE = lower theta in
        # absolute terms but our position is held for hours, not days.
        self._expiry = self.ctx.next_expiry(self.params.underlying)
        logger.info(
            f"[{self.strategy_id}] Started: {self.params.underlying} "
            f"expiry={self._expiry} lots={self.params.quantity_lots} "
            f"itm_offset_pts={self.params.itm_offset_pts}"
        )

    async def on_stop(self) -> None:
        if self._entered:
            logger.info(f"[{self.strategy_id}] Stopping with open position")

    def reset_day_state(self) -> None:
        """Per-day reset — clear today's trade counter + skip-log dedup.
        ``_entered`` is reset by the on_tick exit path; we only zero the
        counters here. Bars persist across days (Donchian uses last
        ``lookback`` bars regardless of date)."""
        today = self.ctx.clock.now().date() if self._context else None
        if today and self._last_trade_date != today:
            self._trades_today = 0
            self._last_trade_date = today
            self._stopped_for_day = False
        self._last_skip_log_minute.clear()

    # ─── Tick handling ──────────────────────────────────────────────

    async def on_tick(self, tick: Tick) -> Signal | None:
        now = self.ctx.clock.now()

        # Capture morning VIX baseline (used by the opt-in intraday-spike
        # filter inherited from BaseStrategy).
        self._capture_morning_vix_if_needed()

        # Per-day reset (trades counter, stopped flag)
        if self._last_trade_date != now.date():
            self._trades_today = 0
            self._last_trade_date = now.date()
            self._stopped_for_day = False

        # Roll forward weekly expiry whenever the front passes.
        new_expiry = self._check_expiry_rollover(self._expiry, self.params.underlying)
        if new_expiry:
            self._expiry = new_expiry

        # Maintain the 1-min spot OHLC ring buffer on EVERY tick.
        # Spot is the reference for the trend signal; the option leg
        # is just the execution mechanism.
        self._update_bars(now)

        # ── Exit path takes precedence ──────────────────────────
        if self._entered:
            exit_sig = self._check_exit_conditions(now)
            if exit_sig is not None:
                return exit_sig
            return None

        # ── Hard square-off at exit_time even if not entered ────
        # (No-op for already-not-entered, but sets stopped_for_day to
        # block any late-day re-entry.)
        if now.time() >= self.params.exit_time:
            self._stopped_for_day = True
            return None

        # ── Entry path ──────────────────────────────────────────
        if self._stopped_for_day:
            return None
        if self._trades_today >= self.params.max_trades_per_day:
            return None
        if now.time() < self.params.entry_time:
            return None
        if now.time() >= self.params.last_entry_time:
            return None

        return await self._try_entry(now)

    def _update_bars(self, now: datetime) -> None:
        """Maintain the 1-min spot OHLC ring buffer.

        Called on every tick. If the current bar's minute matches now,
        update high/low/close in place. If the minute has rolled over,
        finalise the previous bar (push to ring), start a new one, and
        recompute ATR from the now-completed bar.
        """
        spot = float(self.ctx.get_spot_price(self.params.underlying) or 0)
        if spot <= 0:
            return
        minute_key = now.replace(second=0, microsecond=0).isoformat()

        if self._current_bar is None or self._current_bar["minute"] != minute_key:
            # Roll: finalise previous bar (if any) and push to ring buffer.
            if self._current_bar is not None:
                self._bars.append(self._current_bar)
                self._update_atr_after_bar()
            # Open a new bar.
            self._current_bar = {
                "minute": minute_key,
                "open": spot,
                "high": spot,
                "low": spot,
                "close": spot,
            }
            return

        # Same minute as current bar — update HLC.
        bar = self._current_bar
        if spot > bar["high"]:
            bar["high"] = spot
        if spot < bar["low"]:
            bar["low"] = spot
        bar["close"] = spot

    def _update_atr_after_bar(self) -> None:
        """Recompute ATR(period) using Wilder's smoothing.

        Called once per minute when a bar finalises. ATR is over the
        most recent ``params.atr_period`` completed bars. Wilder's
        smoothing: TR = max(high-low, |high-prev_close|, |low-prev_close|),
        ATR = ((period-1)*prev_atr + TR) / period.
        """
        period = max(1, int(self.params.atr_period))
        bars = list(self._bars)
        if len(bars) < 2:
            return  # Need at least 2 bars to compute one TR

        last = bars[-1]
        prev = bars[-2]
        tr = max(
            last["high"] - last["low"],
            abs(last["high"] - prev["close"]),
            abs(last["low"] - prev["close"]),
        )
        if self._atr <= 0:
            # Seed: simple average TR over the window once we have N TRs.
            if len(bars) >= period + 1:
                trs = []
                for i in range(1, period + 1):
                    h, low, p_close = (
                        bars[-i]["high"],
                        bars[-i]["low"],
                        bars[-i - 1]["close"],
                    )
                    trs.append(max(h - low, abs(h - p_close), abs(low - p_close)))
                self._atr = sum(trs) / float(period)
            return
        # Wilder recurrence
        self._atr = ((period - 1) * self._atr + tr) / period

    # ─── Entry ──────────────────────────────────────────────────────

    async def _try_entry(self, now: datetime) -> Signal | None:
        # Need a complete buffer to compute Donchian + ATR
        required = _required_bars(self.params.donchian_lookback, self.params.atr_period)
        if len(self._bars) < required:
            return None  # Buffer warming up — no log spam, this is expected

        # Filters that don't require chain access first (cheap)
        vix_block = self._check_vix_filter()
        if vix_block:
            self._log_skip_throttled(
                "ENTRY_SKIP_VIX",
                f"[{self.strategy_id}] Entry skipped: {vix_block}",
            )
            return None

        spike_block = self._check_intraday_vix_spike_filter()
        if spike_block:
            self._log_skip_throttled(
                "ENTRY_SKIP_VIX_SPIKE",
                f"[{self.strategy_id}] Entry skipped: {spike_block}",
            )
            return None

        # ATR floor — too calm = no breakout edge
        if self._atr <= 0:
            return None
        spot = float(self.ctx.get_spot_price(self.params.underlying) or 0)
        if spot <= 0:
            return None
        atr_floor = self.params.atr_floor_pct_of_spot / 100.0 * spot
        if self._atr < atr_floor:
            self._log_skip_throttled(
                "ENTRY_SKIP_ATR",
                f"[{self.strategy_id}] Entry skipped: ATR {self._atr:.1f} < floor "
                f"{atr_floor:.1f} ({self.params.atr_floor_pct_of_spot}% of spot {spot:.0f})",
            )
            return None

        # Donchian breakout check — uses last `lookback` COMPLETED bars
        # (NOT including the in-progress current_bar).
        bars = list(self._bars)
        lookback = max(1, int(self.params.donchian_lookback))
        # The last bar in self._bars is the most recently FINALISED bar;
        # its close is what we use as "today's close" for the breakout.
        latest_close = bars[-1]["close"]
        # Channel is over the `lookback` bars BEFORE the latest one —
        # i.e., bars[-lookback-1:-1]. If we don't have enough history,
        # fall through (the required-bars check above ensures we do).
        prior_window = bars[-lookback - 1:-1] if len(bars) > lookback else bars[:-1]
        if not prior_window:
            return None
        ch_high = max(b["high"] for b in prior_window)
        ch_low = min(b["low"] for b in prior_window)

        # v2 (May 1 2026): chop-window skip — historical NIFTY low-vol
        # window 11:30-13:00 IST produces low-quality breakouts; gate
        # them out unless the operator disabled the window (both ends
        # set to 00:00).
        cw_start = self.params.skip_chop_window_start
        cw_end = self.params.skip_chop_window_end
        if cw_start != cw_end and cw_start <= now.time() < cw_end:
            self._log_skip_throttled(
                "ENTRY_SKIP_CHOP_WINDOW",
                f"[{self.strategy_id}] Entry skipped: chop window "
                f"{cw_start}-{cw_end} IST",
            )
            return None

        # v2: breakout must clear by max(fixed_pts, atr_mult × ATR).
        # The ATR-multiple gate filters marginal breakouts where the
        # signal-to-noise is too weak for a directional bet.
        breakout_threshold = max(
            self.params.breakout_confirmation_pts,
            self.params.breakout_atr_mult * self._atr,
        )

        side: str = ""
        if latest_close > ch_high + breakout_threshold:
            side = "long"
        elif latest_close < ch_low - breakout_threshold:
            side = "short"
        else:
            # No breakout this bar
            return None

        # Pick a deep-ITM strike from the chain
        chain = self.ctx.get_option_chain(self.params.underlying, self._expiry)
        if chain is None or not chain.strikes:
            self._log_skip_throttled(
                "ENTRY_SKIP_NO_CHAIN",
                f"[{self.strategy_id}] Entry skipped: no option chain for "
                f"expiry={self._expiry}",
            )
            return None

        opt, strike, opt_type = self._select_itm_option(chain, spot, side)
        if opt is None:
            self._log_skip_throttled(
                "ENTRY_SKIP_NO_ITM",
                f"[{self.strategy_id}] Entry skipped: no deep-ITM {opt_type} within "
                f"{self.params.itm_max_strike_search_pts}pts of "
                f"spot{('-' if side == 'long' else '+')}{self.params.itm_offset_pts}",
            )
            return None

        # Build the single-leg LIMIT-at-mid BUY signal
        leg = self._build_option_leg(
            opt.tradingsymbol,
            opt.instrument_token,
            OrderSide.BUY,
            self._quantity,
            opt=opt,
        )
        if leg is None:
            self._log_skip_throttled(
                "ENTRY_SKIP_PRICING",
                f"[{self.strategy_id}] Entry skipped: bid/ask missing on selected "
                f"ITM strike {opt.tradingsymbol}",
            )
            return None

        # Snapshot entry state
        self._entered = True
        self._side = side
        self._entry_token = opt.instrument_token
        self._entry_symbol = opt.tradingsymbol
        self._entry_strike = strike
        self._entry_option_type = opt_type
        self._entry_spot = spot
        self._peak_favorable_spot = spot
        # v2: stamp the entry timestamp for the min-hold gate.
        self._entry_datetime = now
        # Use the realistic-fill ask (we BUY, so we cross the ask). The
        # base helper writes mid as the LIMIT price, but we want to
        # track the ASK as the cost-basis for PT/SL because the
        # broker's executor might fill at ask if mid doesn't fill.
        _bid, ask = self._bid_ask_for(self._entry_token)
        self._entry_premium = Decimal(str(round(float(ask), 2)))
        self._trades_today += 1

        logger.info(
            f"[{self.strategy_id}] ENTRY {side.upper()} {opt_type} strike={strike} "
            f"symbol={opt.tradingsymbol} spot={spot:.1f} ATR={self._atr:.1f} "
            f"ch_high={ch_high:.1f} ch_low={ch_low:.1f} entry_premium={self._entry_premium}"
        )
        return entry_signal(
            self.strategy_id, [leg],
            f"Trend {side.upper()} breakout: spot={spot:.1f} "
            f"close={latest_close:.1f} ch=[{ch_low:.1f},{ch_high:.1f}] ATR={self._atr:.1f}",
        )

    def _select_itm_option(
        self,
        chain: "OptionChain",
        spot: float,
        side: str,
    ) -> tuple[object, float, str]:
        """Select a deep-ITM option from the chain.

        Long bias → BUY a CE strike `itm_offset_pts` BELOW spot.
        Short bias → BUY a PE strike `itm_offset_pts` ABOVE spot.

        Returns (OptionData | None, strike, option_type).
        Searches within ``itm_max_strike_search_pts`` of the ideal
        strike; returns (None, ideal_strike, opt_type) if nothing found.
        """
        opt_type = "CE" if side == "long" else "PE"
        if side == "long":
            ideal = spot - self.params.itm_offset_pts
        else:
            ideal = spot + self.params.itm_offset_pts

        best_entry: "OptionChainEntry | None" = None
        best_dist = float("inf")
        for entry in chain.strikes:
            strike = float(entry.strike)
            dist = abs(strike - ideal)
            if dist > self.params.itm_max_strike_search_pts:
                continue
            opt_for_type = entry.ce if opt_type == "CE" else entry.pe
            if opt_for_type is None:
                continue
            # Require non-zero bid AND ask — fall-back to LTP fill is
            # exactly the kind of "realistic-fill is fiction" the audit
            # warned against.
            if (
                float(opt_for_type.bid_price or 0) <= 0
                or float(opt_for_type.ask_price or 0) <= 0
            ):
                continue
            if dist < best_dist:
                best_dist = dist
                best_entry = entry

        if best_entry is None:
            return None, float(ideal), opt_type
        opt = best_entry.ce if opt_type == "CE" else best_entry.pe
        return opt, float(best_entry.strike), opt_type

    # ─── Exit ───────────────────────────────────────────────────────

    def _check_exit_conditions(self, now: datetime) -> Signal | None:
        """Check trailing stop / hard time stop / premium PT-SL / reverse-on-break.

        Order: time-stop → premium PT → premium SL → ATR trail. Time stop
        is non-negotiable; the others are evaluated against realistic-
        fill prices via ``_bid_ask_for``."""

        # 1) Hard time stop — square off
        if now.time() >= self.params.exit_time:
            return self._exit("Exit time reached")

        # 2) Premium PT/SL — uses realistic close cost (we hold a long, so
        #    we SELL at bid on close)
        bid, _ask = self._bid_ask_for(self._entry_token)
        if bid > 0 and self._entry_premium > 0:
            current_close_price = Decimal(str(round(float(bid), 2)))
            change_pct = float(
                (current_close_price - self._entry_premium) / self._entry_premium * 100
            )
            if change_pct >= self.params.profit_target_pct:
                return self._exit(f"Profit target: premium +{change_pct:.1f}%")
            if change_pct <= -self.params.stop_loss_pct:
                return self._exit(f"Stop loss: premium {change_pct:.1f}%")

        # 3) ATR trailing stop on spot
        spot = float(self.ctx.get_spot_price(self.params.underlying) or 0)
        if spot <= 0 or self._atr <= 0:
            return None

        # v2 (May 1 2026): minimum hold period — gate the trail stop for
        # the first ``min_hold_minutes`` after entry. v1 logs showed
        # trades exiting within seconds of entry on small post-breakout
        # giveback; the min-hold lets the position breathe through the
        # initial chop. PT/SL premium gates above remain active even
        # within the hold period (those are intentional disaster caps).
        if (
            self._entry_datetime is not None
            and self.params.min_hold_minutes > 0
            and (now - self._entry_datetime).total_seconds() < self.params.min_hold_minutes * 60
        ):
            # Still update the favorable-spot tracker so the trail uses
            # the real peak/trough, just don't fire the stop yet.
            if self._side == "long" and spot > self._peak_favorable_spot:
                self._peak_favorable_spot = spot
            elif self._side == "short" and spot < self._peak_favorable_spot:
                self._peak_favorable_spot = spot
            return None

        if self._side == "long":
            if spot > self._peak_favorable_spot:
                self._peak_favorable_spot = spot
            stop_level = self._peak_favorable_spot - self.params.atr_stop_mult * self._atr
            if spot < stop_level:
                return self._exit(
                    f"ATR trail (long): spot={spot:.1f} below stop={stop_level:.1f} "
                    f"(peak={self._peak_favorable_spot:.1f}, ATR={self._atr:.1f})"
                )
        else:  # short
            if spot < self._peak_favorable_spot or self._peak_favorable_spot == self._entry_spot:
                # Note: for short, we want the LOWEST spot since entry
                # (most-favorable-for-short = lower price). Initialize
                # _peak_favorable_spot at entry_spot and only lower it.
                if spot < self._peak_favorable_spot:
                    self._peak_favorable_spot = spot
            stop_level = self._peak_favorable_spot + self.params.atr_stop_mult * self._atr
            if spot > stop_level:
                return self._exit(
                    f"ATR trail (short): spot={spot:.1f} above stop={stop_level:.1f} "
                    f"(trough={self._peak_favorable_spot:.1f}, ATR={self._atr:.1f})"
                )

        return None

    def _exit(self, reason: str) -> Signal:
        """Close the open single-leg position, reset entry state, mark
        stopped-for-day."""
        # Build a SELL leg at LIMIT-mid via the base helper
        leg = self._build_option_leg(
            self._entry_symbol,
            self._entry_token,
            OrderSide.SELL,
            self._quantity,
        )
        if leg is None:
            # Quote unavailable — log and emit a MARKET fallback so the
            # position doesn't leak. This mirrors the IC EXIT-fallback
            # warning pattern.
            from src.strategy.signals import make_leg
            from src.core.types import OrderType
            leg = make_leg(
                self._entry_symbol,
                self._entry_token,
                OrderSide.SELL,
                self._quantity,
                order_type=OrderType.MARKET,
            )
            logger.warning(
                f"[{self.strategy_id}] EXIT fallback to MARKET for "
                f"{self._entry_symbol} — bid/ask missing"
            )

        logger.info(f"[{self.strategy_id}] EXIT {self._side.upper()} reason={reason}")

        # Reset state BEFORE returning so a re-entry on the same tick
        # (rare but possible if reverse-on-break is added later)
        # doesn't see stale state.
        self._entered = False
        self._side = ""
        self._entry_token = 0
        self._entry_symbol = ""
        self._entry_strike = 0.0
        self._entry_option_type = ""
        self._entry_premium = Decimal("0")
        self._entry_spot = 0.0
        self._peak_favorable_spot = 0.0
        self._entry_datetime = None  # v2: clear min-hold timer
        self._stopped_for_day = True

        return exit_signal(self.strategy_id, [leg], reason)

    # ─── Scoring (orchestrator-compat; not used standalone) ─────────

    def evaluate_score(self) -> int:
        """Return a 0-100 setup score for orchestrator comparison.

        Standalone validation doesn't use this — the on_tick path is
        gated by the actual breakout condition. This is for the future
        case where TrendITM is one of N candidates the orchestrator
        picks between.
        """
        try:
            if not self._bars or len(self._bars) < _required_bars(
                self.params.donchian_lookback, self.params.atr_period
            ):
                return 0
            if self._check_vix_filter():
                return 0
            if self._atr <= 0:
                return 0
            spot = float(self.ctx.get_spot_price(self.params.underlying) or 0)
            if spot <= 0:
                return 0
            atr_floor = self.params.atr_floor_pct_of_spot / 100.0 * spot
            if self._atr < atr_floor:
                return 0
            # Score is higher when ATR is meaningfully above the floor —
            # more room for the trail stop to give the trade headroom.
            atr_ratio = min(2.0, self._atr / max(atr_floor, 1e-9))
            return min(100, 50 + int(25 * atr_ratio))
        except Exception:
            return 0
