"""Parameter sweep harness for entry-gate calibration.

Apr 28 2026. The standalone_v2 IC validation revealed that the +2.30
Sharpe seen in the Apr 26 standalone_v1 run was an artifact of commit
``f928037``'s pre-state, where ``_paper_mode=True`` (set by
``PAPER_TRADING=true`` in .env) silently bypassed score / VIX / PCR /
max-pain entry filters. After f928037 enforced the gates uniformly,
the same strategy on the same data produces -3.90 Sharpe — meaning the
gate parameters were never actually exercised in standalone_v1, so we
have no idea which threshold values genuinely work.

This script sweeps a grid of entry-gate parameters and reports the
resulting full-window backtest metrics for each combination, so we
can pick a configuration whose strategy *both* passes its own filters
*and* validates positively.

## Why no CPCV / walk-forward in the sweep

CPCV+WF on the deep ``gdfl_v2`` chain takes ~3-4h per strategy. A
16-config sweep at that depth = 48-64h, which is wasteful when the
goal is *picking a configuration*, not validating one. The full-window
backtest (~10-30 min per config on the deep chain) plus the new MC
permutation + block-bootstrap-CI gates gives us ~80% of the signal at
~10% of the cost. Once we've narrowed to 1-3 promising configs, those
go through the full validate_strategy.py harness.

## Usage

    uv run python scripts/sweep_strategy.py \\
        --strategy iron_condor \\
        --train-end 2025-05-30 --val-end 2025-07-31 \\
        --parquet-dir data/gdfl_v2 --underlying NIFTY \\
        --sweep-config sweeps/ic_gates_v1.json \\
        --out reports/sweeps/ic_gates_v1.csv

## Sweep config format (JSON)

Two equivalent forms — pick whichever is more readable for the sweep:

### Form A: cartesian grid (combinatorial product)

    {
      "base_params": {"vix_entry_min": 14},
      "grid": {
        "vix_entry_max": [22, 25, 28],
        "wing_width_strikes": [6, 8, 10]
      }
    }

→ 9 configs, each named ``vix_entry_max=22_wing_width_strikes=6`` etc.

### Form B: explicit list (when grid would over-generate)

    {
      "base_params": {"vix_entry_min": 14},
      "configs": [
        {"name": "tight",   "vix_entry_max": 22, "wing_width_strikes": 6},
        {"name": "default", "vix_entry_max": 22, "wing_width_strikes": 8},
        {"name": "wide",    "vix_entry_max": 28, "wing_width_strikes": 10}
      ]
    }

## Output CSV columns

- ``config``: name of the configuration
- one column per swept parameter (the value used for that config)
- ``total_pnl``, ``sharpe``, ``num_trades``, ``max_dd`` from the full-window run
- ``mc_p_value``: Monte Carlo permutation p-value (lower = stronger skill signal)
- ``boot_ci_low``, ``boot_ci_high``: 90% block-bootstrap Sharpe CI
- ``cost_sharpe_+0.5``: Sharpe at +0.5 spread cost shift (robustness check)
- ``runtime_sec``: per-config wall time (so the sweep itself is auditable)

Configs are written to the CSV incrementally — if the sweep crashes
mid-way, completed rows are preserved.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
import time as _time
from copy import deepcopy
from datetime import date
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.backtest.common import import_strategies
from src.backtest.engine import BacktestEngine
from src.backtest.gdfl_market_source import GDFLMarketSource
from src.backtest.validation.cost_sensitivity import sensitivity_curve
from src.backtest.validation.metrics import (
    monte_carlo_skill_pvalue,
    stationary_bootstrap_sharpe_ci,
)
from src.backtest.validation.splits import SplitLoader, StrategySplit
from src.market_data.simulator import NIFTY_SPOT_TOKEN

logger = logging.getLogger("sweep_strategy")


# ── Sweep-config expansion ──────────────────────────────────────────


def _expand_sweep(sweep: dict) -> list[tuple[str, dict]]:
    """Return ``[(config_name, full_params_dict), ...]`` from sweep JSON.

    ``full_params_dict`` = base_params merged with the per-config overrides.
    Names are deterministic so the CSV is stable across re-runs of the
    same sweep file.
    """
    base = dict(sweep.get("base_params", {}))
    out: list[tuple[str, dict]] = []
    if "grid" in sweep:
        grid = sweep["grid"]
        keys = list(grid.keys())
        for combo in product(*(grid[k] for k in keys)):
            overrides = dict(zip(keys, combo, strict=True))
            name = "_".join(f"{k}={v}" for k, v in overrides.items())
            out.append((name, {**base, **overrides}))
    elif "configs" in sweep:
        for i, cfg in enumerate(sweep["configs"]):
            cfg = dict(cfg)
            name = cfg.pop("name", f"cfg_{i}")
            out.append((name, {**base, **cfg}))
    else:
        raise ValueError("sweep config must define either 'grid' or 'configs'")
    return out


# ── Per-config evaluation ──────────────────────────────────────────


# Same calibration constants as src/backtest/validation/report.py to keep
# sweep MC/bootstrap numbers comparable to the full validation report.
_BOOTSTRAP_BLOCK_DAYS = 15.0
_BOOTSTRAP_N = 10_000
_BOOTSTRAP_SEED = 20260427
_BOOTSTRAP_CONFIDENCE = 0.90


async def _run_one(
    engine: BacktestEngine,
    strategy_name: str,
    parquet_dir: str,
    underlying: str,
    spot_token: int,
    initial_capital: float,
    seed: int,
    days: list[date],
    params: dict,
) -> dict:
    """Run one full-window backtest + cost sweep + MC/bootstrap. No CPCV/WF."""
    # Fresh source per call so the parquet day-cache doesn't bleed between configs.
    source = GDFLMarketSource(parquet_dir, underlying, spot_token)
    available = set(source.available_days())
    days_run = [d for d in days if d in available]
    if not days_run:
        raise RuntimeError(f"No overlap between sweep days and {parquet_dir} availability")

    result = await engine.run(
        strategy_name=strategy_name,
        strategy_params=dict(params),
        initial_capital=initial_capital,
        seed=seed,
        market_source=source,
        days=days_run,
    )
    metrics = dict(result.get("metrics", {}))
    trades = list(result.get("trades", []))
    daily_results = list(result.get("daily_results", []))
    daily_pnl = np.asarray([float(d.get("pnl", 0.0)) for d in daily_results], dtype=float)

    # Cost sensitivity at +0.5 shift only (cheapest robustness signal).
    cost_curve = sensitivity_curve(trades, shifts=(0.5,), initial_capital=initial_capital)
    cost_sharpe = float(cost_curve.get(0.5, {}).get("sharpe_ratio", 0.0))

    # MC + block-bootstrap CI gates (only if we have enough days).
    if daily_pnl.size >= 30:
        mc_p = monte_carlo_skill_pvalue(
            daily_pnl,
            block_size_mean=_BOOTSTRAP_BLOCK_DAYS,
            n_perm=_BOOTSTRAP_N,
            benchmark=0.0,
            seed=_BOOTSTRAP_SEED,
        )
        ci_low, _ci_med, ci_high = stationary_bootstrap_sharpe_ci(
            daily_pnl,
            block_size_mean=_BOOTSTRAP_BLOCK_DAYS,
            n_boot=_BOOTSTRAP_N,
            confidence=_BOOTSTRAP_CONFIDENCE,
            seed=_BOOTSTRAP_SEED,
        )
    else:
        mc_p, ci_low, ci_high = 1.0, 0.0, 0.0

    return {
        "total_pnl": float(metrics.get("total_pnl", 0.0)),
        "sharpe": float(metrics.get("sharpe_ratio", 0.0)),
        "num_trades": int(metrics.get("num_trades", 0)),
        "max_dd": float(metrics.get("max_drawdown", 0.0)),
        "win_rate": float(metrics.get("win_rate", 0.0)),
        "mc_p_value": float(mc_p),
        "boot_ci_low": float(ci_low),
        "boot_ci_high": float(ci_high),
        "cost_sharpe_+0.5": cost_sharpe,
        "n_days": int(daily_pnl.size),
    }


# ── CLI entry point ────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--strategy", required=True, help="Registered strategy name (e.g. iron_condor)")
    p.add_argument("--train-end", required=True)
    p.add_argument("--val-end", required=True)
    p.add_argument(
        "--holdout-end", default="",
        help="Optional holdout-end (only used to construct StrategySplit; "
             "sweep does not access holdout)",
    )
    p.add_argument("--parquet-dir", required=True)
    p.add_argument("--underlying", default="NIFTY")
    p.add_argument("--spot-token", type=int, default=NIFTY_SPOT_TOKEN)
    p.add_argument("--sweep-config", required=True, help="JSON sweep specification")
    p.add_argument("--out", required=True, help="Output CSV path")
    p.add_argument("--initial-capital", type=float, default=1_000_000.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


async def main_async(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    import_strategies()

    # Load + expand sweep config
    sweep_path = Path(args.sweep_config)
    if not sweep_path.exists():
        logger.error("--sweep-config %s does not exist", sweep_path)
        return 2
    with sweep_path.open() as fh:
        sweep = json.load(fh)
    configs = _expand_sweep(sweep)
    if not configs:
        logger.error("Sweep produced 0 configurations — check --sweep-config")
        return 2
    logger.info("Sweep expanded to %d configurations", len(configs))

    # Build day set: train ∪ val (no holdout)
    parquet_dir = Path(args.parquet_dir)
    if not parquet_dir.exists():
        logger.error("--parquet-dir %s does not exist", parquet_dir)
        return 2
    holdout_end = args.holdout_end or args.val_end
    split = StrategySplit(
        train_end=date.fromisoformat(args.train_end),
        val_end=date.fromisoformat(args.val_end),
        holdout_end=date.fromisoformat(holdout_end),
    )
    gdfl_for_loader = GDFLMarketSource(parquet_dir, args.underlying, args.spot_token)
    loader = SplitLoader(split=split, strategy=args.strategy, gdfl_source=gdfl_for_loader)
    train_days = loader.train_days()
    val_days = loader.val_days()
    combined = sorted(set(train_days) | set(val_days))
    logger.info(
        "Sweep window: %d days (%d train + %d val)",
        len(combined), len(train_days), len(val_days),
    )

    engine = BacktestEngine()

    # Resolve column set for the CSV. Per-config swept-param keys may
    # vary (Form B configs can have different keys), so collect union.
    swept_keys: list[str] = []
    seen_keys: set[str] = set()
    base_keys = set((sweep.get("base_params") or {}).keys())
    for _, params in configs:
        for k in params:
            if k not in seen_keys and k not in base_keys:
                seen_keys.add(k)
                swept_keys.append(k)

    metric_cols = [
        "total_pnl", "sharpe", "num_trades", "max_dd", "win_rate",
        "mc_p_value", "boot_ci_low", "boot_ci_high",
        "cost_sharpe_+0.5", "n_days",
    ]
    header = ["config", *swept_keys, *metric_cols, "runtime_sec", "status"]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fh = out_path.open("w", newline="")
    writer = csv.DictWriter(fh, fieldnames=header)
    writer.writeheader()
    fh.flush()

    for i, (name, params) in enumerate(configs, 1):
        t0 = _time.perf_counter()
        logger.info(
            "[%d/%d] Running config %r — overrides: %s",
            i, len(configs), name,
            {k: v for k, v in params.items() if k not in base_keys},
        )
        row: dict[str, Any] = {"config": name}
        for k in swept_keys:
            row[k] = params.get(k, "")
        try:
            metrics = await _run_one(
                engine=engine,
                strategy_name=args.strategy,
                parquet_dir=str(parquet_dir),
                underlying=args.underlying,
                spot_token=args.spot_token,
                initial_capital=args.initial_capital,
                seed=args.seed,
                days=combined,
                params=params,
            )
            row.update(metrics)
            row["status"] = "ok"
        except Exception as e:
            logger.exception("Config %r failed: %s", name, e)
            for col in metric_cols:
                row[col] = ""
            row["status"] = f"error: {type(e).__name__}: {e}"
        row["runtime_sec"] = round(_time.perf_counter() - t0, 1)
        writer.writerow(row)
        fh.flush()
        logger.info(
            "[%d/%d] Done %r in %.1fs — sharpe=%s pnl=%s n_trades=%s mc_p=%s",
            i, len(configs), name, row["runtime_sec"],
            row.get("sharpe", "n/a"), row.get("total_pnl", "n/a"),
            row.get("num_trades", "n/a"), row.get("mc_p_value", "n/a"),
        )

    fh.close()
    logger.info("Sweep complete — %d configs written to %s", len(configs), out_path)
    return 0


def main() -> int:
    return asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    sys.exit(main())
