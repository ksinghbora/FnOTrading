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
from dataclasses import dataclass, field
from pathlib import Path

from src.strategy.indicators import BreakoutSignal

logger = logging.getLogger(__name__)


@dataclass
class ScoreBreakdown:
    """Per-factor breakdown of a trend-following score.

    Returned by :func:`score_trend_following_breakdown`. Carries the same
    headline `score` and `reasons` that :func:`score_trend_following` returns,
    plus the integer contribution of each individual factor and a flag for
    whether the [0, 100] clamp was hit. Phase A of the Apr 18 score
    validation plan logs every field per TREND decision so we can attribute
    blocked entries to specific factors during the 30-day shadow window.

    Invariant: `clamp_hit=False` implies `score == sum(f1..f6)`.
    """
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    f1_breakout: int = 0
    f2_oi: int = 0
    f3_duration: int = 0
    f4_vix_level: int = 0
    f5_vix_dir: int = 0
    f6_banknifty: int = 0
    clamp_hit: bool = False


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


def score_trend_following_breakdown(
    breakout: BreakoutSignal,
    oi_confirmed: bool,
    trend_duration_minutes: int,
    vix: float,
    vix_prev: float = 0.0,
    banknifty_confirming: bool | None = None,
) -> ScoreBreakdown:
    """Per-factor variant of :func:`score_trend_following`.

    Returns a :class:`ScoreBreakdown` carrying the headline score, the
    same human-readable reasons, AND the integer contribution of each
    individual factor (so the decision logger can attribute blocked
    entries during the 30-day shadow window — see
    `memory/score_validation_plan.md`).

    The scoring math is the canonical implementation; the back-compat
    :func:`score_trend_following` wrapper just projects this to
    `(score, reasons)` to keep existing callers and tests untouched.

    Calibrated for Indian markets (Apr 18 2026 review):

      - VIX-level (factor 4) reduced to +10/+5 to make room for VIX-direction
        (factor 5) without double-counting the same regime signal.
      - VIX-direction (factor 5) threshold raised from 1% → 2% — Indian
        VIX prints ~0.8% intraday jitter on quiet days, so a 1% bar would
        trip "rising" by random walk on 30-40% of sessions. 2% requires
        a real fear shift. vix_prev=0.0 skips the branch (back-compat).
      - BankNifty (factor 6) asymmetry inverted from +10/-15 to +5/-20:
        BN follows NIFTY ~80% of the time unconditionally (correlation
        ~0.85 on M5 returns), so confirmation is the common, low-info
        case; divergence is the rare, high-info case. The new weights
        match Bayesian information content. None = unknown, no penalty.

    Score is clamped to [0, 100] before return so the value is comparable
    against a fixed threshold; the docstring "0-100" promise is now real
    rather than aspirational. When the clamp fires, `clamp_hit=True` and
    `score != sum(f1..f6)` — the per-factor fields hold the unclamped
    contributions for honest attribution.
    """
    bd = ScoreBreakdown()

    if not breakout.direction:
        bd.reasons = ["no breakout"]
        return bd

    # 1. Breakout strength (+30)
    if breakout.strength >= 0.5:
        bd.f1_breakout = 30
        bd.reasons.append(f"breakout={breakout.strength:.2f}% strong")
    elif breakout.strength >= 0.3:
        bd.f1_breakout = 15
        bd.reasons.append(f"breakout={breakout.strength:.2f}% moderate")

    # 2. OI confirmation (+25)
    if oi_confirmed:
        bd.f2_oi = 25
        bd.reasons.append("OI confirmed")

    # 3. Trend sustained (+25)
    if trend_duration_minutes >= 30:
        bd.f3_duration = 25
        bd.reasons.append(f"sustained={trend_duration_minutes}min")
    elif trend_duration_minutes >= 15:
        bd.f3_duration = 12
        bd.reasons.append(f"sustained={trend_duration_minutes}min early")

    # 4. VIX level adequate (+10) — reduced from +20 (Apr 18). Factor 5
    # below now captures the "regime supports this direction" signal that
    # was previously bundled in here. Keeping both at +20/+10 each was
    # double-counting and inflating scores ~30% for trending VIX days.
    if vix >= 14:
        bd.f4_vix_level = 10
        bd.reasons.append(f"VIX={vix:.1f} supports trend")
    elif vix >= 11:
        bd.f4_vix_level = 5
        bd.reasons.append(f"VIX={vix:.1f} low for trend")

    # 5. VIX direction (+10 / -15) — ported from trend-improvements 766df2e.
    # Rising VIX + UP breakout = contradiction (fear rising while buying = fake).
    # Rising VIX + DOWN breakout = confirmation (fear + breakdown = real selling).
    # Threshold 2% (raised from 1% Apr 18): Indian VIX intraday jitter is
    # ~0.8%, so 1% tripped on noise; 2% requires a real fear shift.
    if vix_prev > 0:
        vix_rising = vix > vix_prev * 1.02
        if breakout.direction == "UP":
            if vix_rising:
                bd.f5_vix_dir = -15
                bd.reasons.append(f"VIX_rising={vix:.1f}>{vix_prev:.1f} contradicts UP")
            else:
                bd.f5_vix_dir = 10
                bd.reasons.append("VIX_stable/falling supports UP")
        else:  # DOWN
            if vix_rising:
                bd.f5_vix_dir = 10
                bd.reasons.append(f"VIX_rising={vix:.1f} confirms DOWN")
            else:
                bd.f5_vix_dir = -15
                bd.reasons.append("VIX_falling contradicts DOWN(bounce risk)")

    # 6. BankNifty sector confirmation (+5 / -20) — ported from 766df2e,
    # asymmetry recalibrated Apr 18. BN follows NIFTY ~80% unconditionally
    # (correlation ~0.85 on M5 returns), so confirmation is the common
    # weak-info case; divergence is the rare high-info case. Old +10/-15
    # was backward — common case earned more than rare case.
    if banknifty_confirming is True:
        bd.f6_banknifty = 5
        bd.reasons.append("BankNifty confirming")
    elif banknifty_confirming is False:
        bd.f6_banknifty = -20
        bd.reasons.append("BankNifty diverging(sector-only move)")

    # Clamp to 0-100 — keeps the threshold comparison semantically clean.
    # Without clamp, max possible was ~110 (90 base + 10 VIXdir + 5 BN)
    # which silently shifted what the 60-threshold "means" relative to
    # the historical backtest. Track whether clamp fired so the decision
    # logger can flag rows where attribution sums won't match the score.
    raw = (
        bd.f1_breakout + bd.f2_oi + bd.f3_duration
        + bd.f4_vix_level + bd.f5_vix_dir + bd.f6_banknifty
    )
    clamped = max(0, min(100, raw))
    bd.clamp_hit = (raw != clamped)
    bd.score = clamped
    return bd


def score_trend_following(
    breakout: BreakoutSignal,
    oi_confirmed: bool,
    trend_duration_minutes: int,
    vix: float,
    vix_prev: float = 0.0,
    banknifty_confirming: bool | None = None,
) -> tuple[int, list[str]]:
    """Score conditions for trend following (0-100, clamped at boundaries).

    Thin back-compat wrapper around :func:`score_trend_following_breakdown`
    that returns just the headline `(score, reasons)`. New callers that
    need per-factor attribution should use the breakdown function directly.
    """
    bd = score_trend_following_breakdown(
        breakout=breakout,
        oi_confirmed=oi_confirmed,
        trend_duration_minutes=trend_duration_minutes,
        vix=vix,
        vix_prev=vix_prev,
        banknifty_confirming=banknifty_confirming,
    )
    return bd.score, bd.reasons


# ─── IV Rank Baseline (52-week VIX context) ──────────────────────────
# Ported from trend-improvements cd21a10. The "tastytrade insight":
# selling premium is most rewarding when current IV is high relative to
# the recent regime, not just absolutely high. A VIX of 15 means very
# different things in a 10-20 environment vs a 15-30 one.
#
# Currently used in shadow mode only — see iv_rank_shadow_adj() below.
# Promote to a hard score adjustment once 30+ trading days correlate
# IV-Rank-low entries with poor outcomes.


# Resolve repo root once at import — this module lives at
# src/strategy/implementations/portfolio_scoring.py, so parents[3] is the
# repo root regardless of CWD. Fixes a silent (0,0) failure when the
# trader process is started from anywhere other than the repo root
# (systemd, container, `cd scripts && …`).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_VIX_CSV = _REPO_ROOT / "data" / "india_vix_minute.csv"


def load_iv_rank_baseline(
    vix_csv: str | Path | None = None,
    as_of_date: "date | None" = None,
) -> tuple[float, float]:
    """Compute 52-week VIX high and low from historical minute data.

    Uses the last 252 trading days of daily closing VIX values (last tick
    per day) **on or before** ``as_of_date``. Returns
    ``(vix_52w_high, vix_52w_low)``. Returns ``(0.0, 0.0)`` on any error
    so callers can detect unavailability and skip IV Rank.

    Args:
        vix_csv: optional override for the VIX minute CSV path.
        as_of_date: cutoff. Only daily closes with date <= as_of_date
            contribute to the 52w window. ``None`` means "use everything"
            (legacy behaviour). **Backtests must pass the strategy's
            current trading date** — without this, a Sep 2024 backtest
            would compute the 52w range from data through Apr 2026,
            silently leaking forward-looking VIX into IV-Rank features
            (Apr 25 2026 audit).

    Side-effect-free apart from a WARNING log on failure.
    """
    try:
        path = Path(vix_csv) if vix_csv is not None else _DEFAULT_VIX_CSV
        if not path.exists():
            return 0.0, 0.0

        as_of_str: str | None = as_of_date.isoformat() if as_of_date else None

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
                if as_of_str is not None and date_str > as_of_str:
                    # Skip future dates relative to the caller's "now".
                    continue
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
