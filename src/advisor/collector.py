"""Collect today's trading data from logs and database for advisor analysis.

Reads structured logs in two formats:

1. **JSONL (preferred, post-Apr-17)** — ``data/logs/trader.jsonl`` produced
   by ``src.utils.logging.JsonFormatter``. Records carry a typed ``tag``
   field (``ATTRIBUTION``, ``DAY_SUMMARY``, ``ENTRY_QUALITY``, ``MONITOR``)
   and well-named numeric fields. Lookup is one ``dict.get`` — no regex.

2. **Legacy text logs (.log)** — ``logs/trader-YYYYMMDD.log`` with
   ``[ATTRIBUTION] strategy=... delta=...`` substrings. Kept as a
   fallback for replaying historical days that predate the JSONL sink.
   Drop this path once 30 days of JSONL exist.

Selection rule: if a JSONL file exists for the target date (matched by
``ts`` prefix), use it; otherwise fall back to the legacy text parser.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

from src.advisor.models import (
    DayResult,
    LegAttribution,
    PremiumLegData,
    TodayData,
    TrendLegData,
)
from src.config import Settings
from src.utils.log_tags import Tag

logger = logging.getLogger(__name__)


# Default location of the live JSONL log (DATA_RELIABILITY_PLAN §8.2).
# Made a constant so tests can override via ``jsonl_path=`` instead of
# trampling the global, and so we have ONE place to update if we ever
# move the directory.
DEFAULT_JSONL_PATH = Path("data/logs/trader.jsonl")


# ── JSONL-based parsing (preferred) ─────────────────────────────────


def _iter_jsonl(path: Path, target_date: date) -> list[dict[str, Any]]:
    """Yield records from a JSONL file whose ``ts`` falls on ``target_date``.

    Filtering by date here (rather than reading the whole file) keeps
    memory bounded — we only carry today's records, not the rolling tail
    of every previous day. Matches against the ISO ``ts`` prefix so we
    don't have to parse the timestamp.
    """
    prefix = target_date.isoformat()  # "2026-04-17"
    out: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw or not raw.startswith("{"):
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    # A malformed line shouldn't kill the parse — log once and skip.
                    continue
                ts = rec.get("ts", "")
                if isinstance(ts, str) and ts.startswith(prefix):
                    out.append(rec)
    except OSError as e:
        logger.warning(
            "could not read JSONL log",
            extra={"tag": Tag.ADVISOR, "phase": "collect", "path": str(path), "error": str(e)},
        )
    return out


def _records_with_tag(records: list[dict[str, Any]], tag: Tag) -> list[dict[str, Any]]:
    """Filter records to those carrying the given tag."""
    name = tag.value
    return [r for r in records if r.get("tag") == name]


def _attributions_from_jsonl(records: list[dict[str, Any]]) -> list[LegAttribution]:
    """Build LegAttribution objects from ATTRIBUTION JSONL records.

    Field names match the emitter in
    ``portfolio_strategy._log_exit_attribution`` — ``delta_pnl``,
    ``gamma_pnl``, etc. (the ``_pnl`` suffix is intentional — they're
    the dollar P&L attributed to that Greek, not the Greek itself).
    """
    out: list[LegAttribution] = []
    for r in records:
        try:
            out.append(LegAttribution(
                delta_pnl=float(r.get("delta_pnl", 0)),
                gamma_pnl=float(r.get("gamma_pnl", 0)),
                theta_pnl=float(r.get("theta_pnl", 0)),
                vega_pnl=float(r.get("vega_pnl", 0)),
                residual=float(r.get("residual", 0)),
                dominant=str(r.get("dominant", "")),
            ))
        except (ValueError, TypeError):
            continue
    return out


def _day_summary_from_jsonl(records: list[dict[str, Any]]) -> tuple[float, float, int, int]:
    """Pull the most recent DAY_SUMMARY record. Returns (prem, trend, n_p, n_t)."""
    summaries = _records_with_tag(records, Tag.DAY_SUMMARY)
    if not summaries:
        return 0.0, 0.0, 0, 0
    # Last one wins — reset_day_state may emit multiple if the day rolled over
    last = summaries[-1]
    try:
        return (
            float(last.get("premium_pnl", 0)),
            float(last.get("trend_pnl", 0)),
            int(last.get("premium_trades", 0)),
            int(last.get("trend_trades", 0)),
        )
    except (ValueError, TypeError):
        return 0.0, 0.0, 0, 0


def _entries_from_jsonl(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pull ENTRY_QUALITY records into the loose dict shape the legacy code expects."""
    out: list[dict[str, Any]] = []
    for r in _records_with_tag(records, Tag.ENTRY_QUALITY):
        out.append({
            "leg": r.get("leg", ""),
            "mode": r.get("mode", ""),
            "direction": r.get("direction", ""),
            "net_delta": float(r.get("net_delta", 0)),
            "net_gamma": float(r.get("net_gamma", 0)),
            "net_theta": float(r.get("net_theta", 0)),
            "net_vega": float(r.get("net_vega", 0)),
            "spot": float(r.get("spot", 0)),
            "VIX": float(r.get("vix", 0)),
        })
    return out


# ── Legacy text-log parsing (fallback) ──────────────────────────────


def _parse_kv(line: str) -> dict[str, str]:
    """Parse key=value pairs from a structured log line."""
    result: dict[str, str] = {}
    for m in re.finditer(r'(\w+)=([^\s,]+)', line):
        result[m.group(1)] = m.group(2)
    return result


def parse_attribution_lines(lines: list[str]) -> list[LegAttribution]:
    """Parse ``[ATTRIBUTION]`` log lines into LegAttribution objects (legacy).

    Kept for replaying historical pre-Apr-17 log files. New code paths go
    through ``_attributions_from_jsonl`` via the JSONL records.
    """
    attributions: list[LegAttribution] = []
    for line in lines:
        if "[ATTRIBUTION]" not in line:
            continue
        kv = _parse_kv(line)
        try:
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
    """Parse ``[DAY_SUMMARY]`` log line. Returns (prem, trend, n_p, n_t) (legacy)."""
    for line in reversed(lines):
        if "[DAY_SUMMARY]" not in line:
            continue
        try:
            prem_match = re.search(r'premium=([+\-]?[\d,]+)', line)
            trend_match = re.search(r'trend=([+\-]?[\d,]+)', line)
            prem_pnl = float(prem_match.group(1).replace(",", "")) if prem_match else 0.0
            trend_pnl = float(trend_match.group(1).replace(",", "")) if trend_match else 0.0

            prem_trades_match = re.search(r'premium=.*?trades=(\d+)', line)
            trend_trades_match = re.search(r'trend=.*?trades=(\d+)', line)
            prem_trades = int(prem_trades_match.group(1)) if prem_trades_match else 0
            trend_trades = int(trend_trades_match.group(1)) if trend_trades_match else 0

            return prem_pnl, trend_pnl, prem_trades, trend_trades
        except (ValueError, AttributeError):
            continue
    return 0.0, 0.0, 0, 0


def parse_entry_quality(lines: list[str]) -> list[dict]:
    """Parse ``[ENTRY_QUALITY]`` log lines (legacy)."""
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
    """Parse ``[MONITOR]`` log lines (legacy)."""
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


# ── File discovery ─────────────────────────────────────────────────


def _find_log_file(log_dir: Path, target_date: date) -> Path | None:
    """Find legacy .log file for the given date."""
    patterns = [
        f"*{target_date.strftime('%Y%m%d')}*",
        f"*{target_date.strftime('%m%d')}*",
        f"*{target_date.isoformat()}*",
    ]
    for pattern in patterns:
        matches = list(log_dir.glob(pattern))
        if matches:
            return matches[0]

    logs = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if logs:
        return logs[0]
    return None


def _read_log_lines(log_path: Path) -> list[str]:
    """Read relevant structured log lines from a legacy text log."""
    tags = ("[ATTRIBUTION]", "[DAY_SUMMARY]", "[ENTRY_QUALITY]", "[MONITOR]")
    lines: list[str] = []
    try:
        with open(log_path) as f:
            for line in f:
                if any(tag in line for tag in tags):
                    lines.append(line.rstrip())
    except OSError as e:
        logger.warning(
            "could not read legacy log file",
            extra={"tag": Tag.ADVISOR, "phase": "collect", "path": str(log_path), "error": str(e)},
        )
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


# ── Main collector ─────────────────────────────────────────────────


async def collect_today_data(
    target_date: date,
    log_dir: Path | None = None,
    settings: Settings | None = None,
    jsonl_path: Path | None = None,
) -> TodayData:
    """Gather all internal trading data for the target date.

    Parameters:
        target_date: The date whose data we want.
        log_dir: Legacy ``logs/`` directory containing ``trader-YYYYMMDD.log``
            files. Used as a fallback if the JSONL file has no records for
            the target date (so historical replay still works).
        settings: Settings instance (loaded if not provided) — needed for
            ``current_params`` lookup.
        jsonl_path: Override JSONL path. Defaults to ``DEFAULT_JSONL_PATH``.
            Tests pass a tmp_path; production code should leave this as None.

    The JSONL reader uses field-level lookups (``rec.get("delta_pnl", 0)``)
    and is the preferred path. The legacy regex parser is the fallback.
    """
    settings = settings or Settings()

    # ── 1. Try JSONL first ─────────────────────────────────────
    jsonl_path = jsonl_path or DEFAULT_JSONL_PATH
    jsonl_records: list[dict[str, Any]] = []
    if jsonl_path.exists():
        jsonl_records = _iter_jsonl(jsonl_path, target_date)
        if jsonl_records:
            logger.info(
                "loaded JSONL records for advisor",
                extra={
                    "tag": Tag.ADVISOR,
                    "phase": "collect",
                    "source": "jsonl",
                    "path": str(jsonl_path),
                    "records": len(jsonl_records),
                    "date": target_date.isoformat(),
                },
            )

    # ── 2. Fall back to legacy text log if JSONL empty ─────────
    log_lines: list[str] = []
    if not jsonl_records and log_dir and log_dir.exists():
        log_file = _find_log_file(log_dir, target_date)
        if log_file:
            log_lines = _read_log_lines(log_file)
            logger.info(
                "loaded legacy log lines for advisor",
                extra={
                    "tag": Tag.ADVISOR,
                    "phase": "collect",
                    "source": "legacy_log",
                    "path": str(log_file),
                    "lines": len(log_lines),
                },
            )
        else:
            logger.warning(
                "no log file found for target date",
                extra={"tag": Tag.ADVISOR, "phase": "collect", "log_dir": str(log_dir), "date": target_date.isoformat()},
            )
    elif not jsonl_records:
        logger.info(
            "no log data available for advisor",
            extra={"tag": Tag.ADVISOR, "phase": "collect", "date": target_date.isoformat()},
        )

    # ── 3. Parse attributions, summary, entries ────────────────
    if jsonl_records:
        # Split attribution by leg using the structured ``leg`` field.
        attr_records = _records_with_tag(jsonl_records, Tag.ATTRIBUTION)
        prem_attr_records = [r for r in attr_records if r.get("leg") == "PREMIUM"]
        trend_attr_records = [r for r in attr_records if r.get("leg") == "TREND"]
        prem_attrs = _attributions_from_jsonl(prem_attr_records)
        trend_attrs = _attributions_from_jsonl(trend_attr_records)

        prem_pnl, trend_pnl, prem_trades, trend_trades = _day_summary_from_jsonl(jsonl_records)
        entries = _entries_from_jsonl(jsonl_records)
    else:
        # Legacy code path — string-based filter on the rendered message.
        prem_attr_lines = [l for l in log_lines if "[ATTRIBUTION]" in l and "leg=PREMIUM" in l]
        trend_attr_lines = [l for l in log_lines if "[ATTRIBUTION]" in l and "leg=TREND" in l]
        prem_attrs = parse_attribution_lines(prem_attr_lines)
        trend_attrs = parse_attribution_lines(trend_attr_lines)

        prem_pnl, trend_pnl, prem_trades, trend_trades = parse_day_summary(log_lines)
        entries = parse_entry_quality(log_lines)

    total_pnl = prem_pnl + trend_pnl

    # ── 4. Derive mode + direction from entry records ─────────
    prem_mode = "idle"
    prem_score = 0
    trend_dir = ""
    trend_score = 0
    for e in entries:
        if e["leg"] == "PREMIUM":
            prem_mode = (e.get("mode") or "").lower()
        elif e["leg"] == "TREND":
            trend_dir = e.get("direction") or e.get("mode") or ""

    # ── 5. Build leg data ──────────────────────────────────────
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

    current_params = _load_current_params(settings)

    return TodayData(
        date=target_date,
        total_pnl=total_pnl,
        premium_leg=premium_leg,
        trend_leg=trend_leg,
        trades_count=prem_trades + trend_trades,
        current_params=current_params,
    )
