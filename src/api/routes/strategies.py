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
    """List all running strategies with their status.

    Returned fields per strategy:
      - strategy_id : runtime instance id (e.g. 'ic_2')
      - name        : canonical registered name (e.g. 'iron_condor')
                      — set by @register_strategy decorator, used by
                      the UI to label and color-code instances without
                      regex-parsing the id
      - state       : 'RUNNING' | 'STARTING' | 'STOPPING' | 'STOPPED'
      - shadow_only : whether the strategy is in shadow mode (logs
                      decisions but does not place real orders) — UI
                      surfaces this as a badge so the operator sees
                      at a glance which strategies are live vs paper
      - regime_family : 'premium_selling' | 'long_vol' |
                       'directional_trend' | 'unknown' — used by the
                       V5 orchestrator and helpful for grouping
      - calibration_module : dotted-path of the per-strategy calibration
                             module (V5 isolation contract) — empty
                             when the strategy hasn't declared one
      - params      : full Pydantic params dump
      - state_data  : strategy-specific runtime state
    """
    strategies = runner.get_all_strategies()
    items = []
    for sid, strategy in strategies.items():
        cal_module = getattr(type(strategy), "calibration", None)
        cal_name = cal_module.__name__ if cal_module is not None else ""
        items.append({
            "strategy_id": sid,
            "name": getattr(type(strategy), "registered_name", ""),
            "parent_id": None,
            "is_active": True,
            "state": strategy.state.value,
            "shadow_only": bool(getattr(strategy.params, "shadow_only", False)),
            "regime_family": getattr(type(strategy), "regime_family", "unknown"),
            "calibration_module": cal_name,
            "params": strategy.params.model_dump(mode="json"),
            "state_data": strategy.get_state_data(),
        })

        # V6 (May 8 2026): if this is an orchestrator with children,
        # expose each child as its own row so the UI can show what's
        # actually trading. Children carry composite ids like
        # ``orchestrator_1/iron_condor`` that PnL queries already roll
        # up under the parent id (commit e18451e).
        children = getattr(strategy, "_children", None)
        active_set = getattr(strategy, "_active_children", set())
        if children:
            for child_name, child in children.items():
                child_cal = getattr(type(child), "calibration", None)
                child_cal_name = child_cal.__name__ if child_cal is not None else ""
                items.append({
                    "strategy_id": child.strategy_id,
                    "name": getattr(type(child), "registered_name", ""),
                    "parent_id": sid,
                    "is_active": child_name in active_set,
                    "state": child.state.value,
                    "shadow_only": bool(getattr(strategy.params, "shadow_only", False)),
                    "regime_family": getattr(type(child), "regime_family", "unknown"),
                    "calibration_module": child_cal_name,
                    "params": child.params.model_dump(mode="json"),
                    "state_data": child.get_state_data(),
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
