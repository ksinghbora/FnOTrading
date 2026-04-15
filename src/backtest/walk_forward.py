"""Walk-forward (out-of-sample) testing engine.

Splits the simulation period into rolling train/test windows and runs
the existing BacktestEngine on each window independently. Train windows
are for reference only (no param optimization yet); test windows are
strictly out-of-sample.

This is a **wrapper** around BacktestEngine — it does NOT modify the
engine or any strategy code. It just orchestrates multiple engine.run()
calls with different date ranges and aggregates the results.

Analogy to ML:
    Train window  = backtest (tune params, observe behavior)
    Test window   = paper trading (frozen params, unseen data)
    Walk-forward  = k-fold cross-validation (multiple train/test splits)

Usage:
    wf = WalkForwardEngine()
    report = await wf.run("short_straddle", total_days=180, test_days=30)
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from src.backtest.engine import BacktestEngine, _import_strategies, _trading_days
from src.backtest.metrics import calculate_metrics
from src.core.clock import MarketClock

logger = logging.getLogger(__name__)


@dataclass
class WindowResult:
    """Result of a single train or test window."""
    window_type: str           # "train" or "test"
    window_num: int
    start_date: date
    end_date: date
    num_days: int
    final_pnl: float
    metrics: dict
    daily_results: list[dict]


@dataclass
class WalkForwardReport:
    """Aggregated walk-forward results across all windows."""
    strategy: str
    total_days: int
    train_days: int
    test_days: int
    num_windows: int
    seed: int

    # Per-window results
    windows: list[WindowResult] = field(default_factory=list)

    # Aggregated test-only metrics (the ones that matter)
    test_total_pnl: float = 0.0
    test_mean_pnl: float = 0.0
    test_win_rate: float = 0.0        # % of test windows profitable
    test_sharpe: float = 0.0
    test_max_drawdown: float = 0.0
    test_profit_factor: float = 0.0

    # Train vs test comparison (overfitting detection)
    train_mean_pnl: float = 0.0
    degradation_pct: float = 0.0      # (train_mean - test_mean) / train_mean * 100

    # Combined equity curve (test windows only, stitched)
    test_equity_curve: list[dict] = field(default_factory=list)
    test_daily_results: list[dict] = field(default_factory=list)


class WalkForwardEngine:
    """Orchestrates rolling walk-forward testing using BacktestEngine."""

    async def run(
        self,
        strategy_name: str,
        strategy_params: dict | None = None,
        total_days: int = 180,
        train_days: int = 60,
        test_days: int = 30,
        step_days: int | None = None,
        start_date: date | None = None,
        initial_capital: float = 1_000_000,
        seed: int = 42,
        tick_interval_minutes: int = 1,
    ) -> WalkForwardReport:
        """Run walk-forward test with rolling train/test windows.

        Args:
            strategy_name: Registered strategy name.
            strategy_params: Strategy parameter overrides.
            total_days: Total trading days to cover.
            train_days: Days in each training window.
            test_days: Days in each test (out-of-sample) window.
            step_days: Days to slide forward between windows (default = test_days).
            start_date: First trading day (default ~total_days + buffer ago).
            initial_capital: Starting capital per window.
            seed: Random seed (same seed = same market for fair comparison).
            tick_interval_minutes: Tick resolution.

        Returns:
            WalkForwardReport with per-window and aggregated results.
        """
        _import_strategies()
        params = strategy_params or {}
        step_days = step_days or test_days

        # Generate full trading calendar
        clock = MarketClock()
        if start_date is None:
            start_date = date.today() - timedelta(days=int(total_days * 1.5))

        all_trading_days = _trading_days(start_date, total_days + train_days + 30, clock)
        if len(all_trading_days) < train_days + test_days:
            raise ValueError(
                f"Not enough trading days: need {train_days + test_days}, "
                f"got {len(all_trading_days)}"
            )

        # Build windows: [train_start, train_end] -> [test_start, test_end]
        windows: list[tuple[list[date], list[date]]] = []
        idx = 0
        while idx + train_days + test_days <= len(all_trading_days):
            train_slice = all_trading_days[idx: idx + train_days]
            test_slice = all_trading_days[idx + train_days: idx + train_days + test_days]
            windows.append((train_slice, test_slice))
            idx += step_days

        if not windows:
            raise ValueError("Could not create any train/test windows")

        num_windows = len(windows)
        logger.info(
            f"[WALK_FORWARD] {strategy_name}: {num_windows} windows, "
            f"train={train_days}d test={test_days}d step={step_days}d "
            f"total_calendar={all_trading_days[0]} to {all_trading_days[-1]}"
        )

        report = WalkForwardReport(
            strategy=strategy_name,
            total_days=total_days,
            train_days=train_days,
            test_days=test_days,
            num_windows=num_windows,
            seed=seed,
        )

        engine = BacktestEngine()
        running_equity = initial_capital

        for win_idx, (train_slice, test_slice) in enumerate(windows):
            logger.info(
                f"[WALK_FORWARD] Window {win_idx + 1}/{num_windows}: "
                f"train={train_slice[0]}..{train_slice[-1]} "
                f"test={test_slice[0]}..{test_slice[-1]}"
            )

            # ─── Train window ───────────────────────────────────
            train_result = await engine.run(
                strategy_name=strategy_name,
                strategy_id=f"{strategy_name}_wf_train_{win_idx}",
                strategy_params=params,
                num_days=len(train_slice),
                start_date=train_slice[0],
                initial_capital=initial_capital,
                seed=seed,
                tick_interval_minutes=tick_interval_minutes,
            )

            train_window = WindowResult(
                window_type="train",
                window_num=win_idx + 1,
                start_date=train_slice[0],
                end_date=train_slice[-1],
                num_days=len(train_slice),
                final_pnl=train_result["final_pnl"],
                metrics=train_result["metrics"],
                daily_results=train_result["daily_results"],
            )
            report.windows.append(train_window)

            # ─── Test window (out-of-sample) ────────────────────
            # Use a different seed offset so test market data is NOT
            # the same as train market data. But keep it deterministic.
            test_seed = seed + 1000 + win_idx

            test_result = await engine.run(
                strategy_name=strategy_name,
                strategy_id=f"{strategy_name}_wf_test_{win_idx}",
                strategy_params=params,
                num_days=len(test_slice),
                start_date=test_slice[0],
                initial_capital=initial_capital,
                seed=test_seed,
                tick_interval_minutes=tick_interval_minutes,
            )

            test_window = WindowResult(
                window_type="test",
                window_num=win_idx + 1,
                start_date=test_slice[0],
                end_date=test_slice[-1],
                num_days=len(test_slice),
                final_pnl=test_result["final_pnl"],
                metrics=test_result["metrics"],
                daily_results=test_result["daily_results"],
            )
            report.windows.append(test_window)

            # Stitch test equity curve
            for d in test_result["daily_results"]:
                running_equity += d["pnl"]
                report.test_daily_results.append(d)
                report.test_equity_curve.append({
                    "date": d["date"],
                    "equity": round(running_equity, 2),
                    "pnl": d["pnl"],
                    "window": win_idx + 1,
                })

            logger.info(
                f"[WALK_FORWARD] Window {win_idx + 1}: "
                f"train_pnl={train_result['final_pnl']:+,.0f} "
                f"test_pnl={test_result['final_pnl']:+,.0f}"
            )

        # ─── Aggregate test-only metrics ────────────────────────
        test_windows = [w for w in report.windows if w.window_type == "test"]
        train_windows = [w for w in report.windows if w.window_type == "train"]

        test_pnls = [w.final_pnl for w in test_windows]
        train_pnls = [w.final_pnl for w in train_windows]

        report.test_total_pnl = round(sum(test_pnls), 2)
        report.test_mean_pnl = round(sum(test_pnls) / len(test_pnls), 2) if test_pnls else 0
        report.test_win_rate = round(
            sum(1 for p in test_pnls if p > 0) / len(test_pnls) * 100, 1
        ) if test_pnls else 0
        report.train_mean_pnl = round(sum(train_pnls) / len(train_pnls), 2) if train_pnls else 0

        # Overfitting detection: how much does performance degrade out-of-sample?
        if report.train_mean_pnl > 0:
            report.degradation_pct = round(
                (report.train_mean_pnl - report.test_mean_pnl) / report.train_mean_pnl * 100, 1
            )
        elif report.train_mean_pnl < 0:
            # Both negative: degradation means test is MORE negative
            report.degradation_pct = round(
                (report.test_mean_pnl - report.train_mean_pnl) / abs(report.train_mean_pnl) * 100, 1
            )

        # Aggregate test daily P&Ls for Sharpe/drawdown
        all_test_daily_pnls = [d["pnl"] for d in report.test_daily_results]
        if all_test_daily_pnls:
            import numpy as np
            pnl_arr = np.array(all_test_daily_pnls)
            cumulative = np.cumsum(pnl_arr)
            equity = initial_capital + cumulative
            peak = np.maximum.accumulate(equity)
            drawdown = equity - peak
            report.test_max_drawdown = round(float(np.min(drawdown)), 2)

            if pnl_arr.std() > 0:
                import math
                report.test_sharpe = round(
                    float(pnl_arr.mean() / pnl_arr.std() * math.sqrt(252)), 2
                )

            wins_sum = float(pnl_arr[pnl_arr > 0].sum()) if (pnl_arr > 0).any() else 0
            losses_sum = float(pnl_arr[pnl_arr < 0].sum()) if (pnl_arr < 0).any() else 0
            if losses_sum != 0:
                report.test_profit_factor = round(abs(wins_sum / losses_sum), 2)

        logger.info(
            f"[WALK_FORWARD] Complete: {strategy_name} "
            f"test_total_pnl={report.test_total_pnl:+,.0f} "
            f"test_mean={report.test_mean_pnl:+,.0f} "
            f"test_win_rate={report.test_win_rate:.0f}% "
            f"degradation={report.degradation_pct:.1f}%"
        )

        return report
