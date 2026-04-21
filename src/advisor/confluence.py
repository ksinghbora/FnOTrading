"""Confluence engine — combines AI advisor signal with rule-based scores."""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path

from src.advisor.models import DayBias

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.7  # Ignore AI signal below this
SIGNIFICANCE_THRESHOLD = 5  # Ignore adjustments smaller than ±5 points

# Per-minute dedup state for repetitive [CONFLUENCE] log lines. Without
# this, on a quiet day_bias=low_confidence morning the IGNORED gate fires
# on every tick — Apr 21 produced ~2,600 identical "[CONFLUENCE] leg=PREMIUM
# rule=70 ai_adj=+0 conf=0.00 IGNORED(low_confidence)" lines in 23 minutes.
# The shadow-audit parser (`src/advisor/shadow.py:build_audit`) already
# discards low-confidence rows via `if d.ai_adj == 0 or d.ai_confidence < 0.7`,
# and over-counting agreements per-tick distorts the not_significant /
# shadow audit math too — so deduping these is *more* accurate, not less.
# Key = (leg, gate_reason); the "applied" path is never deduped (those
# lines are rare and the final score must surface every time).
_LAST_LOG_MINUTE: dict[tuple[str, str], int] = {}


def _emit_throttled(
    key: tuple[str, str], detail: str, minute: int | None
) -> None:
    """Log `detail` once per `minute` per `key`. minute=None → no throttle.

    Callers in production (portfolio_strategy) pass the strategy clock's
    current minute so replay/backtest runs honor the simulated clock —
    using time.time() here would collapse every log line in a fast
    historical replay to a single entry.
    """
    if minute is None:
        logger.info(detail)
        return
    if _LAST_LOG_MINUTE.get(key) == minute:
        return
    _LAST_LOG_MINUTE[key] = minute
    logger.info(detail)


def reset_confluence_log_dedup() -> None:
    """Clear the module-level dedup cache. Called by `reset_day_state` so
    the first decision of a new session always logs cleanly even if the
    process spans a midnight rollover (back-to-back trading days)."""
    _LAST_LOG_MINUTE.clear()


def load_day_bias(path: Path | None = None, *, as_of: date | None = None) -> DayBias | None:
    """Load DayBias from JSON file. Returns None if unavailable.

    Production: reads `data/day_bias.json` (single file, written daily by
    the morning advisor cron).

    Replay/backtest: when `BACKFILL_DAY_BIAS_DIR` env var is set AND `as_of`
    is provided, reads `<dir>/day_bias_<YYYY-MM-DD>.json` instead. This lets
    the chain-replay engine swap the bias file per simulated day without
    code changes to the production hot path. Falls back to the production
    path if the per-day file is missing — caller can decide whether that's
    a soft-skip or a hard error.
    """
    if as_of is not None:
        backfill_dir = os.getenv("BACKFILL_DAY_BIAS_DIR")
        if backfill_dir:
            per_day = Path(backfill_dir) / f"day_bias_{as_of.isoformat()}.json"
            if per_day.exists():
                path = per_day
            # Else: fall through to default — common in production where
            # BACKFILL_DAY_BIAS_DIR is unset. We don't warn because that
            # would spam the live log on every day reset.

    path = path or Path("data/day_bias.json")
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        return DayBias(**data)
    except Exception as e:
        logger.warning(f"[CONFLUENCE] Could not load day_bias.json: {e}")
        return None


def apply_confluence(
    rule_score: int,
    day_bias: DayBias | None,
    leg: str,
    *,
    enabled: bool = False,
    weight: float = 1.0,
    dedup_minute: int | None = None,
) -> tuple[int, str]:
    """Apply AI confluence adjustment to rule-based score.

    Args:
        rule_score: Original score from rule-based scoring (0-100).
        day_bias: Today's AI advisory signal (or None if unavailable).
        leg: "premium" or "trend".
        enabled: If False, log but don't apply (shadow mode).
        weight: Scale factor for AI adjustment (0.0-1.0).
        dedup_minute: When provided, IGNORED/shadow log lines are emitted
            at most once per minute per (leg, gate_reason). Pass the
            strategy clock's current minute (`now.hour*60 + now.minute`)
            so replay engines honor the simulated clock. None → no
            throttle (preserves historical behaviour for tests/scripts
            that don't have a clock to thread through). The "applied"
            path is never throttled — those lines are rare and the
            final score must always surface.

    Returns:
        (final_score, log_detail) — adjusted score and structured log string.
    """
    if not day_bias:
        return rule_score, ""

    # Select per-leg values
    if leg == "premium":
        adj = day_bias.premium_score_adj
        conf = day_bias.premium_confidence
    elif leg == "trend":
        adj = day_bias.trend_score_adj
        conf = day_bias.trend_confidence
    else:
        return rule_score, ""

    shadow_tag = "" if enabled else "(shadow)"

    # Gate 1: Confidence too low
    if conf < CONFIDENCE_THRESHOLD:
        detail = (
            f"[CONFLUENCE] leg={leg.upper()} rule={rule_score} "
            f"ai_adj={adj:+d} conf={conf:.2f} IGNORED(low_confidence) {shadow_tag}"
        )
        _emit_throttled((leg, "low_confidence"), detail, dedup_minute)
        return rule_score, detail

    # Gate 2: Adjustment not significant
    if abs(adj) < SIGNIFICANCE_THRESHOLD:
        detail = (
            f"[CONFLUENCE] leg={leg.upper()} rule={rule_score} "
            f"ai_adj={adj:+d} conf={conf:.2f} IGNORED(not_significant) {shadow_tag}"
        )
        _emit_throttled((leg, "not_significant"), detail, dedup_minute)
        return rule_score, detail

    # Gate 3: Scale by confidence and weight
    scaled_adj = int(adj * conf * weight)

    if not enabled:
        # Shadow mode: log what would happen but don't apply
        would_be = rule_score + scaled_adj
        detail = (
            f"[CONFLUENCE] leg={leg.upper()} rule={rule_score} "
            f"ai_adj={adj:+d} conf={conf:.2f} scaled={scaled_adj:+d} "
            f"would_be={would_be} (shadow)"
        )
        _emit_throttled((leg, "shadow"), detail, dedup_minute)
        return rule_score, detail

    # Active mode: apply adjustment — never throttled (rare, must surface).
    final = max(0, min(100, rule_score + scaled_adj))
    detail = (
        f"[CONFLUENCE] leg={leg.upper()} rule={rule_score} "
        f"ai_adj={adj:+d} conf={conf:.2f} weight={weight:.1f} "
        f"scaled={scaled_adj:+d} final={final}"
    )
    logger.info(detail)
    return final, detail


def get_sizing_multiplier(day_bias: DayBias | None, *, enabled: bool = False) -> float:
    """Get position sizing multiplier from AI advisor.

    Returns 1.0 (no change) if advisor unavailable, disabled, or low confidence.
    """
    if not day_bias or not enabled:
        return 1.0

    if day_bias.confidence < CONFIDENCE_THRESHOLD:
        return 1.0

    return day_bias.sizing_multiplier
