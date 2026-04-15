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


def momentum_breakout(
    candles: list[OHLC],
    morning_candles: int = 3,
    confirmation_pct: float = 0.3,
) -> BreakoutSignal:
    """Detect morning range breakout from M5 candles.

    Uses the first `morning_candles` M5 candles (default 3 = 9:15-9:30)
    to define the morning range. A breakout is confirmed when the current
    price moves beyond the range by at least `confirmation_pct` percent.

    Args:
        candles: M5 candles for the day (oldest first), at least morning_candles + 1.
        morning_candles: Number of opening candles to define the range.
        confirmation_pct: Minimum % move beyond range to confirm breakout.

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

    # Current price = close of the latest candle
    current = float(candles[-1].close)

    # Check for upside breakout
    if current > morning_high:
        move_pct = (current - morning_high) / morning_high * 100
        if move_pct >= confirmation_pct:
            return BreakoutSignal(
                direction="UP",
                strength=move_pct,
                breakout_level=morning_high,
                morning_high=morning_high,
                morning_low=morning_low,
            )

    # Check for downside breakout
    if current < morning_low:
        move_pct = (morning_low - current) / morning_low * 100
        if move_pct >= confirmation_pct:
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
