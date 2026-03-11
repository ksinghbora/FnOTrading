"""Backtest route — serve backtest results and trigger new runs."""

import json
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, Depends

from src.api.routes.auth import verify_api_key

router = APIRouter(prefix="/api/backtest", tags=["backtest"])

RESULTS_FILE = Path(__file__).parent.parent.parent.parent / "backtest_results.json"


@router.get("/results", dependencies=[Depends(verify_api_key)])
async def get_backtest_results() -> dict:
    """Return the latest backtest results."""
    if not RESULTS_FILE.exists():
        return {"status": "no_results", "message": "No backtest has been run yet. Click 'Run Backtest' to start."}

    with open(RESULTS_FILE) as f:
        results = json.load(f)

    return {"status": "ok", "results": results}


@router.post("/run", dependencies=[Depends(verify_api_key)])
async def run_backtest() -> dict:
    """Trigger a new backtest run."""
    script = Path(__file__).parent.parent.parent.parent / "scripts" / "run_backtest.py"
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(script.parent.parent),
        )
        if result.returncode != 0:
            return {"status": "error", "message": result.stderr[-500:] if result.stderr else "Unknown error"}

        return {"status": "ok", "message": "Backtest completed successfully"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "Backtest timed out after 120 seconds"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
