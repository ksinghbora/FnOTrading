"""Dashboard route — overall system summary."""

from decimal import Decimal

from fastapi import APIRouter, Depends

from src.api.deps import get_portfolio_manager, get_strategy_runner
from src.api.routes.auth import verify_api_key
from src.core.types import StrategyState
from src.portfolio.manager import PortfolioManager
from src.strategy.runner import StrategyRunner

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/regime", dependencies=[Depends(verify_api_key)])
async def get_market_regime(
    runner: StrategyRunner = Depends(get_strategy_runner),
) -> dict:
    """Get current market regime assessment for all active underlyings."""
    from src.strategy.regime import RegimeDetector

    feed = runner._feed
    aggregator = runner._aggregator
    chain_builder = runner._chain_builder

    detector = RegimeDetector(feed, aggregator, chain_builder)
    regimes = {}

    for underlying in ("NIFTY", "BANKNIFTY"):
        try:
            snapshot = detector.assess(underlying)
            regimes[underlying] = {
                "regime": snapshot.regime.value,
                "vix": round(snapshot.vix, 1),
                "morning_range_pct": round(snapshot.morning_range_pct, 2),
                "move_from_open_pct": round(snapshot.move_from_open_pct, 2),
                "session_open": round(snapshot.session_open, 1),
                "current_spot": round(snapshot.current_spot, 1),
                "recommended_strategy": snapshot.recommended_strategy,
                "lots_multiplier": snapshot.recommended_lots_multiplier,
                "reason": snapshot.reason,
            }
        except Exception as e:
            regimes[underlying] = {"error": str(e)}

    return {"regimes": regimes}


@router.get("/summary", dependencies=[Depends(verify_api_key)])
async def get_dashboard_summary(
    portfolio: PortfolioManager = Depends(get_portfolio_manager),
    runner: StrategyRunner = Depends(get_strategy_runner),
) -> dict:
    """Overall P&L, active strategies count, and positions count."""
    pnl = portfolio.get_pnl()
    open_positions = portfolio.get_open_positions()
    strategies = runner.get_all_strategies()

    active_count = sum(
        1 for s in strategies.values() if s.state == StrategyState.RUNNING
    )

    return {
        "pnl": {
            "gross": str(pnl.gross),
            "realized": str(pnl.realized),
            "unrealized": str(pnl.unrealized),
            "charges": str(pnl.charges),
            "net": str(pnl.net),
            "day_pnl": str(portfolio.get_day_pnl()),
        },
        "strategies": {
            "total": len(strategies),
            "active": active_count,
        },
        "positions": {
            "open": len(open_positions),
        },
    }
