"""Execution quality routes — daily metrics from structured JSONL logs."""

import json
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from src.api.deps import get_risk_manager
from src.api.routes.auth import verify_api_key
from src.core.structured_logger import STRUCTURED_LOG_DIR
from src.risk.manager import RiskManager

router = APIRouter(prefix="/api/execution", tags=["execution"])


def _read_events(target_date: str, tag: str | None = None) -> list[dict]:
    """Read events from a JSONL file, optionally filtering by tag."""
    path = STRUCTURED_LOG_DIR / f"{target_date}.jsonl"
    if not path.exists():
        return []
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if tag is None or record.get("tag") == tag:
                    events.append(record)
            except json.JSONDecodeError:
                continue
    return events


@router.get("/quality", dependencies=[Depends(verify_api_key)])
async def get_execution_quality(
    target_date: str | None = Query(None, description="Date YYYY-MM-DD (default: today)"),
) -> dict:
    """Execution quality metrics: fill rate, slippage, latency."""
    d = target_date or date.today().isoformat()

    submits = _read_events(d, "SUBMIT")
    fills = _read_events(d, "FILL")

    if not submits and not fills:
        return {"status": "no_data", "date": d}

    # Latency
    submit_times = [e.get("submit_ms", 0) for e in submits if e.get("submit_ms")]
    avg_submit_ms = round(sum(submit_times) / len(submit_times), 1) if submit_times else 0
    max_submit_ms = round(max(submit_times), 1) if submit_times else 0

    # Fill metrics
    ttf_times = [e.get("ttf_ms", 0) for e in fills if e.get("ttf_ms")]
    avg_ttf_ms = round(sum(ttf_times) / len(ttf_times), 1) if ttf_times else 0

    # Slippage
    slippages = [e.get("slippage", 0) for e in fills if "slippage" in e]
    slippage_pcts = [e.get("slippage_pct", 0) for e in fills if "slippage_pct" in e]
    avg_slippage = round(sum(slippages) / len(slippages), 2) if slippages else 0
    avg_slippage_pct = round(sum(slippage_pcts) / len(slippage_pcts), 4) if slippage_pcts else 0

    # Partial fills
    partial_count = sum(1 for e in fills if e.get("partial"))

    return {
        "status": "ok",
        "date": d,
        "orders": {
            "submitted": len(submits),
            "filled": len(fills),
            "fill_rate_pct": round(len(fills) / len(submits) * 100, 1) if submits else 0,
            "partial_fills": partial_count,
        },
        "latency": {
            "avg_submit_ms": avg_submit_ms,
            "max_submit_ms": max_submit_ms,
            "avg_time_to_fill_ms": avg_ttf_ms,
        },
        "slippage": {
            "avg_absolute": avg_slippage,
            "avg_pct": avg_slippage_pct,
        },
    }


@router.get("/events", dependencies=[Depends(verify_api_key)])
async def get_events(
    target_date: str | None = Query(None, description="Date YYYY-MM-DD (default: today)"),
    tag: str | None = Query(None, description="Filter by tag: ENTRY, EXIT, FILL, MONITOR, etc."),
    limit: int = Query(200, ge=1, le=2000),
) -> dict:
    """Return structured events for a given date, optionally filtered by tag."""
    d = target_date or date.today().isoformat()
    events = _read_events(d, tag)

    return {
        "status": "ok",
        "date": d,
        "tag_filter": tag,
        "count": min(len(events), limit),
        "total": len(events),
        "events": events[-limit:],  # Most recent first
    }


@router.get("/risk-snapshot", dependencies=[Depends(verify_api_key)])
async def get_risk_snapshot(
    risk: RiskManager = Depends(get_risk_manager),
) -> dict:
    """Current risk limit utilization snapshot."""
    return risk.get_risk_snapshot()


@router.get("/dates", dependencies=[Depends(verify_api_key)])
async def list_event_dates() -> dict:
    """List all dates with structured event data."""
    if not STRUCTURED_LOG_DIR.exists():
        return {"dates": []}
    files = sorted(STRUCTURED_LOG_DIR.glob("*.jsonl"))
    dates = [f.stem for f in files]
    return {"dates": dates, "count": len(dates)}
