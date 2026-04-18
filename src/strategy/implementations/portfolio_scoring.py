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

import csv
import logging
from pathlib import Path

from src.strategy.indicators import BreakoutSignal

logger = logging.getLogger(__name__)


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

    Optional confirmation factors (ported from trend-improvements branch,
    Apr 15 2026):
      - vix_prev: VIX reading from ~20 min ago. Rising VIX (>1%) on UP
        breakout is a counter-signal (-15) — fear rising while price is
        rising is fakeout-shaped. Falling VIX on UP confirms the move
        (+10). Inverted for DOWN breakouts. Set to 0.0 to skip.
      - banknifty_confirming: True if BankNifty broke its own morning
        high (UP) or low (DOWN) in the same direction (+10). False if
        BN is diverging (-15) — sector-only move, not index-wide.
        None = unknown / not yet built (no penalty).
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

    # 5. VIX direction (+10 / -15) — ported from trend-improvements 766df2e.
    # Rising VIX + UP breakout = contradiction (fear rising while buying = fake).
    # Rising VIX + DOWN breakout = confirmation (fear + breakdown = real selling).
    # Threshold 1% to filter noise — VIX prints sub-percent jitter all session.
    if vix_prev > 0:
        vix_rising = vix > vix_prev * 1.01
        if breakout.direction == "UP":
            if vix_rising:
                score -= 15
                reasons.append(f"VIX_rising={vix:.1f}>{vix_prev:.1f} contradicts UP")
            else:
                score += 10
                reasons.append("VIX_stable/falling supports UP")
        else:  # DOWN
            if vix_rising:
                score += 10
                reasons.append(f"VIX_rising={vix:.1f} confirms DOWN")
            else:
                score -= 15
                reasons.append("VIX_falling contradicts DOWN(bounce risk)")

    # 6. BankNifty sector confirmation (+10 / -15) — ported from 766df2e.
    # BankNifty is ~33% of NIFTY weight. Divergence (NIFTY breaks but BN
    # doesn't) means a sector-specific move that often retraces.
    if banknifty_confirming is True:
        score += 10
        reasons.append("BankNifty confirming")
    elif banknifty_confirming is False:
        score -= 15
        reasons.append("BankNifty diverging(sector-only move)")

    return score, reasons


# ─── IV Rank Baseline (52-week VIX context) ──────────────────────────
# Ported from trend-improvements cd21a10. The "tastytrade insight":
# selling premium is most rewarding when current IV is high relative to
# the recent regime, not just absolutely high. A VIX of 15 means very
# different things in a 10-20 environment vs a 15-30 one.
#
# Currently used in shadow mode only — see iv_rank_shadow_adj() below.
# Promote to a hard score adjustment once 30+ trading days correlate
# IV-Rank-low entries with poor outcomes.


def load_iv_rank_baseline(vix_csv: str | Path = "data/india_vix_minute.csv") -> tuple[float, float]:
    """Compute 52-week VIX high and low from historical minute data.

    Uses the last 252 trading days of daily closing VIX values (last tick
    per day). Returns (vix_52w_high, vix_52w_low). Returns (0.0, 0.0) on
    any error so callers can detect unavailability and skip IV Rank.

    Side-effect-free apart from a WARNING log on failure.
    """
    try:
        path = Path(vix_csv)
        if not path.exists():
            return 0.0, 0.0

        # Group by date, keep last close per day
        daily: dict[str, float] = {}
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Tolerant of the two date column names we've used historically.
                ts = row.get("date") or row.get("ts") or row.get("timestamp")
                if not ts:
                    continue
                date_str = ts[:10]  # ISO prefix → "2025-09-29"
                close = row.get("close") or row.get("vix")
                if close is None:
                    continue
                try:
                    daily[date_str] = float(close)
                except ValueError:
                    continue

        if not daily:
            return 0.0, 0.0

        closes = [v for _, v in sorted(daily.items())]
        window = closes[-252:] if len(closes) >= 252 else closes
        return max(window), min(window)

    except Exception as e:  # pragma: no cover — defensive
        logger.warning(f"IV Rank baseline load failed: {e}")
        return 0.0, 0.0


def compute_iv_rank(vix: float, vix_52w_high: float, vix_52w_low: float) -> float | None:
    """Return IV Rank (0-100) or None if baseline unavailable.

    IV Rank = 100 × (current - 52w_low) / (52w_high - 52w_low). A rank of
    50 means current VIX is exactly halfway between the year's extremes.
    Returns None when the baseline is missing/degenerate so callers can
    distinguish "no signal" from "rank=0 (rock-bottom IV)".
    """
    rng = vix_52w_high - vix_52w_low
    if rng <= 0 or vix_52w_high <= 0:
        return None
    return round((vix - vix_52w_low) / rng * 100, 1)


def iv_rank_shadow_adj(iv_rank: float | None) -> tuple[int, str]:
    """Return (hypothetical_score_adj, reason) for IV Rank — NOT applied.

    Used in shadow mode to log what adjustment WOULD be made so we can
    correlate with outcomes before promoting to a hard filter.

    Rules (tastytrade-derived, calibrated to Indian market data):
      IV Rank < 30%  → -15  (selling at multi-year lows, thin premium)
      IV Rank 30-50% → -8   (below-average premium environment)
      IV Rank ≥ 50%  →  0   (acceptable or rich premium — no adjustment)
    """
    if iv_rank is None:
        return 0, "iv_rank=unavailable"
    if iv_rank < 30:
        return -15, f"iv_rank={iv_rank:.0f}% low(thin_premium)"
    if iv_rank < 50:
        return -8, f"iv_rank={iv_rank:.0f}% below_avg"
    return 0, f"iv_rank={iv_rank:.0f}% acceptable"
