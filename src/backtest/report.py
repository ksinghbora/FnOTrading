"""Backtest report generation."""

import json
from datetime import datetime
from pathlib import Path

from src.core.clock import now_ist


def generate_report(results: dict, output_dir: str = "reports") -> str:
    """Generate a JSON backtest report.

    Args:
        results: Backtest results from BacktestEngine.run().
        output_dir: Directory to save the report.

    Returns:
        Path to the generated report file.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    timestamp = now_ist().strftime("%Y%m%d_%H%M%S")
    strategy_id = results.get("strategy_id", "unknown")
    filename = f"{output_dir}/backtest_{strategy_id}_{timestamp}.json"

    report = {
        "generated_at": now_ist().isoformat(),
        "strategy_id": strategy_id,
        "params": results.get("params", {}),
        "metrics": results.get("metrics", {}),
        "final_pnl": results.get("final_pnl", 0),
        "num_data_points": len(results.get("equity_curve", [])),
        "num_trades": len(results.get("trades", [])),
    }

    with open(filename, "w") as f:
        json.dump(report, f, indent=2, default=str)

    return filename
