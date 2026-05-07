"""Unified signal-scoring primitives.

May 7 2026 — per-strategy ``*_CONFIG`` constants have moved into their
calibration modules under ``src/strategy/calibrations/``. This file
keeps the shared ``ScoreConfig`` dataclass and the ``score_strategy``
scoring function, plus a backward-compat re-export of each migrated
config.

Recalibrating IC's scoring weights now means editing
``src/strategy/calibrations/iron_condor.py`` ONLY — strangle, straddle,
and trend_debit_spread configs are physically separate files.
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


# ─── Backward-compat re-exports (lazy via module-level __getattr__) ──
# Active strategies' scoring configs live in their per-strategy
# calibration modules. We expose them as attributes of this module
# via a lazy __getattr__ to avoid the circular import that would
# otherwise occur (each calibration module imports ``ScoreConfig``
# from here at module-init time, so a top-level re-export from
# calibration modules would deadlock).
#
# Existing call sites like ``from src.strategy.scoring import
# IRON_CONDOR_CONFIG`` keep working unchanged — Python's import
# machinery falls through to ``__getattr__`` for module-level
# attribute lookups (PEP 562). New code should import directly
# from the calibration module to make the per-strategy isolation
# contract visible:
#
#     from src.strategy.calibrations.iron_condor import IRON_CONDOR_CONFIG

_CONFIG_REEXPORTS = {
    "IRON_CONDOR_CONFIG": ("src.strategy.calibrations.iron_condor", "IRON_CONDOR_CONFIG"),
    "SHORT_STRANGLE_CONFIG": ("src.strategy.calibrations.short_strangle", "SHORT_STRANGLE_CONFIG"),
    "SHORT_STRADDLE_CONFIG": ("src.strategy.calibrations.short_straddle", "SHORT_STRADDLE_CONFIG"),
    "TREND_DEBIT_SPREAD_CONFIG": ("src.strategy.calibrations.trend_debit_spread", "TREND_DEBIT_SPREAD_CONFIG"),
}


def __getattr__(name: str):
    if name in _CONFIG_REEXPORTS:
        import importlib
        module_path, attr = _CONFIG_REEXPORTS[name]
        module = importlib.import_module(module_path)
        return getattr(module, attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ScoreConfig",
    "score_strategy",
    "IRON_CONDOR_CONFIG",
    "SHORT_STRANGLE_CONFIG",
    "SHORT_STRADDLE_CONFIG",
    "TREND_DEBIT_SPREAD_CONFIG",
]
