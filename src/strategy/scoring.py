"""Unified signal scoring for all strategies.

Each strategy type has different ideal conditions, but the scoring dimensions
are the same: VIX, morning range, move from open, PCR, DTE.

Usage:
    scorer = StrategyScorer.for_iron_condor()
    score, reasons = scorer.score(vix=18, morning_range_pct=0.3, ...)
"""

from dataclasses import dataclass, field


@dataclass
class ScoreConfig:
    """Scoring weights and thresholds — different per strategy type."""

    name: str = "generic"

    # VIX scoring — (min, max, points) tuples for each band
    # First matching band wins
    vix_bands: list[tuple[float, float, int, str]] = field(default_factory=list)

    # Morning range — tighter is better for premium sellers, wider for trend
    range_tight_max: float = 0.3      # % — full points if range <= this
    range_tight_pts: int = 25
    range_moderate_max: float = 0.5
    range_moderate_pts: int = 12
    range_wide_max: float = 0.8
    range_wide_pts: int = 5
    range_invert: bool = False         # True for trend: wider = better

    # Move from open — flat is better for premium, directional for trend
    move_flat_max: float = 0.15
    move_flat_pts: int = 25
    move_mild_max: float = 0.3
    move_mild_pts: int = 12
    move_drift_max: float = 0.5
    move_drift_pts: int = 5
    move_invert: bool = False          # True for trend: bigger move = better

    # PCR scoring
    pcr_neutral_min: float = 0.8
    pcr_neutral_max: float = 1.2
    pcr_neutral_pts: int = 15
    pcr_acceptable_min: float = 0.6
    pcr_acceptable_max: float = 1.5
    pcr_acceptable_pts: int = 5
    pcr_extreme_penalty: int = -10

    # DTE scoring
    dte_safe_min: int = 3
    dte_safe_pts: int = 10
    dte_ok_min: int = 1
    dte_ok_pts: int = 5
    expiry_penalty: int = -10


# ─── Pre-built configs per strategy ─────────────────────────────

IRON_CONDOR_CONFIG = ScoreConfig(
    name="iron_condor",
    vix_bands=[
        (14, 20, 25, "ideal(IC)"),
        (20, 28, 20, "rich premiums(IC)"),
        (11, 14, 8, "thin premiums(IC)"),
        (28, 50, 5, "elevated(IC-protected)"),
        (0, 11, 0, "too low(IC)"),
    ],
)

SHORT_STRANGLE_CONFIG = ScoreConfig(
    name="short_strangle",
    vix_bands=[
        (12, 16, 25, "ideal"),
        (11, 20, 12, "acceptable"),
        (20, 25, 8, "high(risky naked)"),
        (25, 50, 0, "too high(naked)"),
        (0, 11, 5, "very low"),
    ],
)

SHORT_STRADDLE_CONFIG = ScoreConfig(
    name="short_straddle",
    # Straddle is ATM — very sensitive to moves, needs calm market
    vix_bands=[
        (11, 15, 25, "ideal(ATM)"),
        (15, 18, 15, "ok(ATM)"),
        (8, 11, 10, "low premium(ATM)"),
        (18, 22, 5, "risky(ATM)"),
        (22, 50, 0, "dangerous(ATM)"),
        (0, 8, 0, "dead market"),
    ],
    # Straddle needs very tight range
    range_tight_max=0.2,
    range_tight_pts=30,
    range_moderate_max=0.4,
    range_moderate_pts=15,
    range_wide_max=0.6,
    range_wide_pts=5,
    # Very sensitive to directional moves
    move_flat_max=0.1,
    move_flat_pts=30,
    move_mild_max=0.2,
    move_mild_pts=15,
    move_drift_max=0.3,
    move_drift_pts=5,
)

TREND_DEBIT_SPREAD_CONFIG = ScoreConfig(
    name="trend_debit_spread",
    # Trend needs VIX for option premium to be worth buying
    vix_bands=[
        (16, 25, 25, "good for trend"),
        (12, 16, 15, "adequate"),
        (25, 35, 10, "volatile(cheap spreads)"),
        (0, 12, 0, "too calm"),
        (35, 50, 5, "extreme"),
    ],
    # Trend wants WIDE range (breakout)
    range_invert=True,
    range_tight_max=0.5,    # inverted: >0.5% = good
    range_tight_pts=25,
    range_moderate_max=0.3,
    range_moderate_pts=12,
    range_wide_max=0.8,
    range_wide_pts=5,
    # Trend wants directional move
    move_invert=True,
    move_flat_max=0.3,      # inverted: >0.3% move = good
    move_flat_pts=25,
    move_mild_max=0.15,
    move_mild_pts=12,
    move_drift_max=0.5,
    move_drift_pts=5,
    # DTE matters less for trend
    dte_safe_pts=5,
    dte_ok_pts=3,
    expiry_penalty=-20,     # Debit spreads on expiry are very bad
)


def score_strategy(
    config: ScoreConfig,
    vix: float,
    morning_range_pct: float,
    move_from_open_pct: float,
    pcr_oi: float,
    is_expiry_day: bool,
    dte: int,
) -> tuple[int, list[str]]:
    """Score market conditions for a given strategy type.

    Returns (score 0-100, list of reason strings).
    """
    score = 0
    reasons: list[str] = []

    # 1. VIX scoring
    for lo, hi, pts, label in config.vix_bands:
        if lo <= vix <= hi:
            score += pts
            reasons.append(f"VIX={vix:.1f} {label}")
            break

    # 2. Morning range
    if config.range_invert:
        # Trend: wider range = better
        if morning_range_pct >= config.range_tight_max:
            score += config.range_tight_pts
            reasons.append(f"range={morning_range_pct:.2f}% breakout range")
        elif morning_range_pct >= config.range_moderate_max:
            score += config.range_moderate_pts
            reasons.append(f"range={morning_range_pct:.2f}% developing")
        elif morning_range_pct > 0:
            score += config.range_wide_pts
            reasons.append(f"range={morning_range_pct:.2f}% tight(bad for trend)")
    else:
        # Premium: tighter range = better
        if morning_range_pct <= config.range_tight_max:
            score += config.range_tight_pts
            reasons.append(f"range={morning_range_pct:.2f}% very tight")
        elif morning_range_pct <= config.range_moderate_max:
            score += config.range_moderate_pts
            reasons.append(f"range={morning_range_pct:.2f}% moderate")
        elif morning_range_pct <= config.range_wide_max:
            score += config.range_wide_pts
            reasons.append(f"range={morning_range_pct:.2f}% wide")

    # 3. Move from open
    if config.move_invert:
        # Trend: bigger move = better
        if move_from_open_pct >= config.move_flat_max:
            score += config.move_flat_pts
            reasons.append(f"move={move_from_open_pct:.2f}% directional")
        elif move_from_open_pct >= config.move_mild_max:
            score += config.move_mild_pts
            reasons.append(f"move={move_from_open_pct:.2f}% developing")
        elif move_from_open_pct > 0:
            score += config.move_drift_pts
            reasons.append(f"move={move_from_open_pct:.2f}% flat(bad for trend)")
    else:
        # Premium: flatter = better
        if move_from_open_pct <= config.move_flat_max:
            score += config.move_flat_pts
            reasons.append(f"move={move_from_open_pct:.2f}% flat")
        elif move_from_open_pct <= config.move_mild_max:
            score += config.move_mild_pts
            reasons.append(f"move={move_from_open_pct:.2f}% mild")
        elif move_from_open_pct <= config.move_drift_max:
            score += config.move_drift_pts
            reasons.append(f"move={move_from_open_pct:.2f}% drifting")

    # 4. PCR
    if config.pcr_neutral_min <= pcr_oi <= config.pcr_neutral_max:
        score += config.pcr_neutral_pts
        reasons.append(f"PCR={pcr_oi:.2f} neutral")
    elif config.pcr_acceptable_min <= pcr_oi <= config.pcr_acceptable_max:
        score += config.pcr_acceptable_pts
        reasons.append(f"PCR={pcr_oi:.2f} acceptable")
    elif pcr_oi > 0:
        score += config.pcr_extreme_penalty
        reasons.append(f"PCR={pcr_oi:.2f} extreme")

    # 5. DTE
    if not is_expiry_day and dte >= config.dte_safe_min:
        score += config.dte_safe_pts
        reasons.append(f"DTE={dte} safe")
    elif not is_expiry_day and dte >= config.dte_ok_min:
        score += config.dte_ok_pts
        reasons.append(f"DTE={dte}")

    # 6. Expiry penalty
    if is_expiry_day:
        score += config.expiry_penalty
        reasons.append(f"expiry_day({config.expiry_penalty:+d})")

    return max(0, score), reasons
