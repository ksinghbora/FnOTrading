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
from src.core.models import Signal, Subscription, Tick
from src.core.structured_logger import get_structured_logger
from src.core.types import OrderSide, Timeframe
from src.options.chain_analyzer import get_high_oi_strikes
from src.strategy.base import BaseStrategy
from src.strategy.indicators import BreakoutSignal, momentum_breakout, oi_breakout_confirm
from src.strategy.params import PortfolioParams
from src.strategy.regime import MarketRegime, RegimeDetector
from src.strategy.registry import register_strategy
from src.advisor.confluence import apply_confluence, load_day_bias
from src.advisor.models import DayBias
from src.strategy.decision_logger import DecisionLogger, DecisionSnapshot
from src.strategy.signals import entry_signal, exit_signal, make_leg

logger = logging.getLogger(__name__)


# ─── Signal Scoring ──────────────────────────────────────────────────

def score_premium_selling(
    vix: float,
    morning_range_pct: float,
    move_from_open_pct: float,
    pcr_oi: float,
    is_expiry_day: bool,
    dte: int,
    ic_mode: bool = False,
) -> tuple[int, list[str]]:
    """Score conditions for premium selling (0-100).

    When ic_mode=True, VIX scoring is more generous because iron condor
    wings cap max loss — higher VIX means richer premiums with defined risk.
    """
    score = 0
    reasons: list[str] = []

    # 1. VIX sweet spot (+25)
    if ic_mode:
        # IC has wings — VIX 14-28 is the productive zone (fatter premiums, capped risk)
        if 14 <= vix <= 20:
            score += 25
            reasons.append(f"VIX={vix:.1f} ideal(IC)")
        elif 20 < vix <= 28:
            score += 20
            reasons.append(f"VIX={vix:.1f} rich premiums(IC)")
        elif 11 <= vix < 14:
            score += 8
            reasons.append(f"VIX={vix:.1f} thin premiums(IC)")
        elif vix > 28:
            score += 5
            reasons.append(f"VIX={vix:.1f} elevated(IC-protected)")
    else:
        # Naked selling — conservative VIX scoring
        if 12 <= vix <= 16:
            score += 25
            reasons.append(f"VIX={vix:.1f} ideal")
        elif 11 <= vix <= 20:
            score += 12
            reasons.append(f"VIX={vix:.1f} acceptable")
        elif 20 < vix <= 25:
            score += 8
            reasons.append(f"VIX={vix:.1f} high(risky naked)")
        elif vix > 25:
            reasons.append(f"VIX={vix:.1f} too high")

    # 2. Tight morning range (+25)
    if morning_range_pct <= 0.3:
        score += 25
        reasons.append(f"range={morning_range_pct:.2f}% very tight")
    elif morning_range_pct <= 0.5:
        score += 12
        reasons.append(f"range={morning_range_pct:.2f}% moderate")
    elif morning_range_pct <= 0.8:
        score += 5
        reasons.append(f"range={morning_range_pct:.2f}% wide")

    # 3. No trend (+25)
    if move_from_open_pct <= 0.15:
        score += 25
        reasons.append(f"move={move_from_open_pct:.2f}% flat")
    elif move_from_open_pct <= 0.3:
        score += 12
        reasons.append(f"move={move_from_open_pct:.2f}% mild")
    elif move_from_open_pct <= 0.5:
        score += 5
        reasons.append(f"move={move_from_open_pct:.2f}% drifting")

    # 4. PCR healthy (+15) / extreme (-10)
    if 0.8 <= pcr_oi <= 1.2:
        score += 15
        reasons.append(f"PCR={pcr_oi:.2f} neutral")
    elif 0.6 <= pcr_oi <= 1.5:
        score += 5
        reasons.append(f"PCR={pcr_oi:.2f} acceptable")
    elif pcr_oi > 0:
        score -= 10
        reasons.append(f"PCR={pcr_oi:.2f} extreme")

    # 5. DTE (+10)
    if not is_expiry_day and dte >= 3:
        score += 10
        reasons.append(f"DTE={dte} safe")
    elif not is_expiry_day and dte >= 1:
        score += 5
        reasons.append(f"DTE={dte}")

    # 6. Expiry day penalty (-10)
    if is_expiry_day:
        score -= 10
        reasons.append("expiry_day_penalty=-10")

    return score, reasons


def score_trend_following(
    breakout: BreakoutSignal,
    oi_confirmed: bool,
    trend_duration_minutes: int,
    vix: float,
    vix_prev: float = 0.0,
    banknifty_confirming: bool | None = None,
) -> tuple[int, list[str]]:
    """Score conditions for trend following (0-100).

    New factors vs original:
    - vix_prev: VIX from ~20 min ago. Rising VIX on UP breakout = counter-signal (-15).
      Falling VIX on UP breakout = environment supports move (+10). Inverted for DOWN.
    - banknifty_confirming: True if BankNifty is also above (UP) or below (DOWN) its
      morning high/low. None = unknown (no penalty). False = divergence (-15).
    """
    score = 0
    reasons: list[str] = []

    if not breakout.direction:
        return 0, ["no breakout"]

    # 1. Breakout strength (+30)
    if breakout.strength >= 0.5:
        score += 30
        reasons.append(f"breakout={breakout.strength:.2f}% strong")
    elif breakout.strength >= 0.3:
        score += 15
        reasons.append(f"breakout={breakout.strength:.2f}% moderate")

    # 2. OI confirmation (+25)
    if oi_confirmed:
        score += 25
        reasons.append("OI confirmed")

    # 3. Trend sustained (+25)
    if trend_duration_minutes >= 30:
        score += 25
        reasons.append(f"sustained={trend_duration_minutes}min")
    elif trend_duration_minutes >= 15:
        score += 12
        reasons.append(f"sustained={trend_duration_minutes}min early")

    # 4. VIX level adequate (+20)
    if vix >= 14:
        score += 20
        reasons.append(f"VIX={vix:.1f} supports trend")
    elif vix >= 11:
        score += 8
        reasons.append(f"VIX={vix:.1f} low for trend")

    # 5. VIX direction (+10 / -15)
    # Rising VIX + UP breakout = contradiction (fear rising while buying = fake move).
    # Rising VIX + DOWN breakout = confirmation (fear + breakdown = real selling).
    if vix_prev > 0:
        vix_rising = vix > vix_prev * 1.01  # >1% rise in VIX = meaningful
        if breakout.direction == "UP":
            if vix_rising:
                score -= 15
                reasons.append(f"VIX_rising={vix:.1f}>{vix_prev:.1f} contradicts UP")
            else:
                score += 10
                reasons.append(f"VIX_stable/falling supports UP")
        else:  # DOWN
            if vix_rising:
                score += 10
                reasons.append(f"VIX_rising={vix:.1f} confirms DOWN")
            else:
                score -= 15
                reasons.append(f"VIX_falling contradicts DOWN(bounce risk)")

    # 6. BankNifty sector confirmation (+10 / -15)
    # BankNifty is ~33% of NIFTY. Divergence = sector-specific move, not index-wide.
    if banknifty_confirming is True:
        score += 10
        reasons.append("BankNifty confirming")
    elif banknifty_confirming is False:
        score -= 15
        reasons.append("BankNifty diverging(sector-only move)")

    return score, reasons


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
        self._trend_buy_token: int = 0
        self._trend_buy_symbol: str = ""
        self._trend_sell_token: int = 0
        self._trend_sell_symbol: str = ""
        self._trend_direction: str = ""
        self._entry_debit: Decimal = Decimal("0")
        self._max_spread_value: Decimal = Decimal("0")
        self._peak_spread_value: Decimal = Decimal("0")

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

        # ─── Day-level leg P&L tracking ─────────────────────────
        self._prem_realized_pnl: float = 0.0
        self._trend_realized_pnl: float = 0.0
        self._prem_trades_today: int = 0
        self._trend_trades_today: int = 0
        self._last_monitor_minute: int = -1  # Combined unrealized P&L

        # ─── VIX direction tracking (for trend scoring) ───────
        # Stores VIX readings every 5 min to detect rising/falling VIX at entry.
        self._vix_history: list[tuple[datetime, float]] = []  # (timestamp, vix)

        # ─── BankNifty morning range (for trend confirmation) ─────
        self._bn_morning_high: float = 0.0
        self._bn_morning_low: float = 0.0

        # ─── Decision snapshot logger (ML data collection) ────
        self._decision_logger = DecisionLogger()
        self._last_skip_minute: int = -1  # throttle SKIP logs to 1/5min

        # ─── Paper trading: shadow blocking (log but don't block) ──
        import os
        from scripts.auto_auth import load_env
        load_env()
        self._paper_mode = os.environ.get("PAPER_TRADING", "false").lower() == "true"

    def get_subscriptions(self) -> Subscription:
        return Subscription(instrument_tokens=[], timeframes=[])

    async def on_start(self) -> None:
        self._expiry = self.ctx.next_expiry(self.params.underlying)
        self._regime_detector = RegimeDetector(
            self.ctx._feed, self.ctx._aggregator, self.ctx._chain_builder
        )
        # Load AI advisor day bias (if available)
        self._load_day_bias()
        logger.info(
            f"[{self.strategy_id}] Portfolio strategy started: "
            f"underlying={self.params.underlying} expiry={self._expiry} "
            f"premium_threshold={self.params.signal_threshold} "
            f"trend_threshold={self.params.signal_threshold} "
            f"advisor={'active' if self._confluence_enabled else 'shadow'} "
            f"day_bias={'loaded' if self._day_bias else 'none'}"
        )

    async def on_tick(self, tick: Tick) -> Signal | None:
        now = self.ctx.clock.now()

        # Expiry rollover
        new_expiry = self._check_expiry_rollover(self._expiry, self.params.underlying)
        if new_expiry:
            self._expiry = new_expiry

        # Record VIX every 5 minutes for direction detection at trend entry
        if now.minute % 5 == 0 and now.second < 10:
            vix_now = self.ctx.get_vix()
            if vix_now > 0:
                self._vix_history.append((now, vix_now))
                # Keep only last 60 minutes of history (12 readings)
                if len(self._vix_history) > 12:
                    self._vix_history.pop(0)

        # Build BankNifty morning range (9:15-9:30) from M5 candles once per day.
        # Only possible if BANKNIFTY spot is being subscribed and aggregated.
        if not self._bn_morning_high:
            bn_token = self._find_underlying_token("BANKNIFTY")
            if bn_token:
                bn_candles = self.ctx.get_candles(bn_token, Timeframe.M5, limit=10)
                if len(bn_candles) >= 3:
                    morning = bn_candles[:3]
                    self._bn_morning_high = max(float(c.high) for c in morning)
                    self._bn_morning_low = min(float(c.low) for c in morning)

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
            # Live mode: only 10:00-13:00
            trend_cutoff = time(14, 0) if self._paper_mode else time(13, 0)
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

        regime = self._regime_detector.assess(self.params.underlying)

        if regime.regime == MarketRegime.EXTREME_VOL:
            if not self._paper_mode:
                self._prem_stopped = True
                logger.info(f"[{self.strategy_id}] PREMIUM SIT OUT: {regime.reason}")
                return None
            else:
                logger.info(f"[{self.strategy_id}] [SHADOW_BLOCK] PREMIUM would sit out: {regime.reason}")

        if regime.regime == MarketRegime.CONFLICTED:
            if not self._paper_mode:
                logger.info(f"[{self.strategy_id}] PREMIUM REDUCED: regime conflict — {regime.reason}")
                self._prem_quantity = max(self._lot_size, self._base_quantity // 4)
            else:
                logger.info(f"[{self.strategy_id}] [SHADOW_BLOCK] REGIME CONFLICT — would reduce to 25%: {regime.reason}")

        if regime.regime == MarketRegime.CHOPPY:
            if not self._paper_mode:
                logger.info(f"[{self.strategy_id}] PREMIUM REDUCED: {regime.reason}")
                self._prem_quantity = max(self._lot_size, self._base_quantity // 2)
            else:
                logger.info(f"[{self.strategy_id}] [SHADOW_BLOCK] CHOP detected — would reduce size: chop_score={regime.chop_score:.2f}")

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
                logger.info(f"[{self.strategy_id}] WEEKLY CYCLE: Mon PM DTE=1 — high gamma risk")
            else:
                logger.info(f"[{self.strategy_id}] [SHADOW_BLOCK] WEEKLY CYCLE: would penalize -15 (Mon PM gamma)")

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

        # Apply AI confluence adjustment
        rule_score = self._prem_score
        self._prem_score, _ = apply_confluence(
            self._prem_score, self._day_bias, "premium",
            enabled=self._confluence_enabled, weight=self._confluence_weight,
        )

        is_phase1 = now.time() < time(10, 0)
        threshold = self.params.phase1_threshold if is_phase1 else 65

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
        """Enter IC by default, strangle only when very calm."""
        if vix < self.params.strangle_vix_max:
            return self._enter_strangle(vix)
        result = self._enter_iron_condor(vix)
        if result:
            return result
        return self._enter_strangle(vix)

    # ─── Trend Leg Evaluation ─────────────────────────────────

    def _evaluate_trend(self, now) -> Signal | None:
        """Score and enter trend following on confirmed breakout."""
        # Hard block: expiry day AND ≤2 DTE — theta math is too punishing for debit
        # spread buyers. At 0-2 DTE, the long leg loses 25-60% of value per day.
        # This is a hard block even in paper mode — it produces meaningless results.
        if self._expiry:
            dte = (self._expiry - now.date()).days
            if dte <= 2:
                if now.minute == 0:  # Log once per hour, not every tick
                    logger.info(
                        f"[{self.strategy_id}] TREND hard-blocked: DTE={dte} ≤ 2 "
                        f"(theta too punishing for debit spread buyers)"
                    )
                return None

        spot = float(self.ctx.get_spot_price(self.params.underlying))
        vix = self.ctx.get_vix()
        if spot <= 0 or vix <= 0:
            return None

        breakout, oi_confirmed, trend_duration = self._assess_trend(spot)

        # VIX from ~20 minutes ago (4 readings back at 5-min cadence)
        vix_prev = self._vix_history[-4][1] if len(self._vix_history) >= 4 else 0.0

        # BankNifty sector confirmation
        try:
            bn_price = float(self.ctx.get_spot_price("BANKNIFTY"))
        except Exception:
            bn_price = 0.0
        if bn_price > 0 and self._bn_morning_high > 0:
            if breakout.direction == "UP":
                banknifty_confirming = bn_price > self._bn_morning_high
            else:
                banknifty_confirming = bn_price < self._bn_morning_low
        else:
            banknifty_confirming = None  # Unknown — no penalty

        self._trend_score, reasons = score_trend_following(
            breakout=breakout,
            oi_confirmed=oi_confirmed,
            trend_duration_minutes=trend_duration,
            vix=vix,
            vix_prev=vix_prev,
            banknifty_confirming=banknifty_confirming,
        )

        # Apply AI confluence adjustment
        rule_score = self._trend_score
        self._trend_score, _ = apply_confluence(
            self._trend_score, self._day_bias, "trend",
            enabled=self._confluence_enabled, weight=self._confluence_weight,
        )

        if now.minute % 10 == 0:
            ai_tag = f" ai_adj={self._trend_score - rule_score:+d}" if self._day_bias else ""
            logger.info(
                f"[{self.strategy_id}] TREND: "
                f"score={self._trend_score}/100 [{', '.join(reasons)}]{ai_tag}"
            )

        ai_adj = self._trend_score - rule_score
        trend_threshold = self.params.signal_threshold

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
            f"[ENTRY_QUALITY] strategy={self.strategy_id} leg=PREMIUM mode=STRANGLE "
            f"net_delta={g.get('delta', 0):+.1f} net_gamma={g.get('gamma', 0):+.4f} "
            f"net_theta={g.get('theta', 0):+.2f} net_vega={g.get('vega', 0):+.2f} "
            f"spot={self._prem_entry_spot:.0f} VIX={vix:.1f} "
            f"premium_per_lot={float(self._entry_premium):.2f}"
        )

        get_structured_logger().log(
            "ENTRY", strategy_id=self.strategy_id, leg="PREMIUM", mode="STRANGLE",
            ce_strike=float(best_ce.strike), pe_strike=float(best_pe.strike),
            premium=float(self._entry_premium), qty=qty, score=self._prem_score,
            vix=vix, spot=self._prem_entry_spot,
            delta=g.get("delta", 0), gamma=g.get("gamma", 0),
            theta=g.get("theta", 0), vega=g.get("vega", 0),
        )

        return entry_signal(self.strategy_id, [
            make_leg(self._short_ce_symbol, self._short_ce_token, OrderSide.SELL, qty),
            make_leg(self._short_pe_symbol, self._short_pe_token, OrderSide.SELL, qty),
        ], f"Portfolio premium strangle: score={self._prem_score}")

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

        wing_offset = self.params.ic_wing_width_strikes * step
        long_ce_strike = float(best_ce.strike) + wing_offset
        long_pe_strike = float(best_pe.strike) - wing_offset

        long_ce_entry = long_pe_entry = None
        for entry in chain.strikes:
            s = float(entry.strike)
            if s == long_ce_strike and entry.ce:
                long_ce_entry = entry
            if s == long_pe_strike and entry.pe:
                long_pe_entry = entry

        if not long_ce_entry or not long_ce_entry.ce or not long_pe_entry or not long_pe_entry.pe:
            logger.warning(
                f"[{self.strategy_id}] IC BLOCKED: wing strikes missing "
                f"(long_ce@{long_ce_strike}={long_ce_entry is not None} long_pe@{long_pe_strike}={long_pe_entry is not None})"
            )
            return None  # Don't fallback — caller handles it

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

        # Minimum credit check
        if self._entry_premium < Decimal("10"):
            logger.warning(f"[{self.strategy_id}] IC BLOCKED: credit too low ({self._entry_premium})")
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
            f"[ENTRY_QUALITY] strategy={self.strategy_id} leg=PREMIUM mode=IRON_CONDOR "
            f"net_delta={g.get('delta', 0):+.1f} net_gamma={g.get('gamma', 0):+.4f} "
            f"net_theta={g.get('theta', 0):+.2f} net_vega={g.get('vega', 0):+.2f} "
            f"spot={self._prem_entry_spot:.0f} VIX={vix:.1f} "
            f"credit_per_lot={float(self._entry_premium):.2f} wing_width={wing_offset}pts"
        )

        get_structured_logger().log(
            "ENTRY", strategy_id=self.strategy_id, leg="PREMIUM", mode="IRON_CONDOR",
            ce_strike=float(best_ce.strike), pe_strike=float(best_pe.strike),
            premium=float(self._entry_premium), qty=qty, score=self._prem_score,
            vix=vix, spot=self._prem_entry_spot, wing_offset=wing_offset,
            delta=g.get("delta", 0), gamma=g.get("gamma", 0),
            theta=g.get("theta", 0), vega=g.get("vega", 0),
        )

        return entry_signal(self.strategy_id, [
            make_leg(self._short_ce_symbol, self._short_ce_token, OrderSide.SELL, qty),
            make_leg(self._short_pe_symbol, self._short_pe_token, OrderSide.SELL, qty),
            make_leg(self._long_ce_symbol, self._long_ce_token, OrderSide.BUY, qty),
            make_leg(self._long_pe_symbol, self._long_pe_token, OrderSide.BUY, qty),
        ], f"Portfolio premium IC: score={self._prem_score}")

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

        # Buy ATM (50Δ) not ATM+1 — captures the first point of movement immediately.
        # Short leg 3 strikes OTM (15-20Δ) for cost reduction. Net delta ~0.30-0.35.
        if breakout.direction == "UP":
            buy_strike = atm
            sell_strike = buy_strike + width
            opt_attr = "ce"
        else:
            buy_strike = atm
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

        buy_ltp = self.ctx.get_ltp(self._trend_buy_token)
        sell_ltp = self.ctx.get_ltp(self._trend_sell_token)
        self._entry_debit = buy_ltp - sell_ltp
        self._max_spread_value = Decimal(str(abs(width)))
        self._peak_spread_value = self._entry_debit

        if self._entry_debit <= 0:
            logger.warning(f"[{self.strategy_id}] TREND BLOCKED: debit non-positive ({self._entry_debit})")
            return None

        self._trend_entered = True
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
            f"[ENTRY_QUALITY] strategy={self.strategy_id} leg=TREND dir={breakout.direction} "
            f"net_delta={g.get('delta', 0):+.1f} net_gamma={g.get('gamma', 0):+.4f} "
            f"net_theta={g.get('theta', 0):+.2f} net_vega={g.get('vega', 0):+.2f} "
            f"spot={spot:.0f} VIX={self._trend_entry_vix:.1f} "
            f"debit={float(self._entry_debit):.2f} max_profit={float(self._max_spread_value - self._entry_debit):.2f}"
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

        return entry_signal(self.strategy_id, [
            make_leg(self._trend_buy_symbol, self._trend_buy_token, OrderSide.BUY, self._trend_quantity),
            make_leg(self._trend_sell_symbol, self._trend_sell_token, OrderSide.SELL, self._trend_quantity),
        ], f"Portfolio trend {breakout.direction}: score={self._trend_score}")

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
                logger.info(f"[{self.strategy_id}] [SHADOW_BLOCK] Would exit: weekly time stop Mon 2:30 PM")

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

        # Profit target
        pt_pct = (
            self.params.ic_profit_target_pct if self._prem_mode == "iron_condor"
            else self.params.premium_profit_target_pct
        )
        if change_pct < 0 and abs(change_pct) >= pt_pct:
            return self._exit_premium(f"Profit target: premium decayed {abs(change_pct):.1f}%")

        # Stop loss (gamma-tightened, VIX-scaled for IC)
        if self._prem_mode == "iron_condor":
            base_sl = self.params.ic_stop_loss_pct
            # IC in high VIX: premiums are fatter so noise is larger — widen stop
            vix_now = self.ctx.get_vix()
            if vix_now > 20:
                base_sl = min(base_sl * 1.5, 80.0)  # 40% → 60%, capped at 80%
            sl_pct = base_sl * sl_multiplier
        else:
            sl_pct = self.params.premium_stop_loss_pct * sl_multiplier
        if change_pct > sl_pct:
            tag = " (gamma-tightened)" if gamma_tightened else ""
            return self._exit_premium(f"Stop loss: premium up {change_pct:.1f}%{tag}")

        # Trailing stop — lock in gains after premium decays meaningfully
        trail_pct_base = self.params.premium_trail_stop_pct
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

            if decay_pct >= min_decay_to_trail and self._peak_premium > 0:
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

        legs = [
            make_leg(self._short_ce_symbol, self._short_ce_token, OrderSide.BUY, self._prem_quantity),
            make_leg(self._short_pe_symbol, self._short_pe_token, OrderSide.BUY, self._prem_quantity),
        ]
        if self._prem_mode == "iron_condor" and self._long_ce_token:
            legs.append(make_leg(self._long_ce_symbol, self._long_ce_token, OrderSide.SELL, self._prem_quantity))
            legs.append(make_leg(self._long_pe_symbol, self._long_pe_token, OrderSide.SELL, self._prem_quantity))

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
        self._decision_logger.log(self._build_snapshot(
            "PREMIUM", "EXIT", mode=self._prem_mode,
            exit_reason=reason, outcome_pnl=round(pnl, 2), held_minutes=held_mins,
        ))

        self._prem_entered = False
        self._prem_stopped = True  # No re-entry same day — data shows re-entries are net negative
        return exit_signal(self.strategy_id, legs, reason)

    # ─── Trend Exit ───────────────────────────────────────────

    def _check_trend_exit(self) -> Signal | None:
        """Monitor trend position (debit spread)."""
        if self._entry_debit <= 0:
            return None

        buy_ltp = self.ctx.get_ltp(self._trend_buy_token)
        sell_ltp = self.ctx.get_ltp(self._trend_sell_token)
        current_value = buy_ltp - sell_ltp

        if current_value > self._peak_spread_value:
            self._peak_spread_value = current_value

        # Profit target
        if self._max_spread_value > 0:
            value_pct = float(current_value / self._max_spread_value * 100)
            if value_pct >= self.params.trend_profit_target_pct:
                return self._exit_trend(f"Trend profit: spread at {value_pct:.1f}% of max")

        # Stop loss
        if self._entry_debit > 0:
            loss_pct = float((self._entry_debit - current_value) / self._entry_debit * 100)
            if loss_pct >= self.params.trend_stop_loss_pct:
                return self._exit_trend(f"Trend stop: lost {loss_pct:.1f}%")

        # Trailing stop — activates once spread reaches 30% of max profit
        # This prevents giving back large unrealized gains (e.g. Monday's +517 → -1527)
        if self.params.trend_trailing_stop_pct > 0 and self._max_spread_value > 0:
            max_profit = self._max_spread_value - self._entry_debit
            if max_profit > 0:
                current_profit = current_value - self._entry_debit
                if float(current_profit / max_profit * 100) >= 30:
                    if self._peak_spread_value > 0:
                        pullback = float(
                            (self._peak_spread_value - current_value) / self._peak_spread_value * 100
                        )
                        if pullback >= self.params.trend_trailing_stop_pct:
                            return self._exit_trend(f"Trend trail: pullback {pullback:.1f}%")

        return None

    def _exit_trend(self, reason: str) -> Signal:
        """Close trend leg with P&L attribution."""
        buy_ltp = float(self.ctx.get_ltp(self._trend_buy_token))
        sell_ltp = float(self.ctx.get_ltp(self._trend_sell_token))
        exit_value = buy_ltp - sell_ltp
        pnl = (exit_value - float(self._entry_debit)) * self._trend_quantity
        self._trend_realized_pnl += pnl

        legs = [
            make_leg(self._trend_buy_symbol, self._trend_buy_token, OrderSide.SELL, self._trend_quantity),
            make_leg(self._trend_sell_symbol, self._trend_sell_token, OrderSide.BUY, self._trend_quantity),
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
        self._decision_logger.log(self._build_snapshot(
            "TREND", "EXIT", mode="debit_spread",
            exit_reason=reason, outcome_pnl=round(pnl, 2), held_minutes=held_mins,
        ))

        self._trend_entered = False
        self._trend_stopped = True
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
                f"[MONITOR] strategy={self.strategy_id} leg=PREMIUM mode={self._prem_mode.upper()} "
                f"held={held_min:.0f}min pnl={unrealized:+,.0f} chg={change_pct:+.1f}% "
                f"delta={g['delta']:+.1f} gamma={g['gamma']:+.4f} "
                f"theta={g['theta']:+.2f} vega={g['vega']:+.2f} "
                f"gamma_exp={gamma_exp:.0f}/{self.params.gamma_exit_threshold:.0f} "
                f"theta_eff={theta_gamma:.1f} spot={spot:.0f} VIX={vix:.1f}"
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
                f"[MONITOR] strategy={self.strategy_id} leg=TREND dir={self._trend_direction} "
                f"held={held_min:.0f}min pnl={unrealized:+,.0f} "
                f"spread={current_value:.2f}/{float(self._max_spread_value):.0f} "
                f"delta={g['delta']:+.1f} gamma={g['gamma']:+.4f} "
                f"theta={g['theta']:+.2f} vega={g['vega']:+.2f} "
                f"spot={spot:.0f} VIX={vix:.1f}"
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
            f"[ATTRIBUTION] strategy={self.strategy_id} leg={leg} mode={mode} "
            f"pnl={actual_pnl:+,.0f} spot_chg={ds:+.0f} vix_chg={d_vix:+.1f} held={dt_days * 24:.1f}hrs "
            f"delta={delta_pnl:+,.0f} gamma={gamma_pnl:+,.0f} "
            f"theta={theta_pnl:+,.0f} vega={vega_pnl:+,.0f} "
            f"residual={residual:+,.0f} dominant={dominant}({dominant_pct:.0f}%)"
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

        self._day_bias = load_day_bias()
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
        """Find CE and PE strikes closest to target deltas."""
        best_ce = best_pe = None
        best_ce_diff = best_pe_diff = float("inf")

        for entry in chain.strikes:
            if entry.ce and entry.ce.greeks.delta > 0:
                diff = abs(entry.ce.greeks.delta - target_ce_delta)
                if diff < best_ce_diff:
                    best_ce_diff = diff
                    best_ce = entry
            if entry.pe and entry.pe.greeks.delta < 0:
                diff = abs(entry.pe.greeks.delta - target_pe_delta)
                if diff < best_pe_diff:
                    best_pe_diff = diff
                    best_pe = entry

        return best_ce, best_pe

    def _find_oi_validated_strikes(self, chain, target_ce_delta, target_pe_delta):
        """Find strikes using OI walls + delta validation + VIX range.

        Strategy (from Anant Ladha's methodology):
        1. Find highest Call OI strike (resistance) → sell CE at or above
        2. Find highest Put OI strike (support) → sell PE at or below
        3. Validate with VIX range formula (sell outside 1 SD range)
        4. Cross-check delta is in 0.10-0.30 range (safety)
        5. Fall back to pure delta if OI data is insufficient

        Returns (ce_entry, pe_entry, selection_method)
        """
        import math

        spot = float(chain.spot_price)
        vix = self.ctx.get_vix()

        # Step 1: Find OI walls
        max_ce_oi = 0
        max_ce_oi_entry = None
        max_pe_oi = 0
        max_pe_oi_entry = None

        for entry in chain.strikes:
            if entry.ce and entry.ce.oi > max_ce_oi and float(entry.strike) > spot:
                max_ce_oi = entry.ce.oi
                max_ce_oi_entry = entry
            if entry.pe and entry.pe.oi > max_pe_oi and float(entry.strike) < spot:
                max_pe_oi = entry.pe.oi
                max_pe_oi_entry = entry

        # Step 2: VIX-based expected range (1 SD)
        dte = (self._expiry - self.ctx.clock.now().date()).days if self._expiry else 7
        dte = max(1, dte)
        if vix > 0 and spot > 0:
            expected_range = spot * vix / 100 * math.sqrt(dte / 365)
            vix_ce_boundary = spot + expected_range
            vix_pe_boundary = spot - expected_range
        else:
            vix_ce_boundary = 0
            vix_pe_boundary = 0

        # Step 3: Select CE strike — prefer OI wall if valid
        ce_entry = None
        pe_entry = None
        method = "delta"

        if max_ce_oi_entry and max_ce_oi > 0:
            ce_delta = max_ce_oi_entry.ce.greeks.delta if max_ce_oi_entry.ce else 0
            oi_strike = float(max_ce_oi_entry.strike)

            # OI wall must be:
            # - Above spot (OTM for CE)
            # - Delta in acceptable range (0.05-0.30)
            # - At or beyond VIX range boundary
            if 0.05 <= ce_delta <= 0.30:
                if vix_ce_boundary <= 0 or oi_strike >= vix_ce_boundary * 0.95:
                    ce_entry = max_ce_oi_entry
                    method = "oi_wall"

        if max_pe_oi_entry and max_pe_oi > 0:
            pe_delta = max_pe_oi_entry.pe.greeks.delta if max_pe_oi_entry.pe else 0
            oi_strike = float(max_pe_oi_entry.strike)

            if -0.30 <= pe_delta <= -0.05:
                if vix_pe_boundary <= 0 or oi_strike <= vix_pe_boundary * 1.05:
                    pe_entry = max_pe_oi_entry
                    if method == "oi_wall":
                        method = "oi_wall"
                    else:
                        method = "oi_wall+delta"

        # Step 4: Fall back to delta for any missing side
        if not ce_entry or not pe_entry:
            delta_ce, delta_pe = self._find_delta_strikes(chain, target_ce_delta, target_pe_delta)
            if not ce_entry:
                ce_entry = delta_ce
            if not pe_entry:
                pe_entry = delta_pe
            if method == "delta":
                method = "delta"
            else:
                method = method + "+delta_fallback"

        # Step 5: Minimum premium check (0.5% of spot)
        if ce_entry and pe_entry and ce_entry.ce and pe_entry.pe:
            total_prem = float(ce_entry.ce.ltp + pe_entry.pe.ltp)
            min_prem = spot * 0.005  # 0.5% of spot
            if total_prem < min_prem:
                logger.info(
                    f"[{self.strategy_id}] OI strikes premium {total_prem:.1f} < min {min_prem:.0f} "
                    f"(0.5% of spot) — falling back to delta"
                )
                ce_entry, pe_entry = self._find_delta_strikes(chain, target_ce_delta, target_pe_delta)
                method = "delta(min_prem)"

        # Log selection details
        ce_strike = float(ce_entry.strike) if ce_entry else 0
        pe_strike = float(pe_entry.strike) if pe_entry else 0
        ce_oi = max_ce_oi_entry.ce.oi if max_ce_oi_entry and max_ce_oi_entry.ce else 0
        pe_oi = max_pe_oi_entry.pe.oi if max_pe_oi_entry and max_pe_oi_entry.pe else 0
        logger.info(
            f"[{self.strategy_id}] STRIKE SELECTION: method={method} "
            f"CE@{ce_strike:.0f} PE@{pe_strike:.0f} "
            f"OI_wall_CE@{float(max_ce_oi_entry.strike) if max_ce_oi_entry else 0:.0f}(oi={ce_oi:,}) "
            f"OI_wall_PE@{float(max_pe_oi_entry.strike) if max_pe_oi_entry else 0:.0f}(oi={pe_oi:,}) "
            f"VIX_range=[{vix_pe_boundary:.0f}-{vix_ce_boundary:.0f}] "
            f"spot={spot:.0f}"
        )

        return ce_entry, pe_entry

    def _find_spot_token(self) -> int | None:
        """Find spot instrument token for the underlying."""
        for token, name in self.ctx._chain_builder._spot_tokens.items():
            if name == self.params.underlying:
                return token
        return None

    def _find_underlying_token(self, underlying: str) -> int | None:
        """Find spot instrument token for any underlying name (e.g. BANKNIFTY)."""
        for token, name in self.ctx._chain_builder._spot_tokens.items():
            if name == underlying:
                return token
        return None

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
                f"[DAY_SUMMARY] strategy={self.strategy_id} "
                f"total_pnl={total:+,.0f} "
                f"premium={self._prem_realized_pnl:+,.0f}({prem_pct:+.0f}%) trades={self._prem_trades_today} "
                f"trend={self._trend_realized_pnl:+,.0f}({trend_pct:+.0f}%) trades={self._trend_trades_today}"
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
        self._trend_entered = False
        self._trend_stopped = False
        self._trend_score = 0
        self._peak_spread_value = Decimal("0")
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
