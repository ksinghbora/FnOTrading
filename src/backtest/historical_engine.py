"""Historical backtest engine — replays real spot + VIX data through strategies.

Uses real NIFTY/VIX minute candles from CSV files (downloaded from Kite API)
for spot price movement. Option pricing still uses Black-Scholes (downloading
per-strike option history for all strikes is impractical — thousands of API calls).

This gives us:
- Real intraday patterns (morning range, trending days, VIX spikes, expiry effects)
- Real PCR/max pain won't work (synthetic OI) — but spot-based filters (trend, VIX) work
- Real breakout patterns for trend strategy validation

Usage:
    engine = HistoricalBacktestEngine()
    result = await engine.run("iron_condor", spot_csv="data/nifty_spot_minute.csv")
"""

import csv
import logging
import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytz
from scipy.stats import norm as sp_norm

from src.backtest.engine import (
    BacktestClock,
    _import_strategies,
    _register_options,
    _update_market,
    _TOKEN_BASE,
    _SPOT_TOKENS,
    _STRIKE_STEPS,
    _NUM_STRIKES,
)
from src.backtest.metrics import calculate_metrics
from src.backtest.simulator import FillSimulator
from src.broker.paper.client import PaperBrokerClient
from src.core.clock import MarketClock
from src.core.constants import INDIA_VIX_TOKEN, LOT_SIZES
from src.core.events import EventBus
from src.core.models import Order, PnL, Tick
from src.core.types import OrderStatus, ProductType
from src.market_data.aggregator import OHLCAggregator
from src.market_data.feed import TickFeedManager
from src.market_data.option_chain import OptionChainBuilder
from src.portfolio.charges import calculate_charges
from src.portfolio.manager import PortfolioManager
from src.strategy.context import StrategyContext
from src.strategy.registry import create_strategy

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")


def _load_spot_csv(filepath: str | Path) -> dict[date, list[dict]]:
    """Load spot CSV and group by trading day.

    Returns: {date: [{time, open, high, low, close, volume}, ...]}
    """
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"Spot data file not found: {filepath}")

    day_candles: dict[date, list[dict]] = defaultdict(list)

    with open(filepath) as f:
        reader = csv.DictReader(f)
        for row in reader:
            dt = datetime.fromisoformat(row["date"].replace("+05:30", "+05:30"))
            if dt.tzinfo is None:
                dt = IST.localize(dt)
            day_candles[dt.date()].append({
                "datetime": dt,
                "time": dt.time(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row.get("volume", 0)),
            })

    # Sort candles within each day
    for day in day_candles:
        day_candles[day].sort(key=lambda c: c["datetime"])

    return dict(day_candles)


def _load_vix_csv(filepath: str | Path) -> dict[date, list[dict]]:
    """Load VIX CSV and group by trading day."""
    filepath = Path(filepath)
    if not filepath.exists():
        logger.warning(f"VIX data file not found: {filepath}, will use default VIX=14.5")
        return {}

    day_candles: dict[date, list[dict]] = defaultdict(list)

    with open(filepath) as f:
        reader = csv.DictReader(f)
        for row in reader:
            dt = datetime.fromisoformat(row["date"].replace("+05:30", "+05:30"))
            if dt.tzinfo is None:
                dt = IST.localize(dt)
            day_candles[dt.date()].append({
                "datetime": dt,
                "close": float(row["close"]),
            })

    for day in day_candles:
        day_candles[day].sort(key=lambda c: c["datetime"])

    return dict(day_candles)


class HistoricalBacktestEngine:
    """Replays real spot + VIX data through any registered strategy."""

    async def run(
        self,
        strategy_name: str,
        spot_csv: str | Path,
        vix_csv: str | Path | None = None,
        strategy_id: str = "",
        strategy_params: dict | None = None,
        num_days: int | None = None,
        start_date: date | None = None,
        initial_capital: float = 1_000_000,
    ) -> dict:
        """Run backtest with real historical spot data.

        Args:
            strategy_name: Registered strategy name.
            spot_csv: Path to spot price CSV (from download_spot_data.py).
            vix_csv: Path to VIX CSV (optional, defaults to nearby path).
            strategy_id: Unique ID.
            strategy_params: Strategy parameter overrides.
            num_days: Limit to N trading days (None = use all available).
            start_date: Start from this date (None = earliest in data).
            initial_capital: Starting capital.

        Returns:
            Dict with metrics, daily_results, equity_curve (same format as BacktestEngine).
        """
        strategy_id = strategy_id or f"{strategy_name}_hist"
        params = strategy_params or {}
        underlying = params.get("underlying", "NIFTY")

        _import_strategies()

        # Load data
        spot_data = _load_spot_csv(spot_csv)
        if not spot_data:
            return {"error": "No spot data loaded"}

        if vix_csv is None:
            # Auto-detect VIX file next to spot file
            spot_path = Path(spot_csv)
            vix_csv = spot_path.parent / f"india_vix_{spot_path.name.split('_')[-1]}"

        vix_data = _load_vix_csv(vix_csv) if vix_csv else {}

        # Filter trading days
        trading_days = sorted(spot_data.keys())
        if start_date:
            trading_days = [d for d in trading_days if d >= start_date]
        if num_days:
            trading_days = trading_days[:num_days]

        if not trading_days:
            return {"error": "No trading days in range"}

        logger.info(
            f"[HIST_BACKTEST] Loaded {len(trading_days)} trading days: "
            f"{trading_days[0]} to {trading_days[-1]}"
        )

        # ─── Create infrastructure ────────────────────────────────
        event_bus = EventBus()
        clock = BacktestClock()
        broker = PaperBrokerClient(initial_capital=initial_capital)
        fill_sim = FillSimulator()
        feed = TickFeedManager(event_bus)
        aggregator = OHLCAggregator(event_bus)
        chain_builder = OptionChainBuilder(event_bus, clock)
        portfolio = PortfolioManager(event_bus, broker, chain_builder)

        await broker.connect()

        # ─── Create strategy ──────────────────────────────────────
        strategy = create_strategy(strategy_name, strategy_id, params)

        # ─── Market setup ─────────────────────────────────────────
        spot_token = _SPOT_TOKENS.get(underlying, 256265)
        step = _STRIKE_STEPS.get(underlying, 50)

        chain_builder.register_spot(spot_token, underlying)

        next_token = [_TOKEN_BASE]
        option_tokens: dict[tuple[str, float, str], int] = {}

        def alloc_token(ul: str, strike: float, ot: str) -> int:
            key = (ul, strike, ot)
            if key not in option_tokens:
                option_tokens[key] = next_token[0]
                next_token[0] += 1
            return option_tokens[key]

        # Initial expiry
        first_candle = spot_data[trading_days[0]][0]
        spot = first_candle["close"]
        clock.set_time(IST.localize(datetime.combine(trading_days[0], time(9, 15))))
        expiry = clock.next_expiry(underlying)
        _register_options(chain_builder, underlying, spot, step, _NUM_STRIKES, expiry, alloc_token)

        # ─── Wire order callback ──────────────────────────────────
        async def order_callback(signal_obj):
            orders = []
            for leg in signal_obj.legs:
                ltp = feed.get_ltp(leg.instrument_token)
                price = float(leg.price) if float(leg.price) > 0 else float(ltp or 0)
                if price <= 0:
                    continue

                price = fill_sim.simulate_fill(price, leg.order_side)
                broker.set_ltp(leg.tradingsymbol, price)

                order_id = await broker.place_order(
                    tradingsymbol=leg.tradingsymbol,
                    exchange="NFO",
                    side=leg.order_side,
                    quantity=leg.quantity,
                    price=price,
                )

                order = Order(
                    broker_order_id=order_id,
                    strategy_id=signal_obj.strategy_id,
                    instrument_token=leg.instrument_token,
                    tradingsymbol=leg.tradingsymbol,
                    order_side=leg.order_side,
                    order_type=leg.order_type,
                    product=ProductType.NRML,
                    quantity=leg.quantity,
                    fill_price=Decimal(str(round(price, 2))),
                    fill_quantity=leg.quantity,
                    status=OrderStatus.FILLED,
                )
                portfolio._positions.update_from_fill(order)

                inst = "CE" if "CE" in leg.tradingsymbol else (
                    "PE" if "PE" in leg.tradingsymbol else "FUT"
                )
                charges = calculate_charges(
                    Decimal(str(round(price, 2))), leg.quantity, leg.order_side, inst
                )
                portfolio._pnl.add_charges(signal_obj.strategy_id, charges.total)
                orders.append(order_id)
            return orders

        def portfolio_getter(what, sid):
            if what == "positions":
                return portfolio.get_positions(sid)
            elif what == "pnl":
                return portfolio.get_pnl(sid)
            return None

        # ─── Wire strategy context ────────────────────────────────
        context = StrategyContext(
            strategy_id=strategy_id,
            feed=feed,
            option_chain_builder=chain_builder,
            aggregator=aggregator,
            clock=clock,
            order_callback=order_callback,
            portfolio_getter=portfolio_getter,
        )
        strategy.set_context(context)
        feed.subscribe([spot_token, INDIA_VIX_TOKEN], strategy_id)

        await strategy.on_start()

        # ─── Main replay loop ─────────────────────────────────────
        equity_curve = []
        daily_results = []
        running_pnl = Decimal("0")
        vix = 14.5  # Default VIX

        logger.info(
            f"[HIST_BACKTEST] Starting: strategy={strategy_name} underlying={underlying} "
            f"days={len(trading_days)} capital={initial_capital:,.0f}"
        )

        for day_idx, day in enumerate(trading_days):
            if day_idx > 0:
                strategy.reset_day_state()
                portfolio.reset_daily()
                aggregator._builders.clear()

            # Expiry rollover
            clock.set_time(IST.localize(datetime.combine(day, time(9, 15))))
            new_expiry = clock.next_expiry(underlying)
            if new_expiry != expiry:
                expiry = new_expiry
                _register_options(
                    chain_builder, underlying, spot, step, _NUM_STRIKES, expiry, alloc_token
                )

            candles = spot_data.get(day, [])
            vix_candles = vix_data.get(day, [])
            vix_idx = 0
            day_open = candles[0]["open"] if candles else spot
            day_trades_start = len(broker._trades)

            for candle in candles:
                now = candle["datetime"]
                if now.tzinfo is None:
                    now = IST.localize(now)
                clock.set_time(now)

                spot = candle["close"]

                # Update VIX from real data
                while vix_idx < len(vix_candles) and vix_candles[vix_idx]["datetime"] <= now:
                    vix = vix_candles[vix_idx]["close"]
                    vix_idx += 1

                # Time to expiry
                minutes_into_day = (now.hour - 9) * 60 + now.minute - 15
                minutes_into_day = max(0, minutes_into_day)
                T = max(
                    1 / (365 * 24),
                    (expiry - day).days / 365 - minutes_into_day / (375 * 365),
                )
                iv_base = vix / 100

                # Update all market data (option chain, feed, portfolio LTPs)
                _update_market(
                    feed, broker, chain_builder, portfolio,
                    underlying, expiry, spot, vix, now,
                    spot_token, step, _NUM_STRIKES, T, iv_base,
                    option_tokens, alloc_token,
                    day_open=day_open,
                )

                # Feed spot tick to aggregator for candle building
                spot_tick = Tick.model_construct(
                    instrument_token=spot_token,
                    tradingsymbol=underlying,
                    timestamp=now,
                    ltp=Decimal(str(round(spot, 2))),
                    volume=candle.get("volume", 0),
                    oi=0,
                    bid_price=Decimal("0"), ask_price=Decimal("0"),
                    bid_qty=0, ask_qty=0,
                    high=Decimal(str(round(candle["high"], 2))),
                    low=Decimal(str(round(candle["low"], 2))),
                    open=Decimal(str(round(candle["open"], 2))),
                    close=Decimal(str(round(candle["close"], 2))),
                )
                aggregator.process_tick_direct(spot_tick)

                try:
                    signal = await strategy.on_tick(spot_tick)
                    if signal:
                        await order_callback(signal)
                except Exception as e:
                    logger.debug(f"[HIST_BACKTEST] Strategy tick error: {e}")

            # ─── Day end ──────────────────────────────────────────
            pnl = portfolio.get_pnl(strategy_id)
            day_pnl = pnl.net
            running_pnl += day_pnl
            day_trades = len(broker._trades) - day_trades_start

            daily_results.append({
                "date": day.isoformat(),
                "day_of_week": day.strftime("%A"),
                "spot_open": round(day_open, 2),
                "spot_close": round(spot, 2),
                "pnl": round(float(day_pnl), 2),
                "charges": round(float(pnl.charges), 2),
                "trades": day_trades,
                "equity": round(initial_capital + float(running_pnl), 2),
            })
            equity_curve.append({
                "date": day.isoformat(),
                "equity": round(initial_capital + float(running_pnl), 2),
                "pnl": round(float(day_pnl), 2),
            })

            if day_idx % 10 == 0 or day_idx == len(trading_days) - 1:
                logger.info(
                    f"[HIST_BACKTEST] Day {day_idx + 1}/{len(trading_days)}: {day} "
                    f"spot={spot:.0f} vix={vix:.1f} pnl={float(day_pnl):+.0f} "
                    f"cumulative={float(running_pnl):+.0f} trades={day_trades}"
                )

        # ─── Finalize ─────────────────────────────────────────────
        await strategy.on_stop()

        pnl_values = [d["pnl"] for d in daily_results]
        metrics = calculate_metrics(pnl_values, broker._trades, initial_capital)
        metrics["total_charges"] = round(sum(d["charges"] for d in daily_results), 2)
        metrics["num_days"] = len(trading_days)

        result = {
            "strategy": strategy_name,
            "strategy_id": strategy_id,
            "underlying": underlying,
            "data_source": "historical",
            "spot_csv": str(spot_csv),
            "period": f"{trading_days[0]} to {trading_days[-1]}",
            "num_days": len(trading_days),
            "lots": params.get("quantity_lots", 1),
            "lot_size": LOT_SIZES.get(underlying, 75),
            "initial_capital": initial_capital,
            "params": strategy.params.model_dump(mode="json"),
            "metrics": metrics,
            "daily_results": daily_results,
            "equity_curve": equity_curve,
            "final_pnl": round(float(running_pnl), 2),
        }

        logger.info(
            f"[HIST_BACKTEST] Complete: {strategy_name} {len(trading_days)}d "
            f"P&L={float(running_pnl):+,.0f} "
            f"Sharpe={metrics.get('sharpe_ratio', 0):.2f} "
            f"WinRate={metrics.get('win_rate', 0):.1f}%"
        )

        return result
