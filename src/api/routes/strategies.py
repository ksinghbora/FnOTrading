"""Strategy routes — list, start, and stop strategies."""

from fastapi import APIRouter, Depends, HTTPException, status

from src.api.deps import get_strategy_runner
from src.api.routes.auth import verify_api_key
from src.strategy.runner import StrategyRunner
from src.strategy.registry import create_strategy, list_strategies as list_registered

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


@router.get("", dependencies=[Depends(verify_api_key)])
async def list_strategies(
    runner: StrategyRunner = Depends(get_strategy_runner),
) -> dict:
    """List all running strategies with their status."""
    strategies = runner.get_all_strategies()
    items = []
    for sid, strategy in strategies.items():
        items.append({
            "strategy_id": sid,
            "state": strategy.state.value,
            "params": strategy.params.model_dump(mode="json"),
            "state_data": strategy.get_state_data(),
        })
    return {
        "strategies": items,
        "registered_types": list_registered(),
    }


@router.get("/{strategy_id}", dependencies=[Depends(verify_api_key)])
async def get_strategy_detail(
    strategy_id: str,
    runner: StrategyRunner = Depends(get_strategy_runner),
) -> dict:
    """Get detailed state of a specific strategy."""
    strategy = runner.get_strategy(strategy_id)
    if not strategy:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Strategy '{strategy_id}' not found.",
        )
    tokens = runner._strategy_tokens.get(strategy_id, set())
    return {
        "strategy_id": strategy_id,
        "state": strategy.state.value,
        "params": strategy.params.model_dump(mode="json"),
        "state_data": strategy.get_state_data(),
        "subscribed_tokens": sorted(tokens),
    }


@router.get("/{strategy_id}/diagnostics", dependencies=[Depends(verify_api_key)])
async def get_strategy_diagnostics(
    strategy_id: str,
    runner: StrategyRunner = Depends(get_strategy_runner),
) -> dict:
    """Get live diagnostics for a portfolio strategy (per-leg state, Greeks, P&L)."""
    strategy = runner.get_strategy(strategy_id)
    if not strategy:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Strategy '{strategy_id}' not found.",
        )
    if not hasattr(strategy, "get_diagnostics"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Strategy '{strategy_id}' does not support diagnostics.",
        )
    return strategy.get_diagnostics()


@router.post("/{strategy_id}/start", dependencies=[Depends(verify_api_key)])
async def start_strategy(
    strategy_id: str,
    name: str,
    params: dict | None = None,
    runner: StrategyRunner = Depends(get_strategy_runner),
) -> dict:
    """Start a strategy by registered name and instance ID.

    Query params:
        name: Registered strategy name (e.g., 'short_straddle').
        params: Optional strategy parameters as JSON body.
    """
    # Check if already running
    existing = runner.get_strategy(strategy_id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Strategy '{strategy_id}' is already running.",
        )

    try:
        strategy = create_strategy(name=name, strategy_id=strategy_id, params=params)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    await runner.add_strategy(strategy)
    return {
        "strategy_id": strategy_id,
        "state": strategy.state.value,
        "message": f"Strategy '{strategy_id}' started.",
    }


@router.post("/{strategy_id}/stop", dependencies=[Depends(verify_api_key)])
async def stop_strategy(
    strategy_id: str,
    runner: StrategyRunner = Depends(get_strategy_runner),
) -> dict:
    """Stop a running strategy."""
    strategy = runner.get_strategy(strategy_id)
    if not strategy:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Strategy '{strategy_id}' not found.",
        )

    await runner.remove_strategy(strategy_id)
    return {
        "strategy_id": strategy_id,
        "state": "STOPPED",
        "message": f"Strategy '{strategy_id}' stopped.",
    }
