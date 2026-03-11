"""Market regime detector — determines the optimal strategy for current conditions.

Analyzes VIX level, intraday price action, and morning range to classify
the market regime and recommend which strategy to run (or to sit out).
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum

from src.core.constants import INDIA_VIX_TOKEN, VIX_EXTREME, VIX_HIGH, VIX_LOW, VIX_NORMAL
from src.core.types import Timeframe
from src.market_data.aggregator import OHLCAggregator
from src.market_data.feed import TickFeedManager
from src.market_data.option_chain import OptionChainBuilder

logger = logging.getLogger(__name__)


class MarketRegime(str, Enum):
    """Current market regime classification."""
    LOW_VOL_RANGE = "low_vol_range"         # VIX < 14, range < 0.5% → aggressive strangle
    NORMAL_RANGE = "normal_range"           # VIX 14-18, range < 0.7% → standard strangle
    HIGH_VOL_RANGE = "high_vol_range"       # VIX 18-22, range < 1.0% → iron condor
    TRENDING = "trending"                   # Any VIX, range > 1.0% → sit out or directional
    EXTREME_VOL = "extreme_vol"             # VIX > 22 → sit out entirely
    PRE_MARKET = "pre_market"               # Before 9:20, not enough data yet
    UNKNOWN = "unknown"                     # Data unavailable


@dataclass
class RegimeSnapshot:
    """Point-in-time regime assessment with all supporting data."""
    regime: MarketRegime
    vix: float
    morning_range_pct: float          # High-low range of first 15 minutes
    move_from_open_pct: float         # Current spot vs session open
    session_open: float
    current_spot: float
    recommended_strategy: str         # "short_strangle", "iron_condor", "sit_out"
    recommended_lots_multiplier: float  # 1.0 = full, 0.5 = half, 0.0 = skip
    reason: str
    timestamp: datetime


class RegimeDetector:
    """Detects market regime from VIX, price action, and morning range.

    Usage:
        detector = RegimeDetector(feed, aggregator, chain_builder)
        snapshot = detector.assess("NIFTY")
        if snapshot.regime == MarketRegime.TRENDING:
            # Don't enter premium selling
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
        self._session_opens: dict[str, float] = {}  # underlying -> first open price of day
        self._last_regime: dict[str, RegimeSnapshot] = {}
        self._last_session_date: date | None = None  # Track current session date

    def assess(self, underlying: str) -> RegimeSnapshot:
        """Assess current market regime for an underlying.

        Should be called periodically (e.g., every 5 minutes or before entry).
        Auto-resets session data when the date changes.
        """
        now = datetime.now()
        today = now.date()
        if self._last_session_date and self._last_session_date != today:
            logger.info(f"[RegimeDetector] New trading day detected — resetting session data")
            self.reset_session()
        self._last_session_date = today
        vix = self._get_vix()
        spot = float(self._chain_builder.get_spot_price(underlying))
        session_open = self._get_session_open(underlying)
        morning_range = self._get_morning_range(underlying)

        move_from_open = 0.0
        if session_open > 0 and spot > 0:
            move_from_open = abs(spot - session_open) / session_open * 100

        # Not enough data yet
        if now.time() < time(9, 20) or spot <= 0:
            return self._snap(
                MarketRegime.PRE_MARKET, vix, morning_range, move_from_open,
                session_open, spot, "sit_out", 0.0,
                "Pre-market: insufficient data", now, underlying
            )

        # Extreme VIX — sit out entirely
        if vix > VIX_HIGH:
            return self._snap(
                MarketRegime.EXTREME_VOL, vix, morning_range, move_from_open,
                session_open, spot, "sit_out", 0.0,
                f"VIX {vix:.1f} exceeds {VIX_HIGH} — too dangerous for premium selling",
                now, underlying
            )

        # Strong trend — sit out or go directional
        if move_from_open > 1.0 or morning_range > 1.2:
            return self._snap(
                MarketRegime.TRENDING, vix, morning_range, move_from_open,
                session_open, spot, "sit_out", 0.0,
                f"Market trending: {move_from_open:.2f}% from open, "
                f"morning range {morning_range:.2f}%", now, underlying
            )

        # High VIX but range-bound — iron condor (defined risk)
        if vix > VIX_NORMAL:
            return self._snap(
                MarketRegime.HIGH_VOL_RANGE, vix, morning_range, move_from_open,
                session_open, spot, "iron_condor", 0.5,
                f"High VIX {vix:.1f} but range-bound — use defined-risk iron condor",
                now, underlying
            )

        # Normal VIX + range-bound — standard strangle
        if vix > VIX_LOW:
            return self._snap(
                MarketRegime.NORMAL_RANGE, vix, morning_range, move_from_open,
                session_open, spot, "short_strangle", 1.0,
                f"Normal regime: VIX {vix:.1f}, range {move_from_open:.2f}%",
                now, underlying
            )

        # Low VIX + range-bound — aggressive strangle (max lots)
        return self._snap(
            MarketRegime.LOW_VOL_RANGE, vix, morning_range, move_from_open,
            session_open, spot, "short_strangle", 1.5,
            f"Low VIX {vix:.1f} — favorable for premium selling", now, underlying
        )

    def get_last_regime(self, underlying: str) -> RegimeSnapshot | None:
        """Get the most recent regime assessment."""
        return self._last_regime.get(underlying)

    def _get_vix(self) -> float:
        """Get current India VIX value."""
        ltp = self._feed.get_ltp(INDIA_VIX_TOKEN)
        return float(ltp) if ltp else 0.0

    def _get_session_open(self, underlying: str) -> float:
        """Get today's session opening price.

        Captures the first available price after 9:15 and caches it for the day.
        """
        if underlying in self._session_opens:
            return self._session_opens[underlying]

        spot_token = self._find_spot_token(underlying)
        if not spot_token:
            return 0.0

        # Try to get the first 1-minute candle's open
        candles = self._aggregator.get_completed_candles(spot_token, Timeframe.M1, limit=5)
        if candles:
            self._session_opens[underlying] = float(candles[0].open)
            return self._session_opens[underlying]

        # Fallback to current spot
        return float(self._chain_builder.get_spot_price(underlying))

    def _get_morning_range(self, underlying: str) -> float:
        """Calculate the high-low range of the first 15-30 minutes.

        Returns range as a percentage of the opening price.
        """
        spot_token = self._find_spot_token(underlying)
        if not spot_token:
            return 0.0

        # Get 5-minute candles (first 3-6 candles = 15-30 min)
        candles = self._aggregator.get_completed_candles(spot_token, Timeframe.M5, limit=6)
        if not candles:
            return 0.0

        highs = [float(c.high) for c in candles]
        lows = [float(c.low) for c in candles]
        if not highs or not lows:
            return 0.0

        morning_high = max(highs)
        morning_low = min(lows)
        mid = (morning_high + morning_low) / 2

        if mid <= 0:
            return 0.0

        return (morning_high - morning_low) / mid * 100

    def _find_spot_token(self, underlying: str) -> int | None:
        """Find the spot instrument token for an underlying."""
        for token, name in self._chain_builder._spot_tokens.items():
            if name == underlying:
                return token
        return None

    def _snap(
        self, regime, vix, morning_range, move_from_open,
        session_open, spot, strategy, lots_mult, reason, now,
        underlying: str = "",
    ) -> RegimeSnapshot:
        snapshot = RegimeSnapshot(
            regime=regime,
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
            self._last_regime[underlying] = snapshot
        logger.info(
            f"[RegimeDetector] {underlying or '?'} {regime.value}: VIX={vix:.1f} "
            f"range={morning_range:.2f}% move={move_from_open:.2f}% "
            f"-> {strategy} @ {lots_mult:.1f}x"
        )
        return snapshot

    def reset_session(self) -> None:
        """Reset session data at start of new trading day."""
        self._session_opens.clear()
        self._last_regime.clear()
