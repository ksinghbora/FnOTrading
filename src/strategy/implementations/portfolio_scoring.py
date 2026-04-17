"""Pure scoring functions for the portfolio strategy.

Extracted from portfolio_strategy.py (Apr 17 trader-analysis #13 — god-class
refactor). These are deliberately module-level, side-effect-free, and take
all inputs as plain arguments so they can be unit-tested in isolation and
reused from counterfactual variants without dragging the strategy class in.

Each scorer returns `(score, reasons)` where:
  - score: 0..100 integer (caller compares against an entry threshold)
  - reasons: list[str] of human-readable contributions, written into the
    decision log so a reviewer 30 days later can see *why* the score was 72.
"""

from __future__ import annotations

from src.strategy.indicators import BreakoutSignal


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
) -> tuple[int, list[str]]:
    """Score conditions for trend following (0-100)."""
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

    # 4. VIX adequate (+20)
    if vix >= 14:
        score += 20
        reasons.append(f"VIX={vix:.1f} supports trend")
    elif vix >= 11:
        score += 8
        reasons.append(f"VIX={vix:.1f} low for trend")

    return score, reasons
