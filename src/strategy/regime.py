"""Market regime detector — 2D model with independent scorers and consensus.

Two independent dimensions:
  1. Volatility (from VIX): LOW | NORMAL | HIGH | EXTREME
  2. Price Action (from candles): RANGE_BOUND | CHOPPY | TRENDING

Three independent action scorers run in parallel. If two score high
simultaneously, that's a CONFLICT — regime is transitioning, reduce risk.

Strategy recommendation is a 2D lookup: Vol × Action × Confidence.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum

from src.core.clock import now_ist
from src.core.constants import INDIA_VIX_TOKEN, VIX_EXTREME, VIX_HIGH, VIX_LOW, VIX_NORMAL
from src.core.types import Timeframe
from src.market_data.aggregator import OHLCAggregator
from src.market_data.feed import TickFeedManager
from src.market_data.option_chain import OptionChainBuilder

logger = logging.getLogger(__name__)


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
    ):
        self._feed = feed
        self._aggregator = aggregator
        self._chain_builder = chain_builder
        self._session_opens: dict[str, float] = {}
        self._last_regime: dict[str, RegimeSnapshot] = {}
        self._last_session_date: date | None = None

    def assess(self, underlying: str) -> RegimeSnapshot:
        """Assess current market regime using 2D model.

        Returns RegimeSnapshot with both legacy regime and new 2D decomposition.
        """
        now = now_ist()
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
