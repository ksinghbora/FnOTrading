"""Collect today's trading data from logs and database for advisor analysis."""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path

from src.advisor.models import (
    DayResult,
    LegAttribution,
    PremiumLegData,
    TodayData,
    TrendLegData,
)
from src.config import Settings

logger = logging.getLogger(__name__)


# ── Log Line Parsers ─────────────────────────────────────────────

def _parse_kv(line: str) -> dict[str, str]:
    """Parse key=value pairs from a structured log line."""
    result: dict[str, str] = {}
    # Match key=value where value can be quoted or unquoted
    for m in re.finditer(r'(\w+)=([^\s,]+)', line):
        result[m.group(1)] = m.group(2)
    return result


def parse_attribution_lines(lines: list[str]) -> list[LegAttribution]:
    """Parse [ATTRIBUTION] log lines into LegAttribution objects."""
    attributions: list[LegAttribution] = []
    for line in lines:
        if "[ATTRIBUTION]" not in line:
            continue
        kv = _parse_kv(line)
        try:
            # Extract dominant factor and percentage
            dominant_match = re.search(r'dominant=(\w+)\((\d+)%\)', line)
            dominant = dominant_match.group(1) if dominant_match else ""

            attributions.append(LegAttribution(
                delta_pnl=float(kv.get("delta", "0").replace(",", "")),
                gamma_pnl=float(kv.get("gamma", "0").replace(",", "")),
                theta_pnl=float(kv.get("theta", "0").replace(",", "")),
                vega_pnl=float(kv.get("vega", "0").replace(",", "")),
                residual=float(kv.get("residual", "0").replace(",", "")),
                dominant=dominant,
            ))
        except (ValueError, KeyError):
            continue
    return attributions


def parse_day_summary(lines: list[str]) -> tuple[float, float, int, int]:
    """Parse [DAY_SUMMARY] log line. Returns (prem_pnl, trend_pnl, prem_trades, trend_trades)."""
    for line in reversed(lines):  # Take the last summary (most complete)
        if "[DAY_SUMMARY]" not in line:
            continue
        kv = _parse_kv(line)
        try:
            # Premium field has format: premium=+800(+64%) — extract the first number
            prem_match = re.search(r'premium=([+\-]?[\d,]+)', line)
            trend_match = re.search(r'trend=([+\-]?[\d,]+)', line)
            prem_pnl = float(prem_match.group(1).replace(",", "")) if prem_match else 0.0
            trend_pnl = float(trend_match.group(1).replace(",", "")) if trend_match else 0.0

            # Trade counts
            prem_trades_match = re.search(r'premium=.*?trades=(\d+)', line)
            trend_trades_match = re.search(r'trend=.*?trades=(\d+)', line)
            prem_trades = int(prem_trades_match.group(1)) if prem_trades_match else 0
            trend_trades = int(trend_trades_match.group(1)) if trend_trades_match else 0

            return prem_pnl, trend_pnl, prem_trades, trend_trades
        except (ValueError, AttributeError):
            continue
    return 0.0, 0.0, 0, 0


def parse_entry_quality(lines: list[str]) -> list[dict]:
    """Parse [ENTRY_QUALITY] log lines for entry condition snapshots."""
    entries: list[dict] = []
    for line in lines:
        if "[ENTRY_QUALITY]" not in line:
            continue
        kv = _parse_kv(line)
        entries.append({
            "leg": kv.get("leg", ""),
            "mode": kv.get("mode", ""),
            "net_delta": float(kv.get("net_delta", "0")),
            "net_gamma": float(kv.get("net_gamma", "0")),
            "net_theta": float(kv.get("net_theta", "0")),
            "net_vega": float(kv.get("net_vega", "0")),
            "spot": float(kv.get("spot", "0")),
            "VIX": float(kv.get("VIX", "0")),
        })
    return entries


def parse_monitor_lines(lines: list[str]) -> list[dict]:
    """Parse [MONITOR] log lines for position health snapshots."""
    monitors: list[dict] = []
    for line in lines:
        if "[MONITOR]" not in line:
            continue
        kv = _parse_kv(line)
        monitors.append({
            "leg": kv.get("leg", ""),
            "mode": kv.get("mode", kv.get("dir", "")),
            "pnl": float(kv.get("pnl", "0").replace(",", "")),
            "delta": float(kv.get("delta", "0")),
            "gamma": float(kv.get("gamma", "0")),
            "theta": float(kv.get("theta", "0")),
            "vega": float(kv.get("vega", "0")),
            "spot": float(kv.get("spot", "0")),
            "VIX": float(kv.get("VIX", "0")),
        })
    return monitors


# ── Main Collector ────────────────────────────────────────────────

def _find_log_file(log_dir: Path, target_date: date) -> Path | None:
    """Find log file for the given date. Tries common naming patterns."""
    patterns = [
        f"*{target_date.strftime('%Y%m%d')}*",
        f"*{target_date.strftime('%m%d')}*",
        f"*{target_date.isoformat()}*",
    ]
    for pattern in patterns:
        matches = list(log_dir.glob(pattern))
        if matches:
            return matches[0]

    # Fallback: try the most recent .log file
    logs = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if logs:
        return logs[0]
    return None


def _read_log_lines(log_path: Path) -> list[str]:
    """Read relevant structured log lines from file."""
    tags = ("[ATTRIBUTION]", "[DAY_SUMMARY]", "[ENTRY_QUALITY]", "[MONITOR]")
    lines: list[str] = []
    try:
        with open(log_path) as f:
            for line in f:
                if any(tag in line for tag in tags):
                    lines.append(line.rstrip())
    except OSError as e:
        logger.warning(f"[ADVISOR] Could not read log file {log_path}: {e}")
    return lines


def _load_current_params(settings: Settings) -> dict:
    """Load current PortfolioParams from STRATEGIES env var."""
    try:
        strats = json.loads(settings.strategies)
        for s in strats:
            if s.get("name") == "portfolio":
                return s.get("params", {})
    except (json.JSONDecodeError, TypeError):
        pass
    return {}


async def collect_today_data(
    target_date: date,
    log_dir: Path | None = None,
    settings: Settings | None = None,
) -> TodayData:
    """Gather all internal trading data for the target date.

    Collects from:
    1. Structured log files ([ATTRIBUTION], [DAY_SUMMARY], [ENTRY_QUALITY], [MONITOR])
    2. Current strategy parameters from config

    DB queries are handled separately when a session factory is available.
    """
    settings = settings or Settings()

    # Parse logs
    log_lines: list[str] = []
    if log_dir and log_dir.exists():
        log_file = _find_log_file(log_dir, target_date)
        if log_file:
            log_lines = _read_log_lines(log_file)
            logger.info(f"[ADVISOR] Parsed {len(log_lines)} structured log lines from {log_file}")
        else:
            logger.warning(f"[ADVISOR] No log file found for {target_date} in {log_dir}")
    else:
        logger.info("[ADVISOR] No log directory provided, using empty log data")

    # Parse attribution lines by leg
    all_attributions = parse_attribution_lines(log_lines)
    prem_attrs = [a for a in all_attributions]  # All attributions available
    trend_attrs: list[LegAttribution] = []

    # Split by leg from raw log lines
    prem_attr_lines = [l for l in log_lines if "[ATTRIBUTION]" in l and "leg=PREMIUM" in l]
    trend_attr_lines = [l for l in log_lines if "[ATTRIBUTION]" in l and "leg=TREND" in l]
    prem_attrs = parse_attribution_lines(prem_attr_lines)
    trend_attrs = parse_attribution_lines(trend_attr_lines)

    # Parse day summary
    prem_pnl, trend_pnl, prem_trades, trend_trades = parse_day_summary(log_lines)
    total_pnl = prem_pnl + trend_pnl

    # Parse entry quality for mode info
    entries = parse_entry_quality(log_lines)
    prem_mode = "idle"
    prem_score = 0
    trend_dir = ""
    trend_score = 0
    for e in entries:
        if e["leg"] == "PREMIUM":
            prem_mode = e["mode"].lower()
        elif e["leg"] == "TREND":
            trend_dir = e.get("dir", "")

    # Build leg data
    premium_leg = PremiumLegData(
        pnl=prem_pnl,
        mode=prem_mode,
        entry_score=prem_score,
        greeks_at_entry=entries[0] if entries and entries[0]["leg"] == "PREMIUM" else {},
        attributions=prem_attrs,
        trades=prem_trades,
    )

    trend_leg = TrendLegData(
        pnl=trend_pnl,
        direction=trend_dir,
        entry_score=trend_score,
        attributions=trend_attrs,
        trades=trend_trades,
    )

    # Load current params
    current_params = _load_current_params(settings)

    return TodayData(
        date=target_date,
        total_pnl=total_pnl,
        premium_leg=premium_leg,
        trend_leg=trend_leg,
        trades_count=prem_trades + trend_trades,
        current_params=current_params,
    )
