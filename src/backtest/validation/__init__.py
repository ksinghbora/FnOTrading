"""Statistical validation harness for strategy backtests.

Public API consolidation — external callers should import from
``src.backtest.validation`` rather than individual submodules so that
future re-organisations don't break downstream code.
"""

from src.backtest.validation.capacity import (
    LOT_MULTIPLES,
    CapacityUnavailable,
    simulate_capacity,
)
from src.backtest.validation.cost_sensitivity import (
    sensitivity_accepted,
    sensitivity_curve,
    shift_fill_pnl,
)
from src.backtest.validation.cpcv import CombinatorialPurgedCV, CPCVPath
from src.backtest.validation.metrics import (
    deflated_sharpe_ratio,
    min_track_record_length,
    pbo,
    probabilistic_sharpe_ratio,
    stationary_bootstrap_sharpe_ci,
)
from src.backtest.validation.regime import (
    REGIMES,
    RegimeStats,
    bucket_row,
    load_event_dates,
    stratify,
)
from src.backtest.validation.splits import HoldoutOverUsed, SplitLoader, StrategySplit
from src.backtest.validation.walk_forward import WalkForwardValidator, WFReport, WFWindow

__all__ = [
    # metrics
    "probabilistic_sharpe_ratio",
    "deflated_sharpe_ratio",
    "min_track_record_length",
    "pbo",
    "stationary_bootstrap_sharpe_ci",
    # splits
    "StrategySplit",
    "SplitLoader",
    "HoldoutOverUsed",
    # cpcv
    "CPCVPath",
    "CombinatorialPurgedCV",
    # walk-forward
    "WFWindow",
    "WFReport",
    "WalkForwardValidator",
    # regime
    "REGIMES",
    "RegimeStats",
    "load_event_dates",
    "bucket_row",
    "stratify",
    # cost sensitivity
    "shift_fill_pnl",
    "sensitivity_curve",
    "sensitivity_accepted",
    # capacity
    "LOT_MULTIPLES",
    "CapacityUnavailable",
    "simulate_capacity",
]
