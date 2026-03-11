"""WebSocket endpoint — pushes live P&L and position updates to connected clients."""

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from src.api.deps import get_container
from src.config import Settings
from src.core.events import Event, EventBus, EventType

logger = logging.getLogger(__name__)

router = APIRouter()

# Connected WebSocket clients
_clients: set[WebSocket] = set()


async def _broadcast(data: dict[str, Any]) -> None:
    """Send a JSON message to all connected WebSocket clients concurrently."""
    if not _clients:
        return
    message = json.dumps(data, default=str)

    async def _send(ws: WebSocket) -> WebSocket | None:
        try:
            await asyncio.wait_for(ws.send_text(message), timeout=3.0)
            return None
        except Exception:
            return ws

    results = await asyncio.gather(*[_send(ws) for ws in _clients])
    disconnected = {ws for ws in results if ws is not None}
    _clients.difference_update(disconnected)


async def _on_position_updated(event: Event) -> None:
    """Push position updates to WebSocket clients."""
    await _broadcast({
        "type": "position_update",
        "data": event.payload.get("position", {}),
        "timestamp": event.timestamp.isoformat(),
    })


async def _on_order_filled(event: Event) -> None:
    """Push trade execution to WebSocket clients."""
    await _broadcast({
        "type": "trade",
        "data": event.payload.get("order", {}),
        "timestamp": event.timestamp.isoformat(),
    })


async def _on_tick(event: Event) -> None:
    """Push tick data (sampled) to WebSocket clients."""
    # Only forward if clients are connected to avoid unnecessary work
    if not _clients:
        return
    await _broadcast({
        "type": "tick",
        "data": event.payload.get("tick", {}),
        "timestamp": event.timestamp.isoformat(),
    })


def register_ws_handlers(event_bus: EventBus) -> None:
    """Subscribe WebSocket broadcast handlers to the event bus.

    Call this during application startup after the event bus is ready.
    """
    event_bus.subscribe(EventType.POSITION_UPDATED, _on_position_updated)
    event_bus.subscribe(EventType.ORDER_FILLED, _on_order_filled)
    event_bus.subscribe(EventType.TICK, _on_tick)
    logger.info("WebSocket live feed handlers registered")


def _verify_ws_api_key(api_key: str | None) -> bool:
    """Verify API key for WebSocket connections."""
    settings = Settings()
    if not settings.is_production:
        return True
    return api_key == settings.api_secret_key


@router.websocket("/ws/live")
async def websocket_live_feed(
    websocket: WebSocket,
    api_key: str | None = Query(None, alias="api_key"),
) -> None:
    """WebSocket endpoint that streams P&L, position, and tick updates.

    Clients connect with ?api_key=<key> query param (required in production).
    Optionally, clients can send a 'ping' message and will receive 'pong'.
    """
    if not _verify_ws_api_key(api_key):
        await websocket.close(code=4001, reason="Invalid or missing API key")
        return

    await websocket.accept()
    _clients.add(websocket)
    logger.info(f"WebSocket client connected (total: {len(_clients)})")

    # Send initial snapshot
    container = get_container()
    if container.portfolio_manager:
        pnl = container.portfolio_manager.get_pnl()
        positions = container.portfolio_manager.get_open_positions()
        await websocket.send_json({
            "type": "snapshot",
            "data": {
                "pnl": pnl.model_dump(mode="json"),
                "positions": [p.model_dump(mode="json") for p in positions],
            },
        })

    try:
        while True:
            # Keep connection alive; handle client messages
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(websocket)
        logger.info(f"WebSocket client disconnected (total: {len(_clients)})")
