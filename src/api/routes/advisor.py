"""Advisor routes — view advisories and confluence audit scorecard."""

from fastapi import APIRouter, Depends

from src.advisor.store import load_advisory_json, load_audit_json
from src.api.routes.auth import verify_api_key

router = APIRouter(prefix="/api/advisor", tags=["advisor"])


@router.get("/latest", dependencies=[Depends(verify_api_key)])
async def get_latest_advisory() -> dict:
    """Return the most recent nightly advisory."""
    advisory = load_advisory_json()
    if not advisory:
        return {"status": "no_advisory", "message": "No advisory generated yet."}
    return {
        "status": "ok",
        "advisory": advisory.model_dump(mode="json"),
    }


@router.get("/audit", dependencies=[Depends(verify_api_key)])
async def get_latest_audit() -> dict:
    """Return the most recent confluence audit."""
    audit = load_audit_json()
    if not audit:
        return {"status": "no_audit", "message": "No audit data yet."}
    return {
        "status": "ok",
        "audit": audit.model_dump(mode="json"),
    }


@router.get("/day-bias", dependencies=[Depends(verify_api_key)])
async def get_current_day_bias() -> dict:
    """Return the current day's AI bias signal."""
    from src.advisor.confluence import load_day_bias
    bias = load_day_bias()
    if not bias:
        return {"status": "no_bias", "message": "No day bias loaded."}
    return {
        "status": "ok",
        "day_bias": bias.model_dump(mode="json"),
    }
