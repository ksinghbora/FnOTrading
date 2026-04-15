"""Persist advisories and audit data to DB and JSON files."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from src.advisor.models import Advisory, ConfluenceAudit

logger = logging.getLogger(__name__)


def save_advisory_json(advisory: Advisory, path: Path | None = None) -> None:
    """Save advisory to JSON file for quick access."""
    path = path or Path("data/latest_advisory.json")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(advisory.model_dump(mode="json"), f, indent=2, default=str)
        logger.info(f"[ADVISOR] Advisory saved to {path}")
    except OSError as e:
        logger.error(f"[ADVISOR] Failed to save advisory JSON: {e}")


def load_advisory_json(path: Path | None = None) -> Advisory | None:
    """Load latest advisory from JSON file."""
    path = path or Path("data/latest_advisory.json")
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        return Advisory(**data)
    except Exception as e:
        logger.warning(f"[ADVISOR] Failed to load advisory JSON: {e}")
        return None


def save_day_bias_json(advisory: Advisory, path: Path | None = None) -> None:
    """Save DayBias to JSON file for strategy consumption."""
    path = path or Path("data/day_bias.json")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(advisory.day_bias.model_dump(mode="json"), f, indent=2, default=str)
        logger.info(f"[ADVISOR] DayBias saved to {path}")
    except OSError as e:
        logger.error(f"[ADVISOR] Failed to save day_bias JSON: {e}")


def save_audit_json(audit: ConfluenceAudit, path: Path | None = None) -> None:
    """Save confluence audit to JSON file."""
    path = path or Path("data/latest_audit.json")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(audit.model_dump(mode="json"), f, indent=2, default=str)
        logger.info(f"[ADVISOR] Audit saved to {path}")
    except OSError as e:
        logger.error(f"[ADVISOR] Failed to save audit JSON: {e}")


def load_audit_json(path: Path | None = None) -> ConfluenceAudit | None:
    """Load latest audit from JSON file."""
    path = path or Path("data/latest_audit.json")
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        return ConfluenceAudit(**data)
    except Exception as e:
        logger.warning(f"[ADVISOR] Failed to load audit JSON: {e}")
        return None


async def save_advisory_db(advisory: Advisory, session_factory) -> None:
    """Save advisory to database."""
    from src.db.models.advisory import AdvisoryModel
    try:
        async with session_factory() as session:
            model = AdvisoryModel(
                date=advisory.date,
                risk_level=advisory.day_bias.risk_level,
                confidence=advisory.day_bias.confidence,
                summary=advisory.today_summary,
                full_advisory=advisory.model_dump(mode="json"),
                input_data={},  # Populated by caller
                day_bias=advisory.day_bias.model_dump(mode="json"),
            )
            session.add(model)
            await session.commit()
            logger.info(f"[ADVISOR] Advisory saved to DB for {advisory.date}")
    except Exception as e:
        logger.error(f"[ADVISOR] Failed to save advisory to DB: {e}")


async def save_audit_db(audit: ConfluenceAudit, session_factory) -> None:
    """Save confluence audit to database."""
    from src.db.models.advisory import ConfluenceAuditModel
    try:
        async with session_factory() as session:
            model = ConfluenceAuditModel(
                date=audit.date,
                decisions=[d.model_dump(mode="json") for d in audit.decisions],
                agree_count=audit.agree_count,
                disagree_count=audit.disagree_count,
                ai_right_count=audit.ai_right_count,
                rule_right_count=audit.rule_right_count,
                pnl_with_ai=audit.pnl_with_ai,
                pnl_rules_only=audit.pnl_rules_only,
                ai_alpha=audit.ai_alpha,
            )
            session.add(model)
            await session.commit()
            logger.info(f"[ADVISOR] Audit saved to DB for {audit.date}")
    except Exception as e:
        logger.error(f"[ADVISOR] Failed to save audit to DB: {e}")
