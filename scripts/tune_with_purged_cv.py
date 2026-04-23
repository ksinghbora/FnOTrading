"""Purged K-Fold parameter tuning driver.

Evaluates each candidate value of a single parameter across N purged folds
and prints an accept/reject table. Replaces ad-hoc grid search on a fixed
train window — which is how the Apr 23 `breakout_confirmation_pct` 0.5→0.7
tuning got curve-fit to Jul-Aug 2025 loss days.

Usage:
    uv run python scripts/tune_with_purged_cv.py \\
        --strategy portfolio \\
        --param premium_stop_loss_pct \\
        --range 0.15,0.20,0.25,0.30,0.35

    uv run python scripts/tune_with_purged_cv.py \\
        --strategy portfolio --param trend_stop_loss_pct \\
        --range 20,25,30,35 --source historical --splits 5 --embargo 0.01

The wrapper picks GDFL real-tick data when a snapshot parquet is present
(``data/gdfl_snapshots``), otherwise falls back to the historical CSV
engine. Both paths produce comparable ``sharpe``/``profit_factor`` numbers
— the existing layer — so this script doesn't rebuild backtest plumbing.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtest.engine import BacktestEngine, _import_strategies, _trading_days
from src.backtest.purged_kfold import PurgedKFold
from src.core.clock import MarketClock

logger = logging.getLogger("tune_with_purged_cv")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Purged K-Fold CV parameter tuning",
    )
    p.add_argument("--strategy", "-s", required=True,
                   help="Registered strategy name (e.g. portfolio, short_strangle)")
    p.add_argument("--param", required=True,
                   help="Parameter name to sweep (e.g. premium_stop_loss_pct)")
    p.add_argument("--range", dest="values", required=True,
                   help="Comma-separated values to try (e.g. 0.15,0.20,0.25)")
    p.add_argument("--splits", type=int, default=5,
                   help="Number of folds (default 5)")
    p.add_argument("--embargo", type=float, default=0.01,
                   help="Embargo fraction each side of test fold (default 0.01 = 1%%)")
    p.add_argument("--source", choices=["auto", "gdfl", "historical", "synthetic"],
                   default="auto",
                   help="Backtest data source (default: auto-detect GDFL else synthetic)")
    p.add_argument("--underlying", "-u", default="NIFTY")
    p.add_argument("--days", type=int, default=180,
                   help="Trading days of data to span (default 180)")
    p.add_argument("--start", dest="start_date", default="",
                   help="First date YYYY-MM-DD (default: data-dependent)")
    p.add_argument("--capital", "-c", type=float, default=1_000_000)
    p.add_argument("--lots", type=int, default=1)
    p.add_argument("--parquet-dir", default="data/gdfl_snapshots/")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def _parse_values(raw: str) -> list[float | int]:
    """Parse the --range CSV, keeping int vs float based on presence of '.'."""
    out: list[float | int] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if "." in tok or "e" in tok.lower():
            out.append(float(tok))
        else:
            out.append(int(tok))
    if not out:
        raise SystemExit("--range produced no values")
    return out


def _pick_source(args: argparse.Namespace) -> str:
    """Decide between GDFL (real ticks) and synthetic based on --source + what's on disk."""
    if args.source != "auto":
        return args.source
    gdfl_dir = Path(args.parquet_dir)
    if gdfl_dir.exists() and any(gdfl_dir.glob("*.parquet")):
        return "gdfl"
    return "synthetic"


def _collect_dates(args: argparse.Namespace, source: str) -> list[date]:
    """Build the chronological date universe for splitting."""
    if source == "gdfl":
        from src.backtest.gdfl_market_source import GDFLMarketSource
        from src.market_data.simulator import BANKNIFTY_SPOT_TOKEN, NIFTY_SPOT_TOKEN
        spot_token = NIFTY_SPOT_TOKEN if args.underlying == "NIFTY" else BANKNIFTY_SPOT_TOKEN
        src = GDFLMarketSource(args.parquet_dir, args.underlying, spot_token)
        available = src.available_days()
        if args.start_date:
            start = date.fromisoformat(args.start_date)
            available = [d for d in available if d >= start]
        return available[: args.days]
    # Synthetic / historical share the same MarketClock calendar.
    clock = MarketClock()
    start = (date.fromisoformat(args.start_date)
             if args.start_date else date.today().replace(year=date.today().year - 1))
    return _trading_days(start, args.days, clock)


def _make_strategy_fn(args: argparse.Namespace, source: str):
    """Build the async runner PurgedKFold.evaluate() calls per fold.

    The returned callable runs ONE backtest on the test window with the
    override applied — we don't currently re-tune on the train window
    (that'd be the next layer: nested CV). This wrapper intentionally
    stays a thin probe so the result `sharpe` is a pure OOS score.

    GDFL and synthetic paths both go through `BacktestEngine.run` which
    already supports a `market_source=None` switch for synthetic.
    """
    async def runner(
        *, param_set: dict, train_dates: list[date], test_dates: list[date]
    ) -> dict:
        engine = BacktestEngine()
        strategy_params = {
            "underlying": args.underlying,
            "quantity_lots": args.lots,
            **param_set,
        }
        kwargs: dict = {
            "strategy_name": args.strategy,
            "strategy_params": strategy_params,
            "num_days": len(test_dates),
            "start_date": test_dates[0],
            "initial_capital": args.capital,
        }
        if source == "gdfl":
            from src.backtest.gdfl_market_source import GDFLMarketSource
            from src.market_data.simulator import BANKNIFTY_SPOT_TOKEN, NIFTY_SPOT_TOKEN
            spot_token = NIFTY_SPOT_TOKEN if args.underlying == "NIFTY" else BANKNIFTY_SPOT_TOKEN
            kwargs["market_source"] = GDFLMarketSource(
                args.parquet_dir, args.underlying, spot_token
            )
        result = await engine.run(**kwargs)
        m = result.get("metrics", {})
        return {
            "sharpe": float(m.get("sharpe_ratio", 0.0)),
            "profit_factor": float(m.get("profit_factor", 0.0)),
            "total_pnl": float(m.get("total_pnl", 0.0)),
            "num_trades": int(m.get("num_trades", 0)),
            "win_rate": float(m.get("win_rate", 0.0)),
        }

    return runner


def _print_header(args: argparse.Namespace, dates: list[date], source: str) -> None:
    print("=" * 80)
    print(f" Purged K-Fold CV — strategy={args.strategy} param={args.param}")
    print(f" Source={source}  Splits={args.splits}  Embargo={args.embargo:.3f}")
    print(f" Dates: {dates[0]} .. {dates[-1]}  ({len(dates)} days)")
    print("=" * 80)
    header = (
        f"{args.param:<30}  {'mean_OOS_Sharpe':>16}  {'std_OOS_Sharpe':>16}  "
        f"{'OOS_PF':>8}  {'verdict':>8}"
    )
    print(header)
    print("-" * len(header))


def _print_row(value, res: dict) -> None:
    verdict = "ACCEPT" if res["accepted"] else "REJECT"
    print(
        f"{str(value):<30}  {res['mean_oos_sharpe']:>16.3f}  "
        f"{res['std_oos_sharpe']:>16.3f}  {res['oos_pf']:>8.3f}  {verdict:>8}"
    )


async def main_async() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # PurgedKFold logs are the useful part — always show them.
    logging.getLogger("src.backtest.purged_kfold").setLevel(logging.INFO)
    logging.getLogger("tune_with_purged_cv").setLevel(logging.INFO)

    _import_strategies()
    source = _pick_source(args)
    dates = _collect_dates(args, source)
    if len(dates) < args.splits * 2:
        raise SystemExit(
            f"Need >= {args.splits * 2} dates for {args.splits}-fold CV, "
            f"got {len(dates)}. Widen --days or provide more data."
        )

    values = _parse_values(args.values)
    kf = PurgedKFold(n_splits=args.splits, embargo_pct=args.embargo)
    strategy_fn = _make_strategy_fn(args, source)

    _print_header(args, dates, source)

    for v in values:
        res = await kf.evaluate(
            param_set={args.param: v},
            strategy_fn=strategy_fn,
            dates=dates,
        )
        _print_row(v, res)

    print("-" * 80)
    print(" Rejection rule: std_oos_sharpe > mean_oos_sharpe  OR  mean <= 0")
    print(" See src/backtest/purged_kfold.py for the full methodology.")
    return 0


def main() -> None:
    rc = asyncio.run(main_async())
    sys.exit(rc)


if __name__ == "__main__":
    main()
