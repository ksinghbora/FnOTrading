"""Technical indicators for trend detection — pure functions, no external dependencies.

Computes from OHLC candles that context.get_candles() already provides.
"""

from dataclasses import dataclass
from decimal import Decimal

from src.core.models import OHLC


@dataclass
class BreakoutSignal:
    """Result of morning range breakout detection."""
    direction: str | None  # "UP", "DOWN", or None
    strength: float        # % move beyond breakout level
    breakout_level: float  # The level that was breached
    morning_high: float
    morning_low: float


def ema(closes: list[float], period: int) -> list[float]:
    """Compute exponential moving average.

    Args:
        closes: List of closing prices (oldest first).
        period: EMA lookback period.

    Returns:
        List of EMA values (same length as closes, early values use SMA seed).
    """
    if not closes or period <= 0:
        return []
    if period > len(closes):
        period = len(closes)

    # Seed with SMA of first `period` values
    sma = sum(closes[:period]) / period
    multiplier = 2.0 / (period + 1)

    result: list[float] = []
    for i, price in enumerate(closes):
        if i < period:
            # Fill early values with running SMA
            result.append(sum(closes[: i + 1]) / (i + 1))
        elif i == period:
            # First real EMA value seeded from SMA
            result.append(price * multiplier + sma * (1 - multiplier))
        else:
            result.append(price * multiplier + result[-1] * (1 - multiplier))

    return result


def atr(candles: list[OHLC], period: int = 14) -> float:
    """Compute Average True Range from OHLC candles.

    True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    Returns ATR in points. Returns 0.0 if insufficient candles.
    """
    if len(candles) < 2:
        return 0.0

    true_ranges: list[float] = []
    for i in range(1, len(candles)):
        h = float(candles[i].high)
        l = float(candles[i].low)
        pc = float(candles[i - 1].close)
        true_ranges.append(max(h - l, abs(h - pc), abs(l - pc)))

    window = true_ranges[-period:] if len(true_ranges) >= period else true_ranges
    return sum(window) / len(window) if window else 0.0


def candle_body_quality(candle: OHLC, direction: str) -> float:
    """Measure how decisively a candle closed in the breakout direction.

    Returns a value 0.0–1.0:
      1.0 = close exactly at high (UP) or low (DOWN) — perfectly decisive
      0.0 = close at opposite extreme — full wick, no conviction

    For a genuine breakout candle, expect body_quality > 0.65.
    A wick-driven breakout (close near midpoint) scores < 0.5 — classic fakeout.
    """
    candle_range = float(candle.high) - float(candle.low)
    if candle_range <= 0:
        return 1.0  # Doji at breakout level — treat as neutral (won't block)

    if direction == "UP":
        return (float(candle.close) - float(candle.low)) / candle_range
    else:  # DOWN
        return (float(candle.high) - float(candle.close)) / candle_range


def momentum_breakout(
    candles: list[OHLC],
    morning_candles: int = 3,
    confirmation_pct: float = 0.3,
    atr_multiplier: float = 1.0,
    body_quality_min: float = 0.65,
) -> BreakoutSignal:
    """Detect morning range breakout from M5 candles.

    Uses the first `morning_candles` M5 candles (default 3 = 9:15-9:30)
    to define the morning range. Two-stage confirmation:

    1. Price must clear the morning range by at least max(confirmation_pct%,
       atr_multiplier × ATR14) in points — whichever is larger. This makes
       the threshold VIX-adaptive rather than a fixed percentage.

    2. The breakout candle must close decisively in the breakout direction
       (body_quality > body_quality_min). A wick-driven spike that reverses
       within the candle is rejected — classic false breakout signature.

    Args:
        candles: M5 candles for the day (oldest first), at least morning_candles + 1.
        morning_candles: Number of opening candles to define the morning range.
        confirmation_pct: Fallback minimum % move (used if ATR unavailable).
        atr_multiplier: Require this many ATR14 units of clearance beyond range.
        body_quality_min: Breakout candle must close in top/bottom X% of range.

    Returns:
        BreakoutSignal with direction, strength, and levels.
    """
    no_signal = BreakoutSignal(
        direction=None, strength=0.0, breakout_level=0.0,
        morning_high=0.0, morning_low=0.0,
    )

    if len(candles) < morning_candles + 1:
        return no_signal

    # Define morning range from first N candles
    morning = candles[:morning_candles]
    morning_high = max(float(c.high) for c in morning)
    morning_low = min(float(c.low) for c in morning)

    if morning_high <= 0 or morning_low <= 0:
        return no_signal

    range_size = morning_high - morning_low
    if range_size <= 0:
        return no_signal

    # ATR-normalized required clearance (in points).
    # Use the non-morning candles for ATR to avoid morning range noise.
    atr_val = atr(candles, period=14)
    current_price = morning_high  # approximate for pct→pts conversion
    fallback_pts = confirmation_pct / 100.0 * current_price
    required_pts = max(fallback_pts, atr_multiplier * atr_val) if atr_val > 0 else fallback_pts

    # Current price = close of the latest candle
    last_candle = candles[-1]
    current = float(last_candle.close)

    # Check for upside breakout
    if current > morning_high:
        move_pts = current - morning_high
        if move_pts >= required_pts:
            # Body quality gate: candle must close decisively near its high
            bq = candle_body_quality(last_candle, "UP")
            if bq < body_quality_min:
                return no_signal  # Wick-driven spike — fakeout, reject
            move_pct = move_pts / morning_high * 100
            return BreakoutSignal(
                direction="UP",
                strength=move_pct,
                breakout_level=morning_high,
                morning_high=morning_high,
                morning_low=morning_low,
            )

    # Check for downside breakout
    if current < morning_low:
        move_pts = morning_low - current
        if move_pts >= required_pts:
            bq = candle_body_quality(last_candle, "DOWN")
            if bq < body_quality_min:
                return no_signal  # Wick-driven dip — fakeout, reject
            move_pct = move_pts / morning_low * 100
            return BreakoutSignal(
                direction="DOWN",
                strength=move_pct,
                breakout_level=morning_low,
                morning_high=morning_high,
                morning_low=morning_low,
            )

    return BreakoutSignal(
        direction=None, strength=0.0, breakout_level=0.0,
        morning_high=morning_high, morning_low=morning_low,
    )


def oi_breakout_confirm(
    high_oi_strikes: dict[str, list[dict]],
    spot: float,
    direction: str,
) -> bool:
    """Confirm trend by checking if spot breached high-OI resistance/support.

    For UP breakout: spot must be above the highest CE OI strike (resistance broken).
    For DOWN breakout: spot must be below the highest PE OI strike (support broken).

    Args:
        high_oi_strikes: Output of chain_analyzer.get_high_oi_strikes().
            {"ce_high_oi": [{"strike": Decimal, "oi": int}, ...],
             "pe_high_oi": [{"strike": Decimal, "oi": int}, ...]}
        spot: Current spot price.
        direction: "UP" or "DOWN".

    Returns:
        True if OI confirms the breakout direction.
    """
    if not high_oi_strikes or not direction:
        return False

    if direction == "UP":
        ce_strikes = high_oi_strikes.get("ce_high_oi", [])
        if not ce_strikes:
            return False
        # Highest OI CE strike = major resistance
        top_resistance = float(ce_strikes[0].get("strike", 0))
        return spot > top_resistance

    if direction == "DOWN":
        pe_strikes = high_oi_strikes.get("pe_high_oi", [])
        if not pe_strikes:
            return False
        # Highest OI PE strike = major support
        top_support = float(pe_strikes[0].get("strike", 0))
        return spot < top_support

    return False
