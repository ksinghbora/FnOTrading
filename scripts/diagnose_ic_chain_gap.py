"""Diagnostic: why does IC have +2.64 Sharpe on gdfl_snapshots and -5.17 on gdfl_v2?

Apr 29 2026. Three sweep dimensions (risk_reward, strikes, vix_band) and
two sets of validation gates have all failed to recover IC's edge on
``gdfl_v2``. The strategy's parameter space is exhausted — but the same
default params on ``gdfl_snapshots`` produce a clearly-positive validated
result. Something *behavioral* differs between the two runs.

This script runs the iron_condor strategy with default params over the
SAME 227-day train+val window on each corpus, captures the decision log
(every ENTER / EXIT with regime, strikes, hold-time, PnL, exit_reason),
and emits a side-by-side comparison report so we can pinpoint the
divergence.

Output:
- ``reports/diagnose_ic_chain_gap/v2/decisions_*.csv`` — gdfl_v2 decisions
- ``reports/diagnose_ic_chain_gap/v1/decisions_*.csv`` — gdfl_snapshots decisions
- ``reports/diagnose_ic_chain_gap/comparison.md`` — side-by-side summary
- printed to stdout on completion

## Why this design

- Run sequentially (not in parallel) — both runs write to
  ``data/decisions/decisions_YYYY-MM-DD.csv``; concurrent writes would
  interleave rows and break the (timestamp, leg, decision) pairing.
  Sequential adds ~10-15 min wall time but guarantees clean data.
- Use full-window only (no CPCV / WF). The hypothesis is about the
  strategy's actual fill behavior, not its statistical robustness, so
  the cheap path suffices.
- Decisions are written by the strategy's own DecisionLogger
  (BaseStrategy._log_decision), which is the same code path live trading
  uses. Both backtest and live emit identical schemas.
- The live daemon writes ``decisions_2026-04-29.csv`` (today's date).
  Backtest writes ``decisions_2024-09-*.csv`` through
  ``decisions_2025-07-31.csv``. Different filenames → no collision.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.backtest.common import import_strategies
from src.backtest.engine import BacktestEngine
from src.backtest.gdfl_market_source import GDFLMarketSource
from src.backtest.validation.splits import SplitLoader, StrategySplit
from src.market_data.simulator import NIFTY_SPOT_TOKEN
from src.strategy.decision_logger import DECISIONS_DIR

logger = logging.getLogger("diagnose_ic")

OUT_BASE = REPO / "reports" / "diagnose_ic_chain_gap"
TRAIN_END = date(2025, 5, 30)
VAL_END = date(2025, 7, 31)
HOLDOUT_END = date(2026, 2, 27)
WINDOW_GLOB_PATTERNS = ["decisions_2024-*.csv", "decisions_2025-*.csv"]


def _clear_window_decisions() -> int:
    """Remove backtest-window decision CSVs. Live (2026-*) files untouched."""
    n = 0
    for pat in WINDOW_GLOB_PATTERNS:
        for f in DECISIONS_DIR.glob(pat):
            f.unlink()
            n += 1
    return n


def _copy_window_decisions(dst: Path) -> int:
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for pat in WINDOW_GLOB_PATTERNS:
        for f in DECISIONS_DIR.glob(pat):
            shutil.copy2(f, dst / f.name)
            n += 1
    return n


async def _run_full_window(parquet_dir: Path, label: str) -> dict:
    """One full-window IC backtest on the train+val window. Returns engine result."""
    engine = BacktestEngine()
    source = GDFLMarketSource(str(parquet_dir), "NIFTY", NIFTY_SPOT_TOKEN)
    split = StrategySplit(train_end=TRAIN_END, val_end=VAL_END, holdout_end=HOLDOUT_END)
    loader = SplitLoader(split=split, strategy="iron_condor", gdfl_source=source)
    days = sorted(set(loader.train_days()) | set(loader.val_days()))
    available = set(source.available_days())
    days = [d for d in days if d in available]
    logger.info("[%s] Running IC full-window: %d days", label, len(days))

    # Fresh source per call so the parquet day-cache doesn't bleed.
    fresh = GDFLMarketSource(str(parquet_dir), "NIFTY", NIFTY_SPOT_TOKEN)
    result = await engine.run(
        strategy_name="iron_condor",
        strategy_params={},  # all defaults
        initial_capital=1_000_000.0,
        seed=42,
        market_source=fresh,
        days=days,
    )
    metrics = result.get("metrics", {})
    logger.info(
        "[%s] Done — total_pnl=%s sharpe=%s num_trades=%s",
        label,
        metrics.get("total_pnl"),
        metrics.get("sharpe_ratio"),
        metrics.get("num_trades"),
    )
    return result


def _load_decisions(folder: Path) -> pd.DataFrame:
    files = sorted(folder.glob("decisions_*.csv"))
    if not files:
        return pd.DataFrame()
    frames = [pd.read_csv(f) for f in files]
    df = pd.concat(frames, ignore_index=True)
    return df


# ── Reporting helpers ──────────────────────────────────────────────


def _section_summary(df: pd.DataFrame) -> dict:
    """Compute one row of comparison stats for a decisions DataFrame."""
    if df.empty:
        return {"enters": 0, "exits": 0, "outcomes": 0}
    ic = df[df.get("mode", "") == "iron_condor"] if "mode" in df.columns else df
    enters = ic[ic["decision"] == "ENTER"] if "decision" in ic.columns else pd.DataFrame()
    exits = ic[ic["decision"] == "EXIT"] if "decision" in ic.columns else pd.DataFrame()
    out = {
        "enters": len(enters),
        "exits": len(exits),
        "outcomes": int(exits["outcome_pnl"].notna().sum()) if "outcome_pnl" in exits.columns else 0,
    }
    if "outcome_pnl" in exits.columns and exits["outcome_pnl"].notna().any():
        pnl = exits["outcome_pnl"].dropna().astype(float)
        out["pnl_total"] = float(pnl.sum())
        out["pnl_mean"] = float(pnl.mean())
        out["pnl_median"] = float(pnl.median())
        out["pnl_p05"] = float(pnl.quantile(0.05))
        out["pnl_p95"] = float(pnl.quantile(0.95))
        out["wins"] = int((pnl > 0).sum())
        out["losses"] = int((pnl < 0).sum())
        out["win_rate"] = float((pnl > 0).mean() * 100)
    if "held_minutes" in exits.columns and exits["held_minutes"].notna().any():
        held = exits["held_minutes"].dropna().astype(float)
        out["hold_min_p05"] = float(held.quantile(0.05))
        out["hold_min_median"] = float(held.median())
        out["hold_min_p95"] = float(held.quantile(0.95))
    return out


def _value_counts(df: pd.DataFrame, col: str, top: int = 8) -> str:
    if col not in df.columns or df.empty:
        return "(no data)"
    vc = df[col].fillna("").astype(str).value_counts().head(top)
    return ", ".join(f"{k}={v}" for k, v in vc.items())


def _render_comparison(v2_df: pd.DataFrame, v1_df: pd.DataFrame, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    v2_ic = v2_df[v2_df.get("mode", "") == "iron_condor"] if not v2_df.empty else v2_df
    v1_ic = v1_df[v1_df.get("mode", "") == "iron_condor"] if not v1_df.empty else v1_df

    s_v2 = _section_summary(v2_df)
    s_v1 = _section_summary(v1_df)

    lines: list[str] = []
    lines.append("# IC chain-gap diagnostic — gdfl_v2 vs gdfl_snapshots")
    lines.append("")
    lines.append(f"- Window: {TRAIN_END} train_end / {VAL_END} val_end (227 days)")
    lines.append("- Strategy: `iron_condor` with default params (no overrides)")
    lines.append("- Decision sources: `data/decisions/decisions_*.csv` captured per run")
    lines.append("")

    # Top-line stats
    lines.append("## Top-line stats")
    lines.append("")
    lines.append("| Metric | gdfl_v2 (the broken one) | gdfl_snapshots (the working one) | Δ |")
    lines.append("|---|---|---|---|")
    keys = [
        "enters", "exits", "outcomes", "pnl_total", "pnl_mean", "pnl_median",
        "pnl_p05", "pnl_p95", "wins", "losses", "win_rate",
        "hold_min_p05", "hold_min_median", "hold_min_p95",
    ]
    for k in keys:
        a = s_v2.get(k, "")
        b = s_v1.get(k, "")
        try:
            delta = f"{float(a) - float(b):+.2f}"
        except Exception:
            delta = ""
        lines.append(f"| {k} | {a} | {b} | {delta} |")
    lines.append("")

    # Categorical distributions
    lines.append("## Regime distribution at ENTER (top 8)")
    lines.append("")
    if not v2_ic.empty:
        v2_en = v2_ic[v2_ic["decision"] == "ENTER"]
        v1_en = v1_ic[v1_ic["decision"] == "ENTER"]
        for col in ("regime", "vol_regime", "action_regime"):
            lines.append(f"### `{col}`")
            lines.append(f"- v2: {_value_counts(v2_en, col)}")
            lines.append(f"- v1: {_value_counts(v1_en, col)}")
            lines.append("")

    # Exit reasons
    lines.append("## Exit-reason distribution")
    lines.append("")
    if not v2_ic.empty:
        v2_ex = v2_ic[v2_ic["decision"] == "EXIT"]
        v1_ex = v1_ic[v1_ic["decision"] == "EXIT"]
        lines.append(f"- v2: {_value_counts(v2_ex, 'exit_reason')}")
        lines.append(f"- v1: {_value_counts(v1_ex, 'exit_reason')}")
        lines.append("")

    # VIX bucketed PnL
    lines.append("## VIX bucket × per-trade PnL (median)")
    lines.append("")
    for label, frame in (("v2", v2_ic), ("v1", v1_ic)):
        if frame.empty or "vix" not in frame.columns:
            lines.append(f"- {label}: (no vix data)")
            continue
        ex = frame[frame["decision"] == "EXIT"].copy()
        if ex.empty or "outcome_pnl" not in ex.columns:
            lines.append(f"- {label}: (no exits)")
            continue
        ex = ex[ex["outcome_pnl"].notna()]
        ex["vix_bucket"] = pd.cut(
            ex["vix"], bins=[0, 14, 18, 22, 28, 100],
            labels=["<14", "14-18", "18-22", "22-28", ">28"],
            include_lowest=True,
        )
        agg = ex.groupby("vix_bucket", observed=True)["outcome_pnl"].agg(
            n="count", median="median", total="sum",
        ).round(0).to_dict("index")
        lines.append(f"- {label}: " + ", ".join(
            f"{b}: n={v['n']} med={v['median']:+.0f} tot={v['total']:+.0f}"
            for b, v in agg.items()
        ))
    lines.append("")

    out.write_text("\n".join(lines) + "\n")
    print("\n=== Comparison written to:", out)
    # Also dump to stdout for the caller
    print("\n".join(lines))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--gdfl-v2", default="data/gdfl_v2",
        help="Path to the deeper-chain corpus (the broken one)",
    )
    p.add_argument(
        "--gdfl-snapshots", default="data/gdfl_snapshots",
        help="Path to the original-chain corpus (the working one)",
    )
    p.add_argument(
        "--skip-runs", action="store_true",
        help="Don't re-run backtests; just re-render the comparison from "
             "previously-saved decision CSVs in reports/diagnose_ic_chain_gap/{v2,v1}",
    )
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


async def main_async(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    import_strategies()

    # Decisions writer is enabled by default (our env var only disables when set)
    os.environ.pop("FNO_DISABLE_DECISIONS", None)

    v2_out = OUT_BASE / "v2"
    v1_out = OUT_BASE / "v1"

    if not args.skip_runs:
        # Phase 1: gdfl_v2
        n = _clear_window_decisions()
        logger.info("Cleared %d existing window-decision CSVs", n)
        await _run_full_window(Path(args.gdfl_v2), "gdfl_v2")
        copied = _copy_window_decisions(v2_out)
        logger.info("Copied %d decision CSVs to %s", copied, v2_out)

        # Phase 2: gdfl_snapshots
        _clear_window_decisions()
        await _run_full_window(Path(args.gdfl_snapshots), "gdfl_snapshots")
        copied = _copy_window_decisions(v1_out)
        logger.info("Copied %d decision CSVs to %s", copied, v1_out)

    # Compare
    v2_df = _load_decisions(v2_out)
    v1_df = _load_decisions(v1_out)
    logger.info("Loaded v2: %d rows, v1: %d rows", len(v2_df), len(v1_df))
    _render_comparison(v2_df, v1_df, OUT_BASE / "comparison.md")
    return 0


def main() -> int:
    return asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    sys.exit(main())
