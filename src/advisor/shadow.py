"""Shadow audit — track AI advisor accuracy vs rule-based system."""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

from src.advisor.models import ConfluenceAudit, DecisionRecord
from src.utils.log_tags import Tag

logger = logging.getLogger(__name__)


def parse_confluence_logs(log_lines: list[str]) -> list[DecisionRecord]:
    """Parse [CONFLUENCE] log lines into DecisionRecord objects."""
    records: list[DecisionRecord] = []

    for line in log_lines:
        if "[CONFLUENCE]" not in line:
            continue

        # Extract timestamp from log line (format varies by logger config)
        ts_match = re.search(r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})', line)
        timestamp = None
        if ts_match:
            from datetime import datetime
            try:
                timestamp = datetime.fromisoformat(ts_match.group(1))
            except ValueError:
                pass

        # Parse key=value pairs
        kv: dict[str, str] = {}
        for m in re.finditer(r'(\w+)=([^\s,()]+)', line):
            kv[m.group(1)] = m.group(2)

        leg = kv.get("leg", "").lower()
        if not leg:
            continue

        rule_score = int(kv.get("rule", "0"))
        ai_adj_str = kv.get("ai_adj", "0").lstrip("+")
        ai_adj = int(ai_adj_str)
        conf = float(kv.get("conf", "0.0"))

        # Determine if AI was applied or ignored
        applied = "IGNORED" not in line and "(shadow)" not in line

        # Determine final score
        final = int(kv.get("final", kv.get("would_be", str(rule_score))))

        records.append(DecisionRecord(
            timestamp=timestamp or date.today(),
            leg=leg,
            rule_score=rule_score,
            ai_adj=ai_adj,
            ai_confidence=conf,
            final_score=final,
            action="",  # Filled by cross-referencing entry/exit logs
            applied=applied,
        ))

    return records


def _read_confluence_lines(log_dir: Path, target_date: date) -> list[str]:
    """Read [CONFLUENCE] log lines from today's log file."""
    lines: list[str] = []
    for log_file in sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            with open(log_file) as f:
                for line in f:
                    if "[CONFLUENCE]" in line:
                        lines.append(line.rstrip())
        except OSError:
            continue
        if lines:
            break  # Use first (most recent) log file that has confluence lines
    return lines


def build_audit(
    target_date: date,
    log_dir: Path | None = None,
    actual_pnl: float = 0.0,
) -> ConfluenceAudit:
    """Build confluence audit for the target date.

    Compares rule-only decisions vs confluence-adjusted decisions.
    """
    decisions: list[DecisionRecord] = []

    if log_dir and log_dir.exists():
        log_lines = _read_confluence_lines(log_dir, target_date)
        decisions = parse_confluence_logs(log_lines)
        logger.info(
            "parsed confluence decisions",
            extra={"tag": Tag.ADVISOR, "phase": "audit", "decisions": len(decisions)},
        )

    # Count agreements and disagreements
    agree = 0
    disagree = 0
    ai_right = 0
    rule_right = 0

    for d in decisions:
        if d.ai_adj == 0 or d.ai_confidence < 0.7:
            continue  # AI had no opinion on this decision

        # Agreement: AI adjustment doesn't change the entry/skip decision
        threshold = 60  # Default threshold
        rule_would_enter = d.rule_score >= threshold
        confluence_would_enter = (d.rule_score + d.ai_adj) >= threshold

        if rule_would_enter == confluence_would_enter:
            agree += 1
        else:
            disagree += 1
            # Track who was right based on outcome P&L
            if d.outcome_pnl > 0:
                # Positive outcome: whoever suggested entering was right
                if confluence_would_enter:
                    ai_right += 1
                else:
                    rule_right += 1
            elif d.outcome_pnl < 0:
                # Negative outcome: whoever suggested skipping was right
                if not confluence_would_enter:
                    ai_right += 1
                else:
                    rule_right += 1

    # Counterfactual P&L (computed by comparing with-AI vs without-AI)
    # In shadow mode, actual_pnl IS rules-only P&L
    pnl_rules_only = actual_pnl
    pnl_with_ai = actual_pnl  # Same in shadow mode; differs when enabled
    ai_alpha = pnl_with_ai - pnl_rules_only

    audit = ConfluenceAudit(
        date=target_date,
        decisions=decisions,
        agree_count=agree,
        disagree_count=disagree,
        ai_right_count=ai_right,
        rule_right_count=rule_right,
        pnl_with_ai=pnl_with_ai,
        pnl_rules_only=pnl_rules_only,
        ai_alpha=ai_alpha,
    )

    logger.info(
        "shadow audit complete",
        extra={
            "tag": Tag.ADVISOR,
            "phase": "audit",
            "decisions": len(decisions),
            "agree_count": agree,
            "disagree_count": disagree,
            "ai_right_count": ai_right,
            "rule_right_count": rule_right,
            "ai_alpha": round(ai_alpha, 2),
        },
    )

    return audit
