#!/usr/bin/env python
"""Iron Butterfly v2 + calendar-aware smoke on 173-day post-SEBI corpus.

Tests the hypothesis that IB + theory-grounded regime gate (CI+VRP) +
calendar-aware filter (DoW Tue/Wed/Thu + pre-event block) is a
strictly superior strategy to IC v2 on Indian post-SEBI options:

  IC v2 holdout:  +₹584 / 324 trades / Sharpe 0.35 / PF~1.05
  IB v2 expected: similar edge per-trade, ~2.5× capital efficiency
                  (60% margin saving vs IC v2 per OptionX/Bajaj
                  Broking analysis)

  Calendar filter expected effect:
    - Reduces sample 30-50% (only 3 of 5 weekdays, plus 1-2 day
      pre-event blocks)
    - Improves per-trade EV (excludes Monday IV-rise days, Friday
      weekend-gap, pre-event entries that get killed by event move)

Usage:
    uv run python scripts/smoke_ib_v2.py
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


PARAMS_PATH = Path("reports/standalone_post_sebi/ib_v2_calendar_aware_params.json")
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

    logger = logging.getLogger("smoke_ib_v2")

    if not PARAMS_PATH.exists():
        logger.error(f"params file not found: {PARAMS_PATH}")
        return 2

    with PARAMS_PATH.open() as f:
        params_raw = json.load(f)
    params_raw.pop("_doc", None)

    logger.info(f"IB v2 + calendar smoke: params={PARAMS_PATH.name}, "
                f"days={SMOKE_DAYS}, from={SMOKE_START}")
    logger.info(f"  short_call_delta={params_raw.get('short_call_delta')}")
    logger.info(f"  short_put_delta={params_raw.get('short_put_delta')}")
    logger.info(f"  wing_width_strikes={params_raw.get('wing_width_strikes')}")
    logger.info(f"  stop_loss_pct={params_raw.get('stop_loss_pct')}")
    logger.info(f"  profit_target_pct={params_raw.get('profit_target_pct')}")
    logger.info(f"  require_premium_selling_regime_v2={params_raw.get('require_premium_selling_regime_v2')}")
    logger.info(f"  require_calendar_filter={params_raw.get('require_calendar_filter')}")
    logger.info(f"  allowed_days_of_week={params_raw.get('allowed_days_of_week')}")
    logger.info(f"  block_pre_event_days={params_raw.get('block_pre_event_days')}")

    _import_strategies()

    source = GDFLMarketSource(PARQUET_DIR, "NIFTY", NIFTY_SPOT_TOKEN)
    avail = source.available_days()
    if not avail:
        logger.error(f"No GDFL parquet in {PARQUET_DIR}")
        return 2
    logger.info(f"GDFL corpus: {len(avail)} days, range {avail[0]} → {avail[-1]}")

    engine = BacktestEngine()
    results = await engine.run(
        strategy_name="iron_butterfly",
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
    print(f"IB v2 + calendar smoke result — {period}")
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

    print("Comparison to prior IC variants:")
    print(f"  IC v2 holdout:        324 trades, +₹584,  47% WR, Sharpe +0.35")
    print(f"  IC v2 train+val:     ~280 trades, ~PF 1.20")
    print(f"  IB v2 + cal smoke:    {round_trips} trips, ₹{pnl:+,.0f}, {win_rate:.1f}% WR, "
          f"Sharpe {m.get('sharpe_ratio', 0):.2f}")
    print()

    if pnl > 0 and round_trips >= 5:
        print("VERDICT: ✅ IB v2 + calendar produces positive PnL — proceed to formal validation")
    elif pnl > 0:
        print("VERDICT: ⚠️  Positive but sample-thin (calendar filter may be too tight)")
    elif round_trips == 0:
        print("VERDICT: ❌ Filter stack too tight — no entries fired")
    else:
        print("VERDICT: ❌ Negative PnL — IB structural advantage didn't translate")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
