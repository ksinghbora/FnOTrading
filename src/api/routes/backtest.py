"""Backtest route — run parameterized backtests and serve results."""

import json
import logging
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.api.routes.auth import verify_api_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/backtest", tags=["backtest"])

RESULTS_FILE = Path(__file__).parent.parent.parent.parent / "backtest_results.json"


class BacktestRequest(BaseModel):
    """Request body for running a backtest."""

    strategy: str = "short_straddle"
    underlying: str = "NIFTY"
    quantity_lots: int = 1
    num_days: int = 30
    initial_capital: float = 1_000_000
    seed: int = 42
    tick_interval_minutes: int = 1
    start_date: str | None = None  # YYYY-MM-DD


@router.get("/results", dependencies=[Depends(verify_api_key)])
async def get_backtest_results() -> dict:
    """Return the latest backtest results."""
    if not RESULTS_FILE.exists():
        return {
            "status": "no_results",
            "message": "No backtest has been run yet. Use POST /api/backtest/run to start.",
        }

    with open(RESULTS_FILE) as f:
        results = json.load(f)

    return {"status": "ok", "results": results}


@router.post("/run", dependencies=[Depends(verify_api_key)])
async def run_backtest(request: BacktestRequest) -> dict:
    """Run a backtest with the specified parameters.

    Runs synchronously (blocks until complete). For 30-day backtests
    with 1-minute intervals, expect ~20-30 seconds.
    """
    try:
        from src.backtest.engine import BacktestEngine

        engine = BacktestEngine()

        start = None
        if request.start_date:
            start = date.fromisoformat(request.start_date)

        results = await engine.run(
            strategy_name=request.strategy,
            strategy_params={
                "underlying": request.underlying,
                "quantity_lots": request.quantity_lots,
            },
            num_days=request.num_days,
            start_date=start,
            initial_capital=request.initial_capital,
            seed=request.seed,
            tick_interval_minutes=request.tick_interval_minutes,
        )

        if "error" in results:
            return {"status": "error", "message": results["error"]}

        # Save results for GET endpoint
        with open(RESULTS_FILE, "w") as f:
            json.dump(results, f, indent=2, default=str)

        return {"status": "ok", "results": results}

    except Exception as e:
        logger.exception(f"Backtest failed: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/strategies", dependencies=[Depends(verify_api_key)])
async def list_available_strategies() -> dict:
    """List all strategies available for backtesting."""
    from src.backtest.engine import _import_strategies
    from src.strategy.registry import get_params_schema, list_strategies

    _import_strategies()
    strategies = list_strategies()
    return {
        "strategies": [
            {"name": s, "params_schema": get_params_schema(s)}
            for s in strategies
        ]
    }
