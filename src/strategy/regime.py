"""Market regime detector — 2D model with independent scorers and consensus.

Two independent dimensions:
  1. Volatility (from VIX): LOW | NORMAL | HIGH | EXTREME
  2. Price Action (from candles): RANGE_BOUND | CHOPPY | TRENDING

Three independent action scorers run in parallel. If two score high
simultaneously, that's a CONFLICT — regime is transitioning, reduce risk.

Strategy recommendation is a 2D lookup: Vol × Action × Confidence.

May 2 2026 (post-honest-IC-analysis): also exposes proven range-detection
methods (ADX, Bollinger Band squeeze, realized-vs-implied vol ratio)
calibrated for Indian markets (NIFTY/BANKNIFTY 5-min spot bars). These
power a unified ``is_premium_selling_favorable()`` gate that strategies
can use as a HARD filter — only trade when proven indicators agree on
range-bound + overpriced-IV regime.
"""

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum

from src.core.clock import MarketClock, now_ist
from src.core.constants import INDIA_VIX_TOKEN, VIX_EXTREME, VIX_HIGH, VIX_LOW, VIX_NORMAL
from src.core.types import Timeframe
from src.market_data.aggregator import OHLCAggregator
from src.market_data.feed import TickFeedManager
from src.market_data.option_chain import OptionChainBuilder

logger = logging.getLogger(__name__)


# ─── Indian-market-calibrated range-detection thresholds ────────
# May 2 2026: these are deliberately set to Indian NIFTY/BANKNIFTY
# practitioner values, not US-textbook defaults.

# ADX(14) on 5-min spot bars. Wilder 1978 standard period.
# US default: <20 = no trend. Indian NIFTY 5-min has more "fake-trend"
# microstructure noise, so threshold lifted to 22. ADX > 28 = strong
# trend (avoid premium-selling). 22-28 = indeterminate.
ADX_RANGE_THRESHOLD = 22.0
ADX_TREND_THRESHOLD = 28.0
ADX_PERIOD = 14

# Bollinger Band squeeze on 5-min closes, 20-period, 2-std band.
# BB width = (upper - lower) / middle * 100, expressed in %.
# Squeeze = current width in bottom 25th percentile of last LOOKBACK
# readings. NIFTY 5-min BB widths typically span 0.05-0.5% in normal
# trading. Lookback 50 bars = ~4 hours of session context.
BB_PERIOD = 20
BB_STD_MULT = 2.0
BB_SQUEEZE_LOOKBACK = 50
BB_SQUEEZE_PERCENTILE = 25.0  # bottom-quartile width = consolidation

# Realized vs implied vol ratio. India VIX has structural premium of
# 15-30% over realized (similar to US but slightly larger). When 20-day
# realized vol is < 80% of India VIX, IV is genuinely "rich" and
# premium-selling has positive expectancy. When ratio > 1.0, IV is
# under-priced — DON'T sell premium.
RV_IV_FAVORABLE_THRESHOLD = 0.80
RV_PERIOD_DAYS = 20
RV_ANNUALIZATION_DAYS = 252  # NSE trading days per year


# ─── v2 detectors (Apr 30 2026): Choppiness Index + VRP ──────────
# After May 5 burn-the-holdout revealed strong-signal IC was curve-fit,
# and the principled AND-gate (ADX<22 AND BB<25%ile AND RV/IV<0.80)
# fired on 0/2590 valid samples (the three conditions are negatively
# correlated on Indian post-SEBI data — well-priced IV markets), we
# replace with two ORTHOGONAL literature-grounded indicators:
#
# 1. Choppiness Index (Bill Dreiss, ASX) — single-indicator range/trend
#    classifier from technical-analysis canon. Uses Fibonacci thresholds
#    61.8 (range) and 38.2 (trend). PURE price-action measure, no IV
#    component, so independent of the second indicator.
#
# 2. VRP (Variance Risk Premium) — Bollerslev-Tauchen-Zhou (2009 RFS),
#    classic academic premium-selling alpha source. VRP = IV − RV.
#    Positive VRP means IV ≥ RV (premium overpriced relative to recent
#    delivered vol). Threshold 0 is the textbook break-even; selling
#    premium is profitable in expectation when VRP > 0.
#
# Conjunction: CI ≥ 61.8 AND VRP > 0 means:
#   - Market is in a measured chop/sideways state (price-action proof)
#   - Implied vol is genuinely overpriced vs realized (alpha proof)
# These signals come from independent traditions — technical analysis
# (CI) and academic finance (VRP) — so the AND-gate is theoretically
# expected to fire (no negative-correlation trap as with the prior
# AND-of-three gate).
#
# Both thresholds are LITERATURE-CANONICAL, NOT TUNED:
#   - CI 61.8: Fibonacci-based, in every Choppiness Index reference
#     (TradingView, ASX, Dreiss original; see Angel One/IncredibleCharts)
#   - VRP > 0: textbook break-even point in Bollerslev et al. and
#     every premium-selling paper since
#
# Period choices also literature-standard:
#   - CI period 14 (matches ADX/RSI convention; same as Dreiss original)
#   - VRP uses 20-day RV (matches existing RV_PERIOD_DAYS for parity
#     with industry IV-rank convention) and current-tick India VIX
CHOPPINESS_PERIOD = 14
CHOPPINESS_RANGE_THRESHOLD = 61.8   # >= this = range-bound (Fibonacci)
CHOPPINESS_TREND_THRESHOLD = 38.2   # <= this = trending (Fibonacci)
VRP_FAVORABLE_THRESHOLD = 0.0       # VRP > 0 → IV > RV → favourable (textbook break-even)


# ─── Enums ──────────────────────────────────────────────────────

class VolRegime(str, Enum):
    """Volatility regime from VIX (Indian-calibrated bands)."""
    LOW = "low"             # VIX <= 13 — complacency, no premium selling
    NORMAL = "normal"       # VIX 13-16 — strangle ideal
    HIGH = "high"           # VIX 16-20 — iron condor ideal
    EXTREME = "extreme"     # VIX > 20 — stressed; >25 = no trade


class ActionRegime(str, Enum):
    """Price action regime from candles."""
    RANGE_BOUND = "range_bound"
    CHOPPY = "choppy"
    TRENDING = "trending"


# Backward compat — old code references MarketRegime
class MarketRegime(str, Enum):
    """Combined regime classification (backward compatible)."""
    LOW_VOL_RANGE = "low_vol_range"
    NORMAL_RANGE = "normal_range"
    HIGH_VOL_RANGE = "high_vol_range"
    CHOPPY = "choppy"
    TRENDING = "trending"
    EXTREME_VOL = "extreme_vol"
    PRE_MARKET = "pre_market"
    CONFLICTED = "conflicted"
    UNKNOWN = "unknown"


# ─── Data Classes ───────────────────────────────────────────────

@dataclass
class ActionScores:
    """Independent scores from three action detectors (0-1 each)."""
    range_bound: float = 0.0
    choppy: float = 0.0
    trending: float = 0.0

    @property
    def winner(self) -> ActionRegime:
        scores = {
            ActionRegime.RANGE_BOUND: self.range_bound,
            ActionRegime.CHOPPY: self.choppy,
            ActionRegime.TRENDING: self.trending,
        }
        return max(scores, key=scores.get)

    @property
    def confidence(self) -> float:
        """Gap between top and second score. High = clear regime."""
        sorted_scores = sorted([self.range_bound, self.choppy, self.trending], reverse=True)
        return sorted_scores[0] - sorted_scores[1]

    @property
    def is_conflicted(self) -> bool:
        """Two or more detectors scoring high = regime transition."""
        high_count = sum(1 for s in [self.range_bound, self.choppy, self.trending] if s > 0.5)
        return high_count >= 2


@dataclass
class RegimeSnapshot:
    """Point-in-time regime assessment with full 2D data."""
    # Combined regime (backward compatible)
    regime: MarketRegime

    # 2D decomposition
    vol_regime: VolRegime = VolRegime.NORMAL
    action_regime: ActionRegime = ActionRegime.RANGE_BOUND
    action_scores: ActionScores = field(default_factory=ActionScores)

    # Market data
    vix: float = 0.0
    morning_range_pct: float = 0.0
    move_from_open_pct: float = 0.0
    session_open: float = 0.0
    current_spot: float = 0.0

    # Recommendations
    recommended_strategy: str = "sit_out"
    recommended_lots_multiplier: float = 0.0
    reason: str = ""
    timestamp: datetime = field(default_factory=datetime.now)

    # Legacy fields
    trend_direction: str = ""
    move_efficiency: float = 0.0
    chop_score: float = 0.0


# ─── Strategy Lookup Table (Vol × Action × Confidence) ─────────

def _recommend(vol: VolRegime, action: ActionRegime, confidence: float, conflicted: bool) -> tuple[str, float, str]:
    """2D lookup: returns (strategy, lots_multiplier, reason).

    Conflicted regimes always reduce size regardless of vol/action.
    """
    if conflicted:
        return "iron_condor", 0.25, f"REGIME CONFLICT — reduce risk (confidence={confidence:.2f})"

    low_conf = confidence < 0.2

    lookup = {
        # (vol, action): (strategy, lots_mult)
        # LOW (VIX <13): complacency — premium too cheap, naked sellers stop working
        (VolRegime.LOW, ActionRegime.RANGE_BOUND): ("sit_out", 0.0),
        (VolRegime.LOW, ActionRegime.CHOPPY): ("sit_out", 0.0),
        (VolRegime.LOW, ActionRegime.TRENDING): ("trend_debit_spread", 0.5),

        # NORMAL (VIX 13-16): strangle ideal band
        (VolRegime.NORMAL, ActionRegime.RANGE_BOUND): ("short_strangle", 1.0),
        (VolRegime.NORMAL, ActionRegime.CHOPPY): ("iron_condor", 0.5),
        (VolRegime.NORMAL, ActionRegime.TRENDING): ("trend_debit_spread", 1.0),

        # HIGH (VIX 16-20): iron condor ideal — wings protect against expansion
        (VolRegime.HIGH, ActionRegime.RANGE_BOUND): ("iron_condor", 1.0),
        (VolRegime.HIGH, ActionRegime.CHOPPY): ("iron_condor", 0.5),
        (VolRegime.HIGH, ActionRegime.TRENDING): ("trend_debit_spread", 1.0),

        # EXTREME (VIX >20): stressed — IC only, sized down; vix_entry_max blocks >25
        (VolRegime.EXTREME, ActionRegime.RANGE_BOUND): ("iron_condor", 0.5),
        (VolRegime.EXTREME, ActionRegime.CHOPPY): ("sit_out", 0.0),
        (VolRegime.EXTREME, ActionRegime.TRENDING): ("trend_debit_spread", 0.5),
    }

    strategy, lots = lookup.get((vol, action), ("sit_out", 0.0))
    reason = f"{vol.value}_{action.value}"

    if low_conf and lots > 0:
        lots = max(0.25, lots * 0.5)
        reason += " (low_confidence, halved)"

    return strategy, lots, reason


# ─── Regime Detector ────────────────────────────────────────────

class RegimeDetector:
    """2D market regime detector with independent action scorers.

    Usage:
        detector = RegimeDetector(feed, aggregator, chain_builder)
        snapshot = detector.assess("NIFTY")

        # New 2D API:
        if snapshot.vol_regime == VolRegime.HIGH and snapshot.action_scores.is_conflicted:
            # Uncertain high-vol market — sit out

        # Backward compatible:
        if snapshot.regime == MarketRegime.CHOPPY:
            # Reduce size
    """

    def __init__(
        self,
        feed: TickFeedManager,
        aggregator: OHLCAggregator,
        chain_builder: OptionChainBuilder,
        clock: "MarketClock | None" = None,
    ):
        self._feed = feed
        self._aggregator = aggregator
        self._chain_builder = chain_builder
        # May 5 2026 fix: ``clock`` is the simulated MarketClock during
        # backtest, None in live mode (falls back to ``now_ist()`` wall
        # clock). Without this, ``assess()``'s call to ``now_ist()``
        # always returned the REAL wall-clock date, so the day-rollover
        # check in ``_maybe_capture_daily_close`` never fired during
        # backtests — the daily-close deque stayed empty and
        # ``compute_realized_vol`` always returned None. This single
        # bug was responsible for every "0 trades" smoke that used
        # ``require_premium_selling_regime=True``.
        self._clock = clock
        self._session_opens: dict[str, float] = {}
        self._last_regime: dict[str, RegimeSnapshot] = {}
        self._last_session_date: date | None = None
        # May 2 2026: daily-close history for realized-vol computation.
        # Maintained per-underlying via rolling deque (need RV_PERIOD_DAYS+1
        # closes to compute RV_PERIOD_DAYS log returns).
        self._daily_closes: dict[str, deque] = {}
        self._daily_close_running: dict[str, float] = {}
        self._last_close_date: dict[str, date] = {}

    def assess(self, underlying: str) -> RegimeSnapshot:
        """Assess current market regime using 2D model.

        Returns RegimeSnapshot with both legacy regime and new 2D decomposition.
        """
        # May 5 2026 fix: prefer the simulated clock (set at __init__) so
        # backtest day-rollover detection actually works. Falls through
        # to ``now_ist()`` only when no clock was provided (live mode).
        now = self._clock.now() if self._clock is not None else now_ist()
        today = now.date()
        if self._last_session_date and self._last_session_date != today:
            logger.info("[RegimeDetector] New trading day — resetting session data")
            self.reset_session()
        self._last_session_date = today

        vix = self._get_vix()
        spot = float(self._chain_builder.get_spot_price(underlying))
        session_open = self._get_session_open(underlying)
        morning_range = self._get_morning_range(underlying)

        move_from_open = 0.0
        if session_open > 0 and spot > 0:
            move_from_open = abs(spot - session_open) / session_open * 100

        # May 2 2026: maintain daily-close history for realized-vol calc.
        # Captures the running close every tick; promotes to a stored
        # close on day-rollover.
        self._maybe_capture_daily_close(underlying, now, spot)

        # Pre-market — not enough data
        if now.time() < time(9, 20) or spot <= 0:
            snap = self._build_snap(
                MarketRegime.PRE_MARKET, VolRegime.NORMAL, ActionRegime.RANGE_BOUND,
                ActionScores(), vix, morning_range, move_from_open,
                session_open, spot, "sit_out", 0.0,
                "Pre-market: insufficient data", now, underlying,
            )
            return snap

        # ─── Dimension 1: Volatility (from VIX) ─────────────
        vol = self._classify_vol(vix)

        # ─── Dimension 2: Price Action (three independent scorers) ──
        action_scores = self._score_all_actions(underlying, session_open, spot, vix, morning_range, move_from_open, now)

        # ─── Consensus + Conflict Detection ──────────────────
        action = action_scores.winner
        confidence = action_scores.confidence
        conflicted = action_scores.is_conflicted

        # ─── Strategy Recommendation (2D lookup) ─────────────
        strategy, lots_mult, reason = _recommend(vol, action, confidence, conflicted)

        # ─── Map to legacy MarketRegime for backward compat ──
        if conflicted:
            legacy_regime = MarketRegime.CONFLICTED
        elif action == ActionRegime.CHOPPY:
            legacy_regime = MarketRegime.CHOPPY
        elif action == ActionRegime.TRENDING:
            legacy_regime = MarketRegime.TRENDING
        elif vol == VolRegime.EXTREME:
            legacy_regime = MarketRegime.EXTREME_VOL
        elif vol == VolRegime.HIGH:
            legacy_regime = MarketRegime.HIGH_VOL_RANGE
        elif vol == VolRegime.NORMAL:
            legacy_regime = MarketRegime.NORMAL_RANGE
        else:
            legacy_regime = MarketRegime.LOW_VOL_RANGE

        # Trend direction
        trend_dir = ""
        if action == ActionRegime.TRENDING and session_open > 0:
            trend_dir = "UP" if spot > session_open else "DOWN"

        detail = (
            f"vol={vol.value} action={action.value} "
            f"scores=[R={action_scores.range_bound:.2f} C={action_scores.choppy:.2f} T={action_scores.trending:.2f}] "
            f"confidence={confidence:.2f}{' CONFLICTED' if conflicted else ''}"
        )

        snap = self._build_snap(
            legacy_regime, vol, action, action_scores,
            vix, morning_range, move_from_open,
            session_open, spot, strategy, lots_mult,
            f"{detail} → {strategy} @ {lots_mult:.1f}x",
            now, underlying,
        )
        snap.trend_direction = trend_dir
        snap.move_efficiency = action_scores.trending  # Approximate
        snap.chop_score = action_scores.choppy

        return snap

    # ─── Dimension 1: Vol Classification ─────────────────────

    def _classify_vol(self, vix: float) -> VolRegime:
        if vix > VIX_EXTREME:
            return VolRegime.EXTREME
        elif vix > VIX_NORMAL:
            return VolRegime.HIGH
        elif vix > VIX_LOW:
            return VolRegime.NORMAL
        else:
            return VolRegime.LOW

    # ─── Dimension 2: Three Independent Action Scorers ───────

    def _score_all_actions(
        self, underlying: str, session_open: float, spot: float,
        vix: float, morning_range: float, move_from_open: float,
        now: datetime,
    ) -> ActionScores:
        """Run all three action detectors independently."""
        range_score = self._score_range_bound(morning_range, move_from_open, session_open, spot)
        chop_score = self._score_choppy(underlying, session_open, spot, vix, now)
        trend_score = self._score_trending(morning_range, move_from_open, underlying, session_open, spot)

        scores = ActionScores(
            range_bound=range_score,
            choppy=chop_score,
            trending=trend_score,
        )

        logger.info(
            f"[RegimeDetector] {underlying} action_scores: "
            f"R={range_score:.2f} C={chop_score:.2f} T={trend_score:.2f} "
            f"winner={scores.winner.value} conf={scores.confidence:.2f} "
            f"conflict={scores.is_conflicted}"
        )

        return scores

    def _score_range_bound(self, morning_range: float, move_from_open: float,
                           session_open: float, spot: float) -> float:
        """Score how range-bound the market is (0-1).

        High score = tight range, near open, low movement.
        """
        score = 0.0

        # Tight morning range
        if morning_range <= 0.3:
            score += 0.40
        elif morning_range <= 0.5:
            score += 0.25
        elif morning_range <= 0.7:
            score += 0.10

        # Close to open
        if move_from_open <= 0.15:
            score += 0.40
        elif move_from_open <= 0.3:
            score += 0.25
        elif move_from_open <= 0.5:
            score += 0.10

        # Price near session open (within 0.2%)
        if session_open > 0 and spot > 0:
            dist = abs(spot - session_open) / session_open * 100
            if dist < 0.2:
                score += 0.20
            elif dist < 0.4:
                score += 0.10

        return min(1.0, score)

    def _score_choppy(self, underlying: str, session_open: float, spot: float,
                      vix: float, now: datetime) -> float:
        """Score how choppy the market is (0-1).

        High score = wide range but directionless, reversals, efficiency dropping.
        Starts at T+30 (9:45) with limited data, improves over time.
        Re-evaluated every call — catches late-developing chop.
        """
        if now.time() < time(9, 45):
            return 0.0  # Need at least 30 min

        spot_token = self._find_spot_token(underlying)
        if not spot_token:
            return 0.0

        candles = self._aggregator.get_completed_candles(spot_token, Timeframe.M5, limit=20)
        if len(candles) < 6:
            return 0.0

        # Range and efficiency
        highs = [float(c.high) for c in candles]
        lows = [float(c.low) for c in candles]
        session_range = max(highs) - min(lows)

        if session_range <= 0 or session_open <= 0:
            return 0.0

        range_pct = session_range / session_open * 100
        net_move = abs(spot - session_open)
        efficiency = net_move / session_range

        # Direction reversals (normalized by candle count — works at any time)
        reversals = 0
        last_direction = None
        for c in candles:
            direction = "up" if float(c.close) > float(c.open) else "down"
            if last_direction and direction != last_direction:
                reversals += 1
            last_direction = direction
        reversal_rate = reversals / len(candles) if candles else 0

        # Efficiency drop: compare first half vs full window
        # (trend falling apart = chop developing)
        efficiency_early = 0.5
        half = len(candles) // 2
        if half >= 3:
            early = candles[:half]
            early_range = max(float(c.high) for c in early) - min(float(c.low) for c in early)
            early_net = abs(float(early[-1].close) - session_open)
            efficiency_early = early_net / early_range if early_range > 0 else 0.5
        efficiency_drop = max(0, efficiency_early - efficiency)

        # Composite score (5 factors, max 1.0)
        score = 0.0

        # Factor 1: Low efficiency (strongest signal)
        if efficiency < 0.15:
            score += 0.30
        elif efficiency < 0.25:
            score += 0.20
        elif efficiency < 0.35:
            score += 0.10

        # Factor 2: Active market (must have range — dead market is not chop)
        if range_pct > 0.6:
            score += 0.20
        elif range_pct > 0.35:
            score += 0.15

        # Factor 3: High reversal rate (>30% of candles reverse = choppy)
        if reversal_rate > 0.4:
            score += 0.25
        elif reversal_rate > 0.3:
            score += 0.15
        elif reversal_rate > 0.2:
            score += 0.05

        # Factor 4: Efficiency drop (trend falling apart)
        if efficiency_drop > 0.15:
            score += 0.15
        elif efficiency_drop > 0.08:
            score += 0.10

        # Factor 5: VIX in chop danger zone (16-22 = 55% choppy historically)
        if 16 <= vix <= 22:
            score += 0.10

        return min(1.0, score)

    def _score_trending(self, morning_range: float, move_from_open: float,
                        underlying: str, session_open: float, spot: float) -> float:
        """Score how directional/trending the market is (0-1).

        High score = sustained move from open, expanding range.
        """
        score = 0.0

        # Large move from open
        if move_from_open > 1.5:
            score += 0.40
        elif move_from_open > 1.0:
            score += 0.35
        elif move_from_open > 0.7:
            score += 0.25
        elif move_from_open > 0.5:
            score += 0.15

        # Wide morning range
        if morning_range > 1.2:
            score += 0.25
        elif morning_range > 0.8:
            score += 0.15
        elif morning_range > 0.5:
            score += 0.10

        # Efficiency (move in one direction)
        spot_token = self._find_spot_token(underlying)
        if spot_token:
            candles = self._aggregator.get_completed_candles(spot_token, Timeframe.M5, limit=12)
            if len(candles) >= 6:
                highs = [float(c.high) for c in candles]
                lows = [float(c.low) for c in candles]
                total_range = max(highs) - min(lows)
                net_move = abs(spot - session_open) if session_open > 0 else 0
                efficiency = net_move / total_range if total_range > 0 else 0

                if efficiency > 0.7:
                    score += 0.35
                elif efficiency > 0.5:
                    score += 0.25
                elif efficiency > 0.3:
                    score += 0.10

        return min(1.0, score)

    # ─── Helpers ─────────────────────────────────────────────

    def get_last_regime(self, underlying: str) -> RegimeSnapshot | None:
        return self._last_regime.get(underlying)

    def _get_vix(self) -> float:
        ltp = self._feed.get_ltp(INDIA_VIX_TOKEN)
        return float(ltp) if ltp else 0.0

    def _get_session_open(self, underlying: str) -> float:
        if underlying in self._session_opens:
            return self._session_opens[underlying]
        spot_token = self._find_spot_token(underlying)
        if not spot_token:
            return 0.0
        candles = self._aggregator.get_completed_candles(spot_token, Timeframe.M1, limit=5)
        if candles:
            self._session_opens[underlying] = float(candles[0].open)
            return self._session_opens[underlying]
        return float(self._chain_builder.get_spot_price(underlying))

    def _get_morning_range(self, underlying: str) -> float:
        spot_token = self._find_spot_token(underlying)
        if not spot_token:
            return 0.0
        candles = self._aggregator.get_completed_candles(spot_token, Timeframe.M5, limit=6)
        if not candles:
            return 0.0
        highs = [float(c.high) for c in candles]
        lows = [float(c.low) for c in candles]
        morning_high = max(highs)
        morning_low = min(lows)
        mid = (morning_high + morning_low) / 2
        return (morning_high - morning_low) / mid * 100 if mid > 0 else 0.0

    def _find_spot_token(self, underlying: str) -> int | None:
        for token, name in self._chain_builder._spot_tokens.items():
            if name == underlying:
                return token
        return None

    def _build_snap(
        self, regime, vol, action, action_scores,
        vix, morning_range, move_from_open,
        session_open, spot, strategy, lots_mult,
        reason, now, underlying,
    ) -> RegimeSnapshot:
        snap = RegimeSnapshot(
            regime=regime,
            vol_regime=vol,
            action_regime=action,
            action_scores=action_scores,
            vix=vix,
            morning_range_pct=morning_range,
            move_from_open_pct=move_from_open,
            session_open=session_open,
            current_spot=spot,
            recommended_strategy=strategy,
            recommended_lots_multiplier=lots_mult,
            reason=reason,
            timestamp=now,
        )
        if underlying:
            self._last_regime[underlying] = snap
        logger.info(
            f"[RegimeDetector] {underlying} {regime.value}: VIX={vix:.1f} "
            f"range={morning_range:.2f}% move={move_from_open:.2f}% "
            f"-> {strategy} @ {lots_mult:.1f}x"
        )
        return snap

    def reset_session(self) -> None:
        self._session_opens.clear()
        self._last_regime.clear()

    # ─── Indian-market range-detection methods (May 2 2026) ─────
    # ADX, BB squeeze, RV/IV — proven institutional indicators
    # calibrated for NIFTY/BANKNIFTY 5-min spot bars.

    def compute_adx(
        self, underlying: str, period: int = ADX_PERIOD,
        timeframe: Timeframe = Timeframe.M5,
    ) -> float | None:
        """Compute ADX(period) on the latest spot bars.

        ADX (Wilder 1978): measures trend strength independent of direction.
        ADX < 22 = no trend = range-bound (Indian-calibrated; US default 20).
        ADX > 28 = strong trend (avoid premium-selling).
        Returns None when insufficient bars.
        """
        spot_token = self._find_spot_token(underlying)
        if not spot_token:
            return None
        # Need period+1 bars to compute period TR/DM values, then another
        # period bars to seed ATR/DI smoothing — total ~2×period+1.
        need = period * 2 + 2
        candles = self._aggregator.get_completed_candles(spot_token, timeframe, limit=need)
        if len(candles) < period + 2:
            return None

        highs = [float(c.high) for c in candles]
        lows = [float(c.low) for c in candles]
        closes = [float(c.close) for c in candles]
        n = len(highs)

        # +DM, -DM, TR per bar (starting at i=1)
        plus_dm: list[float] = []
        minus_dm: list[float] = []
        tr: list[float] = []
        for i in range(1, n):
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
            minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
            tr.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            ))

        if len(tr) < period:
            return None

        # Wilder smoothing: initialise as period-window sum, then
        # smoothed_t = smoothed_{t-1} - smoothed_{t-1}/period + value_t
        atr = sum(tr[:period])
        plus_smooth = sum(plus_dm[:period])
        minus_smooth = sum(minus_dm[:period])
        dx_values: list[float] = []
        for i in range(period, len(tr)):
            atr = atr - atr / period + tr[i]
            plus_smooth = plus_smooth - plus_smooth / period + plus_dm[i]
            minus_smooth = minus_smooth - minus_smooth / period + minus_dm[i]
            if atr <= 0:
                continue
            plus_di = 100.0 * plus_smooth / atr
            minus_di = 100.0 * minus_smooth / atr
            di_sum = plus_di + minus_di
            if di_sum <= 0:
                continue
            dx = 100.0 * abs(plus_di - minus_di) / di_sum
            dx_values.append(dx)

        if len(dx_values) < period:
            return None
        # ADX = Wilder smoothing of DX over period
        adx = sum(dx_values[:period]) / period
        for i in range(period, len(dx_values)):
            adx = (adx * (period - 1) + dx_values[i]) / period
        return adx

    def compute_bb_squeeze(
        self, underlying: str, period: int = BB_PERIOD,
        std_mult: float = BB_STD_MULT, lookback: int = BB_SQUEEZE_LOOKBACK,
        timeframe: Timeframe = Timeframe.M5,
    ) -> tuple[bool | None, float | None]:
        """Detect Bollinger Band squeeze on spot closes.

        BB width = (upper - lower) / middle expressed in %. A squeeze
        is when current BB width is in the bottom ``BB_SQUEEZE_PERCENTILE``
        of the last ``lookback`` readings — indicating compressed
        volatility / consolidation period (favourable for IC entries).

        Returns (is_squeeze, current_width_pct). Either may be None on
        insufficient data.
        """
        spot_token = self._find_spot_token(underlying)
        if not spot_token:
            return None, None
        need = period + lookback
        candles = self._aggregator.get_completed_candles(spot_token, timeframe, limit=need)
        if len(candles) < period + 5:  # Need at least 5 BB-width readings
            return None, None

        closes = [float(c.close) for c in candles]
        bb_widths: list[float] = []
        for i in range(period - 1, len(closes)):
            window = closes[i - period + 1: i + 1]
            mean = sum(window) / period
            if mean <= 0:
                continue
            variance = sum((x - mean) ** 2 for x in window) / period
            sd = math.sqrt(variance)
            # Full BB band width as % of mid (2 × std on each side = 4 × std total)
            width_pct = (2.0 * std_mult * sd) / mean * 100.0
            bb_widths.append(width_pct)

        if len(bb_widths) < 5:
            return None, None
        current = bb_widths[-1]
        # Use the most-recent ``lookback`` readings (or whatever we have)
        recent = bb_widths[-lookback:] if len(bb_widths) >= lookback else bb_widths
        sorted_recent = sorted(recent)
        idx = max(0, int(len(sorted_recent) * BB_SQUEEZE_PERCENTILE / 100.0) - 1)
        threshold = sorted_recent[idx]
        return current <= threshold, current

    def _maybe_capture_daily_close(self, underlying: str, now: datetime, spot: float) -> None:
        """Maintain rolling daily-close history for realized-vol calc.

        Runs on every assess() call. Captures the latest spot as the
        "running close" for the current day; on day rollover, the prior
        running close becomes a stored daily close.
        """
        if spot <= 0:
            return
        today = now.date()
        last_close_dt = self._last_close_date.get(underlying)
        if last_close_dt is None:
            self._last_close_date[underlying] = today
            self._daily_close_running[underlying] = spot
            return
        if today != last_close_dt:
            # New day — yesterday's running close becomes a stored close.
            running = self._daily_close_running.get(underlying, 0.0)
            if running > 0:
                if underlying not in self._daily_closes:
                    self._daily_closes[underlying] = deque(
                        maxlen=RV_PERIOD_DAYS + 5  # small buffer
                    )
                self._daily_closes[underlying].append(running)
            self._last_close_date[underlying] = today
            self._daily_close_running[underlying] = spot
        else:
            self._daily_close_running[underlying] = spot

    async def warmup_daily_closes(
        self,
        historical_fn,
        underlying: str,
        spot_token: int,
        days: int = 35,
    ) -> int:
        """Seed ``_daily_closes`` from a historical-data callback at startup.

        WHY THIS EXISTS
        ---------------
        ``_capture_daily_close()`` builds the daily-close history purely
        from in-memory live ticks: each new trading day adds 1 close. The
        v2 gate needs ``RV_PERIOD_DAYS+1`` (=21) closes to compute VRP.
        Combined with the launchd 08:50 IST daily restart, the deque
        resets to empty every morning and the gate returns
        ``insufficient_data`` perpetually — v2 never fires.

        This loader is called once from the strategy's ``on_start()`` to
        backfill the deque from broker historical data. After warmup the
        gate fires from the first trading day post-deployment.

        Parameters
        ----------
        historical_fn :
            Async callable matching ``Broker.get_historical_data`` —
            ``async (token, from_date, to_date, interval) -> list[dict]``
            where each dict has ``date`` and ``close`` keys.
        underlying : str
            "NIFTY" or "BANKNIFTY". Keys ``_daily_closes``.
        spot_token : int
            Instrument token for the spot index (e.g. 256265 for NIFTY).
        days : int
            Calendar lookback. Default 35 to ensure 25+ trading days
            even with weekends + holidays.

        Returns
        -------
        int : Number of closes seeded into the deque (0 on failure).

        Failure mode: any exception (network, auth, empty response) is
        logged and swallowed. Strategy startup must not block on warmup.
        Returns 0 → gate stays in ``insufficient_data`` until live ticks
        eventually accumulate (same as before warmup existed).
        """
        from datetime import timedelta
        try:
            now = self._clock.now() if self._clock is not None else now_ist()
            from_date = now - timedelta(days=days)
            bars = await historical_fn(spot_token, from_date, now, "day")
        except Exception as e:
            logger.warning(
                f"[RegimeDetector] warmup_daily_closes({underlying}) failed: {e}"
            )
            return 0

        if not bars:
            logger.warning(
                f"[RegimeDetector] warmup_daily_closes({underlying}): "
                f"historical_fn returned 0 bars"
            )
            return 0

        # Sort ascending by date (Kite returns ascending already, but be defensive)
        try:
            bars_sorted = sorted(bars, key=lambda b: b["date"])
        except (KeyError, TypeError) as e:
            logger.warning(
                f"[RegimeDetector] warmup_daily_closes({underlying}): "
                f"unexpected bar shape: {e}"
            )
            return 0

        if underlying not in self._daily_closes:
            self._daily_closes[underlying] = deque(maxlen=RV_PERIOD_DAYS + 5)

        # Keep only the most recent (RV_PERIOD_DAYS + 5) closes — the
        # deque maxlen would clamp anyway but we want last_close_date
        # to point at the actual most-recent bar we seeded.
        kept = bars_sorted[-(RV_PERIOD_DAYS + 5):]
        seeded = 0
        for b in kept:
            try:
                close = float(b["close"])
                if close > 0:
                    self._daily_closes[underlying].append(close)
                    seeded += 1
            except (KeyError, ValueError, TypeError):
                continue

        if seeded > 0:
            # Set _last_close_date to the date of the LAST seeded bar so
            # _maybe_capture_daily_close() correctly detects "today" as
            # a new day and promotes today's running close on rollover.
            last_bar = kept[-1]
            try:
                last_date = last_bar["date"]
                if hasattr(last_date, "date"):  # datetime → date
                    last_date = last_date.date()
                self._last_close_date[underlying] = last_date
                # Initialise running close to the last seeded close so
                # the deque is consistent until the next live tick.
                self._daily_close_running[underlying] = float(last_bar["close"])
            except Exception:
                pass

        logger.info(
            f"[RegimeDetector] warmup_daily_closes({underlying}): "
            f"seeded {seeded} closes (last_date={self._last_close_date.get(underlying)}, "
            f"deque_size={len(self._daily_closes[underlying])})"
        )
        return seeded

    def compute_realized_vol(
        self, underlying: str, period_days: int = RV_PERIOD_DAYS,
    ) -> float | None:
        """Compute annualised realized vol (%) from rolling daily closes.

        Returns None when fewer than ``period_days+1`` daily closes are
        stored (need N+1 closes for N daily log returns). Vol is
        annualised using NSE ``RV_ANNUALIZATION_DAYS`` = 252 trading
        days/year and reported as a percentage to match VIX scale.
        """
        closes = self._daily_closes.get(underlying)
        if not closes or len(closes) < period_days + 1:
            return None
        # Use the last period_days+1 closes
        seq = list(closes)[-(period_days + 1):]
        log_returns: list[float] = []
        for i in range(1, len(seq)):
            if seq[i - 1] <= 0 or seq[i] <= 0:
                continue
            log_returns.append(math.log(seq[i] / seq[i - 1]))
        if len(log_returns) < period_days // 2:  # need most of the window
            return None
        mean = sum(log_returns) / len(log_returns)
        var = sum((r - mean) ** 2 for r in log_returns) / max(1, len(log_returns) - 1)
        daily_std = math.sqrt(var)
        annual_vol_pct = daily_std * math.sqrt(RV_ANNUALIZATION_DAYS) * 100.0
        return annual_vol_pct

    def compute_rv_iv_ratio(self, underlying: str) -> float | None:
        """Realized vol / India VIX. Returns None on insufficient data.

        ratio < 0.80  → IV is rich, premium-selling has positive expectancy
        ratio 0.80-1.0 → IV approximately fair
        ratio > 1.0  → IV is cheap or under-priced — DON'T sell premium
        """
        rv = self.compute_realized_vol(underlying)
        if rv is None:
            return None
        vix = self._get_vix()
        if vix <= 0:
            return None
        return rv / vix

    def is_premium_selling_favorable(
        self, underlying: str,
    ) -> tuple[bool, dict[str, float | bool | None]]:
        """Unified Indian-calibrated gate for premium-selling strategies.

        Returns (favorable, metrics). ``favorable=True`` means ALL of:
          - ADX(14) on 5-min spot < 22 (no trend / range)
          - BB squeeze active (volatility compression on 5-min)
          - RV/IV < 0.80 (IV genuinely overpriced vs realized)

        ``metrics`` includes the raw values for logging/decision audit.
        Any metric being None is treated as "insufficient data → NOT
        favourable" — we err on the side of NOT trading rather than
        guess. This is the user's principle: small loss / big profit /
        limit losses applied to the entry gate.
        """
        adx = self.compute_adx(underlying)
        bb_squeeze, bb_width = self.compute_bb_squeeze(underlying)
        rv = self.compute_realized_vol(underlying)
        rv_iv = self.compute_rv_iv_ratio(underlying)
        vix = self._get_vix()

        metrics = {
            "adx": adx,
            "adx_threshold": ADX_RANGE_THRESHOLD,
            "bb_squeeze": bb_squeeze,
            "bb_width_pct": bb_width,
            "realized_vol_pct": rv,
            "vix": vix,
            "rv_iv_ratio": rv_iv,
            "rv_iv_threshold": RV_IV_FAVORABLE_THRESHOLD,
        }

        # Any indicator missing → not favourable (insufficient data)
        if adx is None or bb_squeeze is None or rv_iv is None:
            metrics["reason"] = "insufficient_data"
            return False, metrics

        adx_ok = adx < ADX_RANGE_THRESHOLD
        bb_ok = bool(bb_squeeze)
        rv_iv_ok = rv_iv < RV_IV_FAVORABLE_THRESHOLD

        favourable = adx_ok and bb_ok and rv_iv_ok
        metrics["reason"] = (
            f"adx={adx:.1f}({'OK' if adx_ok else 'FAIL'}) "
            f"bb_sqz={bb_ok} "
            f"rv/iv={rv_iv:.2f}({'OK' if rv_iv_ok else 'FAIL'})"
        )
        return favourable, metrics

    # ─── v2 detectors (Apr 30 2026): Choppiness Index + VRP ──────
    # Replaces the AND-of-three gate that fired on 0/2590 valid samples.
    # Two ORTHOGONAL literature-grounded indicators with canonical
    # thresholds — see module-level docstring for derivation.

    def compute_choppiness_index(
        self, underlying: str, period: int = CHOPPINESS_PERIOD,
        timeframe: Timeframe = Timeframe.M5,
    ) -> float | None:
        """Compute Choppiness Index over the latest spot bars.

        Bill Dreiss (ASX) original formula:
            CI = 100 * log10(sum_TR / (max_high − min_low)) / log10(period)

        where sum_TR is the sum of TRUE RANGE over ``period`` bars and
        max_high / min_low span the same window. CI is bounded [0, 100]
        when the formula is well-defined.

        Interpretation (literature-standard Fibonacci thresholds):
          - CI ≥ 61.8 → range-bound / chopping market
          - CI ≤ 38.2 → strong trend
          - 38.2-61.8 → transitional

        Returns None when fewer than ``period+1`` bars are available
        (need period TR values, which need period+1 closes).
        """
        spot_token = self._find_spot_token(underlying)
        if not spot_token:
            return None
        candles = self._aggregator.get_completed_candles(
            spot_token, timeframe, limit=period + 5
        )
        if len(candles) < period + 1:
            return None

        # Use the most-recent ``period+1`` bars for ``period`` TR values
        bars = candles[-(period + 1):]
        highs = [float(c.high) for c in bars]
        lows = [float(c.low) for c in bars]
        closes = [float(c.close) for c in bars]

        tr_values: list[float] = []
        for i in range(1, len(bars)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            tr_values.append(tr)

        if len(tr_values) < period:
            return None
        sum_tr = sum(tr_values[-period:])
        # Range over the same period (excludes the seed bar at index 0,
        # matches Dreiss's "period" interpretation: the window of bars
        # whose TRs we summed).
        window_highs = highs[1:]
        window_lows = lows[1:]
        max_h = max(window_highs[-period:])
        min_l = min(window_lows[-period:])
        rng = max_h - min_l
        if rng <= 0 or sum_tr <= 0 or period <= 1:
            return None
        ratio = sum_tr / rng
        if ratio <= 0:
            return None
        ci = 100.0 * math.log10(ratio) / math.log10(period)
        # Clamp to formula's natural [0, 100] band; numerical noise can
        # produce values just outside on degenerate inputs.
        return max(0.0, min(100.0, ci))

    def compute_vrp(self, underlying: str) -> float | None:
        """Variance Risk Premium (VRP) = India VIX − 20-day realized vol.

        Both expressed in same units (annualised vol percent points), so
        VRP > 0 means implied vol is over-priced relative to delivered
        vol over the prior month. Bollerslev-Tauchen-Zhou (2009 RFS) is
        the canonical reference; positive VRP is the textbook
        premium-selling alpha source.

        Returns None when realized-vol history is insufficient (need
        ``RV_PERIOD_DAYS+1`` daily closes) or VIX is unavailable.
        """
        rv = self.compute_realized_vol(underlying)
        if rv is None:
            return None
        vix = self._get_vix()
        if vix <= 0:
            return None
        return vix - rv

    def is_premium_selling_favorable_v2(
        self, underlying: str,
    ) -> tuple[bool, dict[str, float | bool | None]]:
        """v2 gate: Choppiness Index ≥ 61.8 AND VRP > 0.

        Two ORTHOGONAL literature-grounded gates with canonical
        thresholds (no parameter tuning):

          - Choppiness Index ≥ 61.8 → market is in a measured chop /
            sideways regime. PURE price-action, no IV component.
          - VRP > 0 → implied vol exceeds realized vol → premium is
            genuinely overpriced. PURE volatility-pricing alpha.

        Independence of the two signals (technical vs academic) means
        the AND-gate does NOT suffer the negative-correlation failure
        mode that left the v1 (ADX + BB-squeeze + RV/IV) gate firing
        0 / 2590 times on Indian post-SEBI data.

        Insufficient data → NOT favourable (err on the side of don't
        trade — same conservative rule as v1).
        """
        ci = self.compute_choppiness_index(underlying)
        vrp = self.compute_vrp(underlying)
        rv = self.compute_realized_vol(underlying)
        vix = self._get_vix()

        metrics: dict[str, float | bool | None] = {
            "choppiness_index": ci,
            "ci_threshold": CHOPPINESS_RANGE_THRESHOLD,
            "vrp": vrp,
            "vrp_threshold": VRP_FAVORABLE_THRESHOLD,
            "realized_vol_pct": rv,
            "vix": vix,
        }

        if ci is None or vrp is None:
            metrics["reason"] = "insufficient_data"
            return False, metrics

        ci_ok = ci >= CHOPPINESS_RANGE_THRESHOLD
        vrp_ok = vrp > VRP_FAVORABLE_THRESHOLD
        favourable = ci_ok and vrp_ok
        metrics["reason"] = (
            f"ci={ci:.1f}({'OK' if ci_ok else 'FAIL'}) "
            f"vrp={vrp:+.2f}({'OK' if vrp_ok else 'FAIL'})"
        )
        return favourable, metrics

    def is_long_vol_favorable_v2(
        self, underlying: str,
    ) -> tuple[bool, dict[str, float | bool | None]]:
        """v2 gate for LONG-vol structures (calendar, long straddle, etc).

        Orthogonal to ``is_premium_selling_favorable_v2`` on the
        volatility axis only. Both gates require range-bound markets
        (Choppiness Index ≥ 61.8); they differ on whether IV is rich or
        cheap relative to realized:

          IC v2 (premium-selling):  CI ≥ 61.8  AND  VRP > 0  (IV rich)
          LC v2 (long-vol):         CI ≥ 61.8  AND  VRP < 0  (IV cheap)

        Why the gates share the range condition: long-calendar profits
        from spot staying near the ATM strike (theta differential
        between near and far expiries). A trending market drags spot
        away from the strike and kills the calendar's edge — so trending
        is bad for both IC and LC.

        Why they invert the vol condition:
          - Premium-selling needs IV to be over-priced relative to
            future delivered vol (VRP > 0). Selling expensive premium
            and letting it decay is the alpha.
          - Long-vol calendar needs IV to be UNDER-priced with room to
            expand. Buying cheap vega and waiting for IV to mean-revert
            up (or for a vol shock) is the alpha. Positive vol expansion
            on the back leg dwarfs the front leg's vega exposure.

        The two gates are MUTUALLY EXCLUSIVE: VRP cannot be both > 0 and
        < 0 simultaneously, so IC v2 and LC v2 never fire on the same
        underlying on the same day. Combined with the shared range
        condition, this gives a clean regime-routing pattern:

          range + IV-rich  → IC v2 fires
          range + IV-cheap → LC v2 fires
          trending         → NEITHER fires (correct — both need range)

        Insufficient data → NOT favourable (conservative — same as IC v2).
        """
        ci = self.compute_choppiness_index(underlying)
        vrp = self.compute_vrp(underlying)
        rv = self.compute_realized_vol(underlying)
        vix = self._get_vix()

        metrics: dict[str, float | bool | None] = {
            "choppiness_index": ci,
            "ci_threshold": CHOPPINESS_RANGE_THRESHOLD,
            "vrp": vrp,
            "vrp_threshold": VRP_FAVORABLE_THRESHOLD,
            "realized_vol_pct": rv,
            "vix": vix,
        }

        if ci is None or vrp is None:
            metrics["reason"] = "insufficient_data"
            return False, metrics

        ci_ok = ci >= CHOPPINESS_RANGE_THRESHOLD
        # Note the strict inequality: VRP < 0 (IV strictly under-priced)
        # is the textbook long-vol entry. VRP == 0 means IV ≈ RV — no
        # vol-pricing edge in either direction, don't pay the round-trip
        # cost.
        vrp_ok = vrp < VRP_FAVORABLE_THRESHOLD
        favourable = ci_ok and vrp_ok
        metrics["reason"] = (
            f"ci={ci:.1f}({'OK' if ci_ok else 'FAIL'}) "
            f"vrp={vrp:+.2f}({'OK_LV' if vrp_ok else 'FAIL_LV'})"
        )
        return favourable, metrics

    # ─── V5 (May 7 2026): regime-aware confidence scoring ────────
    # The boolean v2 gates above (is_premium_selling_favorable_v2,
    # is_long_vol_favorable_v2, is_long_vol_favorable_v2b) collapse the
    # underlying market structure into a binary pass/fail. The V5
    # orchestrator wants a CONTINUOUS confidence in [0.0, 1.0] so it can:
    #   - Compare strategies of DIFFERENT regime families on a common scale
    #     (premium-selling vs long-vol vs directional-trend)
    #   - Implement a cash-floor (refuse to allocate when max-confidence
    #     across all families is below threshold)
    #   - Smoothly down-size when conditions are marginal instead of binary
    #     on/off whipsaw on a tick crossing the threshold
    #
    # Each method returns 0.0 (insufficient data or unfavourable) up to
    # 1.0 (textbook-ideal regime). The math is deliberately simple — a
    # GEOMETRIC mean of factor sub-scores — so a single failing factor
    # drives the overall confidence to zero. Arithmetic mean would let a
    # high-VIX day with VRP < 0 still produce middling confidence for
    # premium-selling, which is exactly the failure mode we want to AVOID.
    #
    # Factor sub-scores are LINEARLY INTERPOLATED between empirically-
    # derived "fail" and "ideal" anchors. Anchors are the same canonical
    # thresholds used by the v2 boolean gates (CI 38.2/61.8, VRP 0,
    # VIX 13/16/22 etc). Where a soft transition makes more sense than
    # a hard cliff, the interpolation gives 0.0 below the lower anchor,
    # 1.0 above the upper anchor, and a linear ramp in between.

    def regime_confidence_for_premium_selling(self, underlying: str) -> float:
        """Continuous confidence (0.0-1.0) that conditions favour premium-selling.

        Combines four orthogonal factors via geometric mean:
          - CI factor:  range-bound score from Choppiness Index
                        (0 below CI=38.2 trend threshold; 1 above CI=61.8 range threshold)
          - VRP factor: variance-risk-premium positivity score
                        (0 at VRP=-2; 1 at VRP=+2; linear in between)
          - VIX factor: India-VIX band fitness for short premium
                        (0 outside 13-22; 1 in the 16-20 ideal IC band; 0.5 at edges)
          - DoW factor: day-of-week edge from Anurag Goel Sharpe-1.96 NIFTY backtest
                        (1.0 Tue/Wed/Thu, 0.5 Mon, 0.3 Fri)

        Returns 0.0 when any factor is missing (insufficient data) or
        any factor pegs to 0 (clearly unfavourable). The 4th-root
        geometric mean keeps each factor on equal footing and prevents
        a strong score on three factors masking a structural failure
        on the fourth.
        """
        ci = self.compute_choppiness_index(underlying)
        vrp = self.compute_vrp(underlying)
        vix = self._get_vix()
        if ci is None or vrp is None or vix <= 0:
            return 0.0

        # CI factor: fail at CI=38.2, ideal at CI=61.8
        ci_factor = max(0.0, min(1.0,
            (ci - CHOPPINESS_TREND_THRESHOLD) /
            (CHOPPINESS_RANGE_THRESHOLD - CHOPPINESS_TREND_THRESHOLD)
        ))

        # VRP factor: linear ramp -2 → +2 vol-points (annualised)
        vrp_factor = max(0.0, min(1.0, (vrp + 2.0) / 4.0))

        # VIX factor: NORMAL/HIGH bands ideal; LOW/EXTREME unsuitable
        if vix < 13.0 or vix > 22.0:
            vix_factor = 0.0
        elif 16.0 <= vix <= 20.0:
            vix_factor = 1.0
        elif 13.0 <= vix < 16.0:
            # Strangle band — moderate fit
            vix_factor = 0.6 + (vix - 13.0) * 0.4 / 3.0
        else:
            # 20 < vix <= 22, stressed but defined-risk OK
            vix_factor = 1.0 - (vix - 20.0) * 0.5 / 2.0

        # DoW factor (matches calendar-filter Anurag Goel canonical)
        now = self._clock.now() if self._clock is not None else now_ist()
        dow = now.weekday()
        dow_factor = {0: 0.5, 1: 1.0, 2: 1.0, 3: 1.0, 4: 0.3}.get(dow, 0.5)

        # 4th-root geometric mean — single-factor failure pulls the whole
        # confidence to zero (avoids the "good on 3, bad on 1" trap).
        product = ci_factor * vrp_factor * vix_factor * dow_factor
        if product <= 0:
            return 0.0
        return product ** 0.25

    def regime_confidence_for_long_vol(self, underlying: str) -> float:
        """Continuous confidence (0.0-1.0) that conditions favour long-vol structures.

        Long-vol = long calendar, long straddle. Profits when IV expands
        from depressed levels and/or spot moves away from the strike
        (long straddle) or stays near it (long calendar). The unifying
        factor is "IV is cheap with room to expand" — VRP < 0.

        Factor decomposition:
          - VRP factor:  inverse of premium-selling — 1 when VRP ≪ 0,
                         0 when VRP > 0 (IV is rich, no expansion alpha)
          - VIX factor:  long-vol wants moderate VIX with expansion room.
                         0 below 12 (no expansion expected),
                         1 in 14-20 band (typical expansion zone),
                         falling off above 22 (already-expanded, late entry)
          - CI factor:   long calendar prefers range; long straddle prefers
                         move. Use a NEUTRAL CI factor (1.0 — don't gate)
                         and let strategy-level configuration decide.
                         Actually: prefer CI in mid-range (38.2-61.8) where
                         neither strong trend nor strong chop dominates —
                         this is where vol mean-reverts cleanly.
        """
        vrp = self.compute_vrp(underlying)
        ci = self.compute_choppiness_index(underlying)
        vix = self._get_vix()
        if vrp is None or vix <= 0:
            return 0.0

        # VRP factor: 1 at VRP=-3, 0 at VRP=+1
        vrp_factor = max(0.0, min(1.0, (1.0 - vrp) / 4.0))

        # VIX factor: long-vol expansion zone
        if vix < 12.0 or vix > 25.0:
            vix_factor = 0.0
        elif 14.0 <= vix <= 20.0:
            vix_factor = 1.0
        elif 12.0 <= vix < 14.0:
            vix_factor = (vix - 12.0) / 2.0
        else:
            # 20 < vix <= 25 — already-expanded, late entry
            vix_factor = 1.0 - (vix - 20.0) / 5.0

        # CI factor: prefer mid-range (vol mean-reverts cleanly)
        # 0.5 at CI=0 or CI=100, 1.0 at CI=50, smooth quadratic dropoff
        if ci is None:
            ci_factor = 0.5  # Neutral when CI unavailable
        else:
            ci_factor = 1.0 - abs(ci - 50.0) / 50.0  # Triangle peak at 50
            ci_factor = max(0.3, ci_factor)          # Floor — don't kill on extreme CI

        product = vrp_factor * vix_factor * ci_factor
        if product <= 0:
            return 0.0
        return product ** (1.0 / 3.0)

    def regime_confidence_for_directional_trend(self, underlying: str) -> float:
        """Continuous confidence (0.0-1.0) that conditions favour directional/trend trades.

        Trend strategies (TrendDaily, TrendITM, TrendDebitSpread) profit
        on sustained directional moves. Detector signals:

          - ADX factor:   trend strength on 5-min spot
                          (0 at ADX<22; 1 at ADX>28; linear ramp)
          - VIX factor:   trend wants moderate vol — too low = no breakouts,
                          too high = mean-reverting whipsaw
                          (0 outside 12-22; 1 inside 14-20 band)
          - CI factor:    INVERSE of premium-selling — trend prefers low CI
                          (0 at CI=61.8 range threshold; 1 at CI=38.2 trend
                          threshold)

        Trend confidence is structurally the OPPOSITE of premium-selling
        on the CI dimension. The two should rarely both score high
        simultaneously — when they do (ADX high AND CI high), the market
        is in a confusing state and BOTH families should down-size.
        """
        ci = self.compute_choppiness_index(underlying)
        adx = self.compute_adx(underlying)
        vix = self._get_vix()
        if adx is None or vix <= 0:
            return 0.0

        # ADX factor: 0 at 22, 1 at 28+
        adx_factor = max(0.0, min(1.0, (adx - ADX_RANGE_THRESHOLD) /
                                       (ADX_TREND_THRESHOLD - ADX_RANGE_THRESHOLD)))

        # VIX factor: trend's sweet spot is moderate vol
        if vix < 12.0 or vix > 22.0:
            vix_factor = 0.0
        elif 14.0 <= vix <= 20.0:
            vix_factor = 1.0
        elif 12.0 <= vix < 14.0:
            vix_factor = (vix - 12.0) / 2.0
        else:
            # 20 < vix <= 22
            vix_factor = 1.0 - (vix - 20.0) / 2.0

        # CI factor: inverse of premium-selling
        if ci is None:
            ci_factor = 0.5  # Neutral
        else:
            ci_factor = max(0.0, min(1.0,
                (CHOPPINESS_RANGE_THRESHOLD - ci) /
                (CHOPPINESS_RANGE_THRESHOLD - CHOPPINESS_TREND_THRESHOLD)
            ))

        product = adx_factor * vix_factor * ci_factor
        if product <= 0:
            return 0.0
        return product ** (1.0 / 3.0)

    def regime_confidence_snapshot(self, underlying: str) -> dict[str, float]:
        """Compute confidence for all three regime families in one call.

        Returns a dict {family_name: confidence_0_to_1} suitable for
        logging and for the OrchestratorStrategy's selection logic. Used
        by the V5 coordinator to decide:
          - Which regime family is the strongest fit right now
          - Whether ANY family clears the cash-floor confidence threshold
          - How to allocate when multiple families are eligible

        Family names match the ``regime_family`` class attribute that
        each strategy declares (see BaseStrategy.regime_family).
        """
        return {
            "premium_selling": self.regime_confidence_for_premium_selling(underlying),
            "long_vol": self.regime_confidence_for_long_vol(underlying),
            "directional_trend": self.regime_confidence_for_directional_trend(underlying),
        }

    def is_long_vol_favorable_v2b(
        self, underlying: str,
    ) -> tuple[bool, dict[str, float | bool | None]]:
        """v2b gate for long-vol structures: pure VRP < 0 (no range condition).

        Single-condition gate, designed after the May 6 2026 173-day
        smoke of LC v2 (CI≥61.8 AND VRP<0) fired only 17 entries with
        net -₹1,707 train+val PnL. The CI condition was the binding
        constraint — Indian post-SEBI 5-min markets rarely register
        CI≥61.8 because intraday microstructure noise pushes the
        Choppiness Index below the Fibonacci threshold even on
        consolidating days.

        Theoretical justification for dropping CI: a long calendar
        with the v3 default ``min_back_days=21`` is dominated by the
        back-leg vega exposure, not the front-leg theta. Vega rises
        with IV, so when IV is cheap (VRP<0) and reverts up, the
        back leg gains more than the front loses, regardless of
        whether spot stays near the strike.

        The "spot must stay near strike" requirement that motivated
        the CI condition in v2 is now defended by the strategy's
        ``max_underlying_move_pct`` stop loss (default 1.5%) — a
        structural per-trade safety, not a regime gate.

        Insufficient data → NOT favourable (conservative, same as v2).
        """
        vrp = self.compute_vrp(underlying)
        rv = self.compute_realized_vol(underlying)
        vix = self._get_vix()

        metrics: dict[str, float | bool | None] = {
            "vrp": vrp,
            "vrp_threshold": VRP_FAVORABLE_THRESHOLD,
            "realized_vol_pct": rv,
            "vix": vix,
        }

        if vrp is None:
            metrics["reason"] = "insufficient_data"
            return False, metrics

        favourable = vrp < VRP_FAVORABLE_THRESHOLD
        metrics["reason"] = (
            f"vrp={vrp:+.2f}({'OK_LV2B' if favourable else 'FAIL_LV2B'})"
        )
        return favourable, metrics
