"""Backtest performance metrics — Sharpe, drawdown, win rate, etc."""

import math
from typing import Any

import numpy as np


def calculate_metrics(
    pnl_curve: list[float],
    trades: list[dict],
    initial_capital: float,
) -> dict[str, Any]:
    """Calculate comprehensive performance metrics.

    Args:
        pnl_curve: List of cumulative P&L values over time.
        trades: List of trade dicts from broker.
        initial_capital: Starting capital.

    Returns:
        Dict with all performance metrics.
    """
    if not pnl_curve:
        return _empty_metrics()

    pnl = np.array(pnl_curve, dtype=float)
    equity = initial_capital + pnl

    # ─── Returns ─────────────────────────────────────────────────
    total_pnl = float(pnl[-1])
    total_return_pct = (total_pnl / initial_capital) * 100

    # Daily returns (approximate)
    returns = np.diff(pnl)
    if len(returns) == 0:
        returns = np.array([0.0])

    # ─── Drawdown ────────────────────────────────────────────────
    peak = np.maximum.accumulate(equity)
    drawdown = equity - peak
    max_drawdown = float(np.min(drawdown))
    max_drawdown_pct = float(np.min(drawdown / peak) * 100) if peak.max() > 0 else 0

    # ─── Sharpe Ratio (annualized, assuming 252 trading days) ────
    if returns.std() > 0:
        sharpe = float(returns.mean() / returns.std() * math.sqrt(252))
    else:
        sharpe = 0.0

    # ─── Sortino Ratio (using downside deviation) ────────────────
    downside = returns[returns < 0]
    if len(downside) > 0 and downside.std() > 0:
        sortino = float(returns.mean() / downside.std() * math.sqrt(252))
    else:
        sortino = 0.0

    # ─── Calmar Ratio ────────────────────────────────────────────
    if max_drawdown != 0:
        calmar = float(total_pnl / abs(max_drawdown))
    else:
        calmar = 0.0

    # ─── Trade Statistics ────────────────────────────────────────
    num_trades = len(trades)
    if num_trades > 0:
        # Pair trades into round trips for win/loss analysis
        trade_pnls = _compute_trade_pnls(trades)
        wins = [p for p in trade_pnls if p > 0]
        losses = [p for p in trade_pnls if p < 0]

        win_rate = len(wins) / len(trade_pnls) * 100 if trade_pnls else 0
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        profit_factor = abs(sum(wins) / sum(losses)) if sum(losses) != 0 else float("inf")
    else:
        win_rate = avg_win = avg_loss = profit_factor = 0

    return {
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round(total_return_pct, 2),
        "max_drawdown": round(max_drawdown, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "sharpe_ratio": round(sharpe, 2),
        "sortino_ratio": round(sortino, 2),
        "calmar_ratio": round(calmar, 2),
        "num_trades": num_trades,
        "win_rate": round(win_rate, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else "inf",
        "max_equity": round(float(equity.max()), 2),
        "min_equity": round(float(equity.min()), 2),
    }


def _compute_trade_pnls(trades: list[dict]) -> list[float]:
    """Estimate P&L per trade from trade list."""
    pnls = []
    positions: dict[str, list[dict]] = {}

    for trade in trades:
        symbol = trade.get("tradingsymbol", "")
        side = trade.get("transaction_type", "")
        price = float(trade.get("average_price", 0))
        qty = int(trade.get("quantity", 0))

        if symbol not in positions:
            positions[symbol] = []

        if side == "BUY":
            positions[symbol].append({"qty": qty, "price": price, "side": "BUY"})
        else:
            # Match with existing buy positions
            remaining = qty
            for pos in positions[symbol]:
                if pos["side"] == "BUY" and pos["qty"] > 0:
                    close_qty = min(remaining, pos["qty"])
                    pnl = close_qty * (price - pos["price"])
                    pnls.append(pnl)
                    pos["qty"] -= close_qty
                    remaining -= close_qty
                    if remaining == 0:
                        break
            if remaining > 0:
                positions[symbol].append({"qty": remaining, "price": price, "side": "SELL"})

    return pnls


def _empty_metrics() -> dict:
    return {
        "total_pnl": 0, "total_return_pct": 0, "max_drawdown": 0,
        "max_drawdown_pct": 0, "sharpe_ratio": 0, "sortino_ratio": 0,
        "calmar_ratio": 0, "num_trades": 0, "win_rate": 0,
        "avg_win": 0, "avg_loss": 0, "profit_factor": 0,
        "max_equity": 0, "min_equity": 0,
    }
