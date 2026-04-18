"""Decision snapshot logger — captures market state at every trading decision.

Writes one CSV row per ENTER/EXIT/SKIP decision. Designed for ML training:
each row = features at decision time + outcome P&L (backfilled on exit).

CSV files: data/decisions/decisions_YYYY-MM-DD.csv
"""

import csv
import logging
from dataclasses import dataclass, field, fields, asdict
from datetime import datetime
from pathlib import Path

from src.core.clock import now_ist

logger = logging.getLogger(__name__)

DECISIONS_DIR = Path("data/decisions")

# CSV column order (fixed for ML pipeline stability — APPEND new columns
# at the end, never reorder, so old parsers and downstream tools keep
# working).
COLUMNS = [
    "timestamp", "strategy_id", "leg", "decision", "mode",
    # Market state
    "spot", "vix", "pcr_oi", "max_pain", "max_pain_dist_pct", "iv_skew_ratio",
    # Intraday
    "move_from_open_pct", "morning_range_pct",
    # Time
    "dte", "hour", "minute", "day_of_week", "is_expiry",
    # Scoring
    "rule_score", "ai_adj", "final_score", "threshold",
    # Regime
    "regime",
    # Entry/exit details
    "entry_premium", "quantity", "exit_reason",
    # Outcome (backfilled on exit)
    "outcome_pnl", "held_minutes",
    # Phase A trend-score breakdown (Apr 18 — see memory/score_validation_plan.md).
    # Inputs to score_trend_following so we can replay the new score from logs:
    "breakout_strength", "oi_confirmed", "trend_duration_minutes",
    "vix_prev", "banknifty_confirming", "bn_data_available",
    # Per-factor contributions (sum equals rule_score unless score_clamp_hit):
    "score_f1_breakout", "score_f2_oi", "score_f3_duration",
    "score_f4_vix_level", "score_f5_vix_dir", "score_f6_banknifty",
    "score_clamp_hit",
    # Live threshold at decision time (catches future config drift):
    "trend_signal_threshold",
]


@dataclass
class DecisionSnapshot:
    """One row of decision data."""
    timestamp: str = ""
    strategy_id: str = ""
    leg: str = ""          # PREMIUM / TREND
    decision: str = ""     # ENTER / EXIT / SKIP
    mode: str = ""         # strangle / iron_condor / debit_spread

    # Market state
    spot: float = 0.0
    vix: float = 0.0
    pcr_oi: float = 0.0
    max_pain: float = 0.0
    max_pain_dist_pct: float = 0.0
    iv_skew_ratio: float = 0.0

    # Intraday context
    move_from_open_pct: float = 0.0
    morning_range_pct: float = 0.0

    # Time features
    dte: int = 0
    hour: int = 0
    minute: int = 0
    day_of_week: int = 0   # 0=Monday
    is_expiry: int = 0     # 0/1

    # Scoring
    rule_score: int = 0
    ai_adj: int = 0
    final_score: int = 0
    threshold: int = 0

    # Regime — 2D model
    regime: str = ""                   # Legacy combined regime
    vol_regime: str = ""               # LOW / NORMAL / HIGH / EXTREME
    action_regime: str = ""            # RANGE_BOUND / CHOPPY / TRENDING
    range_bound_score: float = 0.0     # Independent scorer 0-1
    choppy_score: float = 0.0          # Independent scorer 0-1
    trending_score: float = 0.0        # Independent scorer 0-1
    regime_confidence: float = 0.0     # Gap between top 2 scores
    regime_conflicted: bool = False    # Two scores > 0.5

    # Price action features (for ML regime classifier)
    move_efficiency: float = 0.0       # net_move / range (0=chop, 1=trend)
    session_range_pct: float = 0.0     # High-low range so far
    reversal_count: int = 0            # Direction changes in session
    vwap_crosses: int = 0              # Times price crossed session VWAP

    # Options microstructure (from chain recorder)
    atm_iv: float = 0.0               # ATM implied volatility
    iv_skew_ce_pe: float = 0.0        # CE IV / PE IV ratio (>1 = call expensive)
    total_oi_ce: int = 0              # Total CE open interest
    total_oi_pe: int = 0              # Total PE open interest
    oi_change_ce: int = 0             # CE OI change from yesterday
    oi_change_pe: int = 0             # PE OI change from yesterday
    bid_ask_spread_atm: float = 0.0   # ATM bid-ask spread (liquidity proxy)

    # Cross-market (for future use)
    nifty_banknifty_corr: float = 0.0  # Intraday correlation

    # Execution context
    prev_trade_pnl: float = 0.0       # Previous trade P&L (autocorrelation)
    trades_today: int = 0             # Trades so far today
    streak_count: int = 0             # Win/loss streak (positive=wins)

    # Entry/exit details
    entry_premium: float = 0.0
    quantity: int = 0
    exit_reason: str = ""

    # Outcome (backfilled on exit)
    outcome_pnl: float | None = None
    held_minutes: int | None = None

    # Actual day outcome (backfilled end-of-day for ML training labels)
    actual_day_regime: str = ""        # Ground truth: RANGE_BOUND / CHOPPY / TRENDING
    actual_day_move_pct: float = 0.0   # End-of-day open-to-close %
    actual_day_range_pct: float = 0.0  # End-of-day high-low range %

    # Shadow blocking (paper mode: entered despite block)
    shadow_blocked: bool = False

    # ─── Phase A trend-score breakdown (Apr 18 — score_validation_plan.md) ─
    # Inputs to score_trend_following so we can replay the new score from logs.
    # These are populated for TREND decisions; PREMIUM rows leave them at defaults.
    breakout_strength: float = 0.0          # % move beyond breakout level
    oi_confirmed: bool = False              # oi_breakout_confirm output
    trend_duration_minutes: int = 0         # mins since trend signal first fired
    vix_prev: float = 0.0                   # VIX at previous decision tick (factor 5)
    banknifty_confirming: bool | None = None  # TRUE/FALSE/None per BN alignment
    bn_data_available: bool = False         # FALSE = sticky-sentinel said BN unavailable

    # Per-factor contributions to rule_score. Sum equals rule_score unless
    # score_clamp_hit=True (then unclamped raw was outside [0, 100]).
    score_f1_breakout: int = 0
    score_f2_oi: int = 0
    score_f3_duration: int = 0
    score_f4_vix_level: int = 0
    score_f5_vix_dir: int = 0
    score_f6_banknifty: int = 0
    score_clamp_hit: bool = False

    # Live threshold at decision time — catches future config drift if
    # someone tunes trend_signal_threshold without an audit trail.
    trend_signal_threshold: int = 0


class DecisionLogger:
    """Append-only CSV logger for trading decisions.

    Usage:
        dl = DecisionLogger()
        dl.log(DecisionSnapshot(timestamp=..., ...))
    """

    def __init__(self, output_dir: Path = DECISIONS_DIR):
        self._dir = output_dir
        self._current_date: str = ""
        self._writer = None
        self._file = None

    def log(self, snap: DecisionSnapshot) -> None:
        """Write one decision row to today's CSV."""
        try:
            today = snap.timestamp[:10] if snap.timestamp else now_ist().strftime("%Y-%m-%d")
            self._ensure_file(today)
            row = asdict(snap)
            self._writer.writerow([row.get(c, "") for c in COLUMNS])
            self._file.flush()
        except Exception:
            logger.exception("[DECISION] Failed to write snapshot")

    def close(self) -> None:
        """Close the current file handle."""
        if self._file:
            self._file.close()
            self._file = None
            self._writer = None

    def _ensure_file(self, date_str: str) -> None:
        """Open or rotate the CSV file for today."""
        if self._current_date == date_str and self._writer:
            return

        self.close()
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"decisions_{date_str}.csv"
        is_new = not path.exists()

        self._file = open(path, "a", newline="")
        self._writer = csv.writer(self._file)
        self._current_date = date_str

        if is_new:
            self._writer.writerow(COLUMNS)
            self._file.flush()
            logger.info(f"[DECISION] Created {path}")
