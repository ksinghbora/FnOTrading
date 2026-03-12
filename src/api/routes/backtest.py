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
    param_overrides: dict | None = None  # Strategy-specific param overrides


class MultiSeedRequest(BaseModel):
    """Request body for multi-seed validation."""

    strategies: list[str] | None = None  # None = all 4
    underlying: str = "NIFTY"
    quantity_lots: int = 1
    num_days: int = 30
    num_seeds: int = 10
    initial_capital: float = 1_000_000
    param_overrides: dict | None = None  # Per-strategy overrides: {"iron_condor": {"stop_loss_pct": 60}}


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
    """Run a backtest with the specified parameters."""
    try:
        from src.backtest.engine import BacktestEngine

        engine = BacktestEngine()

        start = None
        if request.start_date:
            start = date.fromisoformat(request.start_date)

        params = {
            "underlying": request.underlying,
            "quantity_lots": request.quantity_lots,
        }
        if request.param_overrides:
            params.update(request.param_overrides)

        results = await engine.run(
            strategy_name=request.strategy,
            strategy_params=params,
            num_days=request.num_days,
            start_date=start,
            initial_capital=request.initial_capital,
            seed=request.seed,
            tick_interval_minutes=request.tick_interval_minutes,
        )

        if "error" in results:
            return {"status": "error", "message": results["error"]}

        with open(RESULTS_FILE, "w") as f:
            json.dump(results, f, indent=2, default=str)

        return {"status": "ok", "results": results}

    except Exception as e:
        logger.exception(f"Backtest failed: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/multi-seed", dependencies=[Depends(verify_api_key)])
async def run_multi_seed(request: MultiSeedRequest) -> dict:
    """Run multi-seed validation across strategies.

    Returns per-strategy summary with mean P&L, win rate, best/worst, StdDev,
    plus per-seed detailed results with equity curves.
    """
    try:
        from src.backtest.engine import BacktestEngine, _import_strategies

        _import_strategies()
        from src.strategy.registry import list_strategies as _list

        engine = BacktestEngine()
        strategies = request.strategies or [
            s for s in _list()
            if s in ("short_straddle", "short_strangle", "iron_condor", "delta_neutral")
        ]

        all_results: dict[str, dict] = {}

        for strat in strategies:
            seed_results = []
            params = {
                "underlying": request.underlying,
                "quantity_lots": request.quantity_lots,
            }
            if request.param_overrides and strat in request.param_overrides:
                params.update(request.param_overrides[strat])

            for seed in range(1, request.num_seeds + 1):
                r = await engine.run(
                    strategy_name=strat,
                    strategy_params=params,
                    num_days=request.num_days,
                    seed=seed,
                    initial_capital=request.initial_capital,
                )
                seed_results.append({
                    "seed": seed,
                    "pnl": r["final_pnl"],
                    "metrics": r["metrics"],
                    "equity_curve": r.get("equity_curve", []),
                    "daily_results": r.get("daily_results", []),
                })

            # Compute summary stats
            pnls = [sr["pnl"] for sr in seed_results]
            mean_pnl = sum(pnls) / len(pnls)
            wins = sum(1 for p in pnls if p > 0)
            std = (sum((p - mean_pnl) ** 2 for p in pnls) / len(pnls)) ** 0.5

            all_results[strat] = {
                "summary": {
                    "mean_pnl": round(mean_pnl, 2),
                    "win_count": wins,
                    "total_seeds": len(pnls),
                    "win_rate_pct": round(wins / len(pnls) * 100, 1),
                    "best_pnl": round(max(pnls), 2),
                    "worst_pnl": round(min(pnls), 2),
                    "std_dev": round(std, 2),
                },
                "seeds": seed_results,
            }

        return {"status": "ok", "results": all_results}

    except Exception as e:
        logger.exception(f"Multi-seed backtest failed: {e}")
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
