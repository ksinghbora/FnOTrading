"""Decision snapshot routes — query ML training data."""

import csv
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from src.api.routes.auth import verify_api_key
from src.strategy.decision_logger import DECISIONS_DIR

router = APIRouter(prefix="/api/decisions", tags=["decisions"])


@router.get("/", dependencies=[Depends(verify_api_key)])
async def get_decisions(
    target_date: str | None = Query(None, description="Date YYYY-MM-DD (default: today)"),
    leg: str | None = Query(None, description="Filter by leg: PREMIUM or TREND"),
    decision: str | None = Query(None, description="Filter: ENTER, EXIT, or SKIP"),
    limit: int = Query(100, ge=1, le=1000),
) -> dict:
    """Return decision snapshots for a given date."""
    d = target_date or date.today().isoformat()
    path = DECISIONS_DIR / f"decisions_{d}.csv"

    if not path.exists():
        return {"status": "no_data", "date": d, "rows": []}

    rows = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if leg and row.get("leg") != leg:
                continue
            if decision and row.get("decision") != decision:
                continue
            rows.append(row)
            if len(rows) >= limit:
                break

    return {"status": "ok", "date": d, "count": len(rows), "rows": rows}


@router.get("/dates", dependencies=[Depends(verify_api_key)])
async def list_decision_dates() -> dict:
    """List all dates with decision data."""
    if not DECISIONS_DIR.exists():
        return {"dates": []}
    files = sorted(DECISIONS_DIR.glob("decisions_*.csv"))
    dates = [f.stem.replace("decisions_", "") for f in files]
    return {"dates": dates, "count": len(dates)}


@router.get("/stats", dependencies=[Depends(verify_api_key)])
async def get_decision_stats(
    target_date: str | None = Query(None, description="Date YYYY-MM-DD (default: today)"),
) -> dict:
    """Summary stats for a given date's decisions."""
    d = target_date or date.today().isoformat()
    path = DECISIONS_DIR / f"decisions_{d}.csv"

    if not path.exists():
        return {"status": "no_data", "date": d}

    enters = exits = skips = 0
    total_pnl = 0.0
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            dec = row.get("decision", "")
            if dec == "ENTER":
                enters += 1
            elif dec == "EXIT":
                exits += 1
                pnl = row.get("outcome_pnl", "")
                if pnl:
                    total_pnl += float(pnl)
            elif dec == "SKIP":
                skips += 1

    return {
        "status": "ok",
        "date": d,
        "enters": enters,
        "exits": exits,
        "skips": skips,
        "total_pnl": round(total_pnl, 2),
    }
