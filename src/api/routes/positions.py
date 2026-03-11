"""Position routes — current open positions."""

from fastapi import APIRouter, Depends, Query

from src.api.deps import get_portfolio_manager
from src.api.routes.auth import verify_api_key
from src.portfolio.manager import PortfolioManager

router = APIRouter(prefix="/api/positions", tags=["positions"])


@router.get("", dependencies=[Depends(verify_api_key)])
async def get_positions(
    strategy_id: str | None = Query(None, description="Filter by strategy ID"),
    portfolio: PortfolioManager = Depends(get_portfolio_manager),
) -> dict:
    """Return current open positions, optionally filtered by strategy."""
    positions = portfolio.get_open_positions(strategy_id=strategy_id)
    return {
        "positions": [pos.model_dump(mode="json") for pos in positions],
        "count": len(positions),
    }
