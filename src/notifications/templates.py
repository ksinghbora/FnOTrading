"""Message templates for notifications.

Simple string formatting templates for trade execution, P&L updates,
risk alerts, and system status messages.
"""

from datetime import datetime
from decimal import Decimal


# ─── Trade Execution Templates ──────────────────────────────────────


def trade_executed(
    tradingsymbol: str,
    side: str,
    quantity: int,
    price: Decimal,
    strategy_id: str,
    order_id: str = "",
) -> str:
    emoji = "\U0001f7e2" if side == "BUY" else "\U0001f534"
    order_line = f"\nOrder: `{order_id[:8]}...`" if order_id else ""
    return (
        f"{emoji} *Trade Executed*\n"
        f"Strategy: `{strategy_id}`\n"
        f"Symbol: `{tradingsymbol}`\n"
        f"Side: *{side}* | Qty: {quantity}\n"
        f"Price: {price}"
        f"{order_line}"
    )


def order_rejected(
    tradingsymbol: str,
    side: str,
    quantity: int,
    strategy_id: str,
    reason: str = "",
) -> str:
    return (
        f"\u26d4 *Order Rejected*\n"
        f"Strategy: `{strategy_id}`\n"
        f"Symbol: `{tradingsymbol}`\n"
        f"Side: {side} | Qty: {quantity}\n"
        f"Reason: {reason}"
    )


# ─── P&L Update Templates ──────────────────────────────────────────


def pnl_update(
    day_pnl: Decimal,
    realized: Decimal,
    unrealized: Decimal,
    open_positions: int,
) -> str:
    sign = "\U0001f7e2" if day_pnl >= 0 else "\U0001f534"
    return (
        f"{sign} *P&L Update*\n"
        f"Day P&L: *{day_pnl:+,.2f}*\n"
        f"Realized: {realized:+,.2f}\n"
        f"Unrealized: {unrealized:+,.2f}\n"
        f"Open Positions: {open_positions}"
    )


def strategy_pnl(strategy_id: str, pnl: Decimal) -> str:
    sign = "\U0001f7e2" if pnl >= 0 else "\U0001f534"
    return f"{sign} `{strategy_id}` P&L: *{pnl:+,.2f}*"


# ─── Risk Alert Templates ──────────────────────────────────────────


def risk_breach(breach_type: str, details: str) -> str:
    return (
        f"\u26a0\ufe0f *Risk Breach*\n"
        f"Type: *{breach_type}*\n"
        f"Details: {details}"
    )


def circuit_breaker_triggered(reason: str) -> str:
    return (
        f"\U0001f6a8 *Circuit Breaker Triggered*\n"
        f"Reason: {reason}\n"
        f"All new orders are blocked."
    )


def kill_switch_activated(
    reason: str,
    orders_cancelled: int = 0,
    positions_closed: int = 0,
) -> str:
    return (
        f"\U0001f6d1 *KILL SWITCH ACTIVATED*\n"
        f"Reason: {reason}\n"
        f"Orders cancelled: {orders_cancelled}\n"
        f"Positions closed: {positions_closed}\n"
        f"All trading halted."
    )


# ─── System Status Templates ───────────────────────────────────────


def system_started(environment: str, paper_trading: bool) -> str:
    mode = "PAPER" if paper_trading else "LIVE"
    return (
        f"\u2705 *System Started*\n"
        f"Environment: `{environment}`\n"
        f"Mode: *{mode}*\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )


def system_stopped(reason: str = "Normal shutdown") -> str:
    return (
        f"\u23f9 *System Stopped*\n"
        f"Reason: {reason}\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )


def connection_lost(source: str) -> str:
    return (
        f"\u26a0\ufe0f *Connection Lost*\n"
        f"Source: {source}\n"
        f"Time: {datetime.now().strftime('%H:%M:%S')}"
    )


def connection_restored(source: str) -> str:
    return (
        f"\u2705 *Connection Restored*\n"
        f"Source: {source}\n"
        f"Time: {datetime.now().strftime('%H:%M:%S')}"
    )


def strategy_started(strategy_id: str) -> str:
    return f"\u25b6\ufe0f Strategy `{strategy_id}` started"


def strategy_stopped(strategy_id: str) -> str:
    return f"\u23f9 Strategy `{strategy_id}` stopped"


def strategy_error(strategy_id: str, error: str) -> str:
    return (
        f"\u274c *Strategy Error*\n"
        f"Strategy: `{strategy_id}`\n"
        f"Error: {error}"
    )
