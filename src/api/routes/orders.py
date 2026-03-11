"""Order routes — order history listing."""

from fastapi import APIRouter, Depends, Query

from src.api.deps import get_order_manager
from src.api.routes.auth import verify_api_key
from src.oms.manager import OrderManager

router = APIRouter(prefix="/api/orders", tags=["orders"])


@router.get("", dependencies=[Depends(verify_api_key)])
async def get_orders(
    strategy_id: str | None = Query(None, description="Filter by strategy ID"),
    limit: int = Query(100, ge=1, le=1000, description="Max orders to return"),
    oms: OrderManager = Depends(get_order_manager),
) -> dict:
    """Return order history, optionally filtered by strategy."""
    orders = oms.get_order_history(strategy_id=strategy_id, limit=limit)
    return {
        "orders": [order.model_dump(mode="json") for order in orders],
        "count": len(orders),
    }
