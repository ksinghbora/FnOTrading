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
) -> tuple[int, str]:
    """Apply AI confluence adjustment to rule-based score.

    Args:
        rule_score: Original score from rule-based scoring (0-100).
        day_bias: Today's AI advisory signal (or None if unavailable).
        leg: "premium" or "trend".
        enabled: If False, log but don't apply (shadow mode).
        weight: Scale factor for AI adjustment (0.0-1.0).

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
        logger.info(detail)
        return rule_score, detail

    # Gate 2: Adjustment not significant
    if abs(adj) < SIGNIFICANCE_THRESHOLD:
        detail = (
            f"[CONFLUENCE] leg={leg.upper()} rule={rule_score} "
            f"ai_adj={adj:+d} conf={conf:.2f} IGNORED(not_significant) {shadow_tag}"
        )
        logger.info(detail)
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
        logger.info(detail)
        return rule_score, detail

    # Active mode: apply adjustment
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
