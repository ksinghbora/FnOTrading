"""Risk routes — risk status and kill switch control."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.api.deps import get_risk_manager
from src.api.routes.auth import verify_api_key
from src.risk.manager import RiskManager

router = APIRouter(prefix="/api/risk", tags=["risk"])


class KillSwitchRequest(BaseModel):
    reason: str = "Manual activation via API"


@router.get("/status", dependencies=[Depends(verify_api_key)])
async def get_risk_status(
    risk: RiskManager = Depends(get_risk_manager),
) -> dict:
    """Return current risk system status."""
    cb = risk.circuit_breaker
    ks = risk.kill_switch

    return {
        "kill_switch": {
            "activated": ks.is_activated,
        },
        "circuit_breaker": {
            "state": cb.state.value,
            "is_active": cb.is_active,
            "trigger_reason": cb.trigger_reason,
        },
        "limits": {
            "max_day_loss": str(risk.limits.max_day_loss),
            "max_strategy_loss": str(risk.limits.max_strategy_loss),
            "max_total_lots": risk.limits.max_total_lots,
            "max_open_orders": risk.limits.max_open_orders,
        },
    }


@router.post("/kill-switch", dependencies=[Depends(verify_api_key)])
async def activate_kill_switch(
    body: KillSwitchRequest,
    risk: RiskManager = Depends(get_risk_manager),
) -> dict:
    """Activate the kill switch — cancels all orders and closes all positions."""
    results = await risk.kill_switch.activate(reason=body.reason)
    return {
        "activated": True,
        "reason": body.reason,
        "results": results,
    }
