#!/usr/bin/env python
"""Short Strangle Phase 1 smoke — Indian-validated v2 + calendar transfer.

Tests whether IC v2 + cal's edge mechanism (CI+VRP regime gate +
DoW/pre-event filter, validated at +₹13.1/trade) transfers to the
short-strangle-with-hedge structure (5-strike-OTM hedge instead of
IC's 8-strike wing).

Phase 1 keeps US-extrapolated parameters at current defaults
(0.15Δ, 15% PT, 30% SL, VIX 13-16) — only the high-confidence
Indian-validated changes (v2 + calendar) are applied.

Reference points:
  IC v2 + cal:    20 trips | ₹+263 | 47.5% WR | Sharpe +0.26 | ₹+13.1/trade
  IB B2 + cal:    38 trips | ₹+222 | 48.7% WR | Sharpe +0.06 | ₹+5.8/trade
  SS default:    (Sharpe -8.34 baseline from cross-strategy validation)

Decision rule:
  Per-trade EV > +₹8 → deploy SS Phase 1 as ss_1 shadow
  +₹0 to +₹8 → marginal; consider Phase 2 (delta ablation)
  Negative → SS structurally redundant with IC; retire shadow

Usage:
    uv run python scripts/smoke_ss_phase1.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtest.engine import BacktestEngine, _import_strategies
from src.backtest.gdfl_market_source import GDFLMarketSource
from src.market_data.simulator import NIFTY_SPOT_TOKEN


PARAMS_PATH = Path("reports/standalone_post_sebi/ss_phase1_params.json")
PARQUET_DIR = "data/gdfl_v2"
SMOKE_DAYS = 173
SMOKE_START = date(2024, 11, 20)


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("src.portfolio.positions", "src.portfolio.pnl", "src.broker.paper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger = logging.getLogger("smoke_ss_phase1")

    if not PARAMS_PATH.exists():
        logger.error(f"params file not found: {PARAMS_PATH}")
        return 2

    with PARAMS_PATH.open() as f:
        params_raw = json.load(f)
    params_raw.pop("_doc", None)

    logger.info(f"SS Phase 1 smoke (Indian-validated transfer only)")
    logger.info(f"  v2_regime={params_raw.get('require_premium_selling_regime_v2')}")
    logger.info(f"  calendar_filter={params_raw.get('require_calendar_filter')}")
    logger.info(f"  call_delta={params_raw.get('call_delta')} (Phase 1 = unchanged 0.15)")
    logger.info(f"  hedge=5 strikes (default)")

    _import_strategies()

    source = GDFLMarketSource(PARQUET_DIR, "NIFTY", NIFTY_SPOT_TOKEN)
    engine = BacktestEngine()
    results = await engine.run(
        strategy_name="short_strangle",
        strategy_params=params_raw,
        num_days=SMOKE_DAYS,
        start_date=SMOKE_START,
        initial_capital=1_000_000,
        market_source=source,
    )

    if "error" in results:
        logger.error(f"Backtest error: {results['error']}")
        return 2

    m = results.get("metrics", {})
    n_trades = int(m.get("num_trades", 0))
    pnl = float(m.get("total_pnl", 0.0))
    win_rate = float(m.get("win_rate", 0.0))
    sharpe = float(m.get("sharpe_ratio", 0.0))
    period = results.get("period", "n/a")
    round_trips = n_trades // 4
    mean = pnl / max(1, round_trips)

    print()
    print("=" * 70)
    print(f"SS Phase 1 smoke result — {period}")
    print("=" * 70)
    print(f"  Trade fills:     {n_trades}")
    print(f"  Round trips:     {round_trips}")
    print(f"  Total P&L:       Rs {pnl:>12,.2f}")
    print(f"  Win Rate:        {win_rate:>11.1f}%")
    print(f"  Sharpe:          {sharpe:>11.2f}")
    print(f"  Mean per trip:   Rs {mean:>+8,.1f}")
    print()
    print("Comparison to deployed shadow strategies:")
    print(f"  IC v2 + cal:     20 trips | ₹+263    | 47.5% | Sharpe +0.26 | +₹13.1/trip")
    print(f"  IB B2 + cal:     38 trips | ₹+222    | 48.7% | Sharpe +0.06 | +₹5.8/trip")
    print(f"  SS Phase 1:      {round_trips} trips | ₹{pnl:+,.0f} | {win_rate:.1f}% | "
          f"Sharpe {sharpe:+.2f} | ₹{mean:+.1f}/trip")
    print()

    if mean > 8 and round_trips >= 15:
        print("VERDICT: ✅ Deploy as ss_1 shadow alongside ic_2/ib_1")
    elif mean > 0 and round_trips >= 15:
        print("VERDICT: ⚠️  Marginal positive — proceed to Phase 2 (delta ablation)")
    elif round_trips < 15:
        print("VERDICT: ⚠️  Sample-thin — calendar filter too tight for SS?")
    else:
        print("VERDICT: ❌ Negative per-trade EV — SS likely redundant with IC v2+cal")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
