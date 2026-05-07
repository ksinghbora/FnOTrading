#!/usr/bin/env python
"""LC v3a smoke — Indian-optimized long calendar (configuration only).

Tests configuration-level changes from v2/v2b that don't require code:
  - VIX band tightened: [12, 17] (Indian baseline ~15) vs [12, 22]/[14, 25]
  - Profit target raised: 40% (cost-wall-adjusted) vs 30%
  - Stop loss = 100% (max possible) vs 50%
  - Front-close buffer 60 min (SEBI Feb-2025 margin spike avoidance)
  - DROP CI+VRP regime gate entirely — use legacy filter pipeline
    with VIX-only filter (PCR + max-pain disabled, score gate
    inactive on calendar)

Code-required changes (NOT in v3a, flagged for v3b):
  - Day-of-week filter: Wed/Thu/Fri only
  - India VIX percentile filter (IV Rank proxy)
  - Per-strike IV differential check (front IV > back IV)
  - Event-day awareness (RBI MPC, CPI, budget)
  - Profit target on max-profit basis (not debit basis)

Usage:
    uv run python scripts/smoke_lc_v3a.py
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


PARAMS_PATH = Path("reports/standalone_post_sebi/lc_v3a_indian_optimized_params.json")
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

    logger = logging.getLogger("smoke_lc_v3a")

    if not PARAMS_PATH.exists():
        logger.error(f"params file not found: {PARAMS_PATH}")
        return 2

    with PARAMS_PATH.open() as f:
        params_raw = json.load(f)
    params_raw.pop("_doc", None)

    logger.info(f"LC v3a smoke (Indian-optimized config-only): "
                f"params={PARAMS_PATH.name}, days={SMOKE_DAYS}, from={SMOKE_START}")
    logger.info(f"  VIX band: [{params_raw.get('vix_entry_min')}, {params_raw.get('vix_entry_max')}]")
    logger.info(f"  profit target: {params_raw.get('profit_target_pct')}%")
    logger.info(f"  stop loss: {params_raw.get('stop_loss_pct')}%")
    logger.info(f"  front close buffer: {params_raw.get('front_close_buffer_minutes')} min")
    logger.info(f"  regime v2={params_raw.get('require_long_vol_regime_v2')} "
                f"v2b={params_raw.get('require_long_vol_regime_v2b')}")

    _import_strategies()

    source = GDFLMarketSource(PARQUET_DIR, "NIFTY", NIFTY_SPOT_TOKEN)
    avail = source.available_days()
    if not avail:
        logger.error(f"No GDFL parquet in {PARQUET_DIR}")
        return 2
    logger.info(f"GDFL corpus: {len(avail)} days, range {avail[0]} → {avail[-1]}")

    engine = BacktestEngine()
    results = await engine.run(
        strategy_name="long_calendar",
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
    period = results.get("period", "n/a")

    print()
    print("=" * 60)
    print(f"LC v3a smoke result — {period}")
    print("=" * 60)
    print(f"  Days backtested: {results.get('num_days', 0)}")
    print(f"  Trade fills:     {n_trades}")
    print(f"  Total P&L:       Rs {pnl:>12,.2f}")
    print(f"  Win Rate:        {win_rate:>11.1f}%")
    print(f"  Sharpe:          {m.get('sharpe_ratio', 0):>11.2f}")
    print(f"  Max DD:          Rs {m.get('max_drawdown', 0):>12,.2f}")
    print()
    round_trips = n_trades // 4
    print(f"  ≈ {round_trips} round trips ({n_trades} fills)")
    print()

    print("Comparison to prior LC variants:")
    print(f"  LC v2 (CI+VRP):   17 trips,  -₹1,707, 47.1% WR, Sharpe -0.95")
    print(f"  LC v2b (VRP-only): 31 trips, -₹120,734, 19.4% WR, Sharpe -3.02")
    print(f"  LC v3a (Indian config-only): {round_trips} trips, "
          f"₹{pnl:+,.0f}, {win_rate:.1f}% WR, Sharpe {m.get('sharpe_ratio', 0):.2f}")
    print()

    if pnl > 0 and round_trips >= 5:
        print("VERDICT: ✅ Indian-optimized config produces positive PnL")
    elif pnl > 0:
        print("VERDICT: ⚠️  Positive but sample-thin")
    elif round_trips == 0:
        print("VERDICT: ❌ Filter too tight — no entries")
    else:
        print("VERDICT: ❌ Still negative after Indian-optimized config-only changes")
        print("         (DoW filter + IV percentile + event-day require code; see v3b plan)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
