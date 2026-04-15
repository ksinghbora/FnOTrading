"""FastAPI application factory.

Creates and configures the FastAPI app with all routes,
CORS middleware, and WebSocket endpoints.
"""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.api.routes import advisor, backtest, dashboard, decisions, execution, orders, positions, risk, strategies
from src.api.ws import live_feed

STATIC_DIR = Path(__file__).parent / "static"

logger = logging.getLogger(__name__)


def create_app(is_production: bool = False) -> FastAPI:
    """Create and configure the FastAPI application.

    Includes all API routes and WebSocket endpoints.
    CORS is restricted in production, open in development.
    """
    app = FastAPI(
        title="FnO Trading System",
        description="API for the F&O Trading System — positions, orders, strategies, and risk management.",
        version="1.0.0",
    )

    # ─── CORS Middleware ────────────────────────────────────────────
    if is_production:
        allowed_origins = ["http://localhost:8000"]
    else:
        allowed_origins = ["*"]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["X-API-Key", "Content-Type"],
    )

    # ─── REST Routes ───────────────────────────────────────────────
    app.include_router(dashboard.router)
    app.include_router(strategies.router)
    app.include_router(orders.router)
    app.include_router(positions.router)
    app.include_router(risk.router)
    app.include_router(backtest.router)
    app.include_router(advisor.router)
    app.include_router(decisions.router)
    app.include_router(execution.router)

    # ─── WebSocket Routes ──────────────────────────────────────────
    app.include_router(live_feed.router)

    # ─── Health Check ──────────────────────────────────────────────
    @app.get("/health", tags=["system"])
    async def health_check() -> dict:
        return {"status": "ok"}

    # ─── Dashboard UI ────────────────────────────────────────────
    @app.get("/", tags=["system"])
    async def serve_dashboard():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    logger.info("FastAPI application created")
    return app


def create_api_app(app_ctx: dict) -> FastAPI:
    """Create the FastAPI app and wire up the dependency container.

    Called from main.py with the application context dict containing
    all system components.
    """
    from src.api.deps import init_container

    init_container(
        settings=app_ctx["settings"],
        event_bus=app_ctx["event_bus"],
        order_manager=app_ctx["order_manager"],
        portfolio_manager=app_ctx["portfolio"],
        risk_manager=app_ctx["risk_manager"],
        strategy_runner=app_ctx["strategy_runner"],
    )

    # Register WebSocket broadcast handlers
    live_feed.register_ws_handlers(app_ctx["event_bus"])

    return create_app(is_production=app_ctx["settings"].is_production)
