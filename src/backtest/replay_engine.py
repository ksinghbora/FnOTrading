"""Replay backtest engine — replays recorded option chain snapshots through strategies.

Uses real option chain data (captured by ChainSnapshotRecorder during live trading)
instead of synthetic Black-Scholes pricing. This is the most realistic backtest mode.

Falls back to BS pricing for any strikes/times not covered by snapshots.

Usage:
    engine = ReplayBacktestEngine()
    result = await engine.run(
        "iron_condor",
        snapshot_dir="data/chain_snapshots",
        spot_csv="data/nifty_spot_minute.csv",
    )
"""

import csv
import logging
import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytz

from src.backtest.engine import (
    BacktestClock,
    _import_strategies,
    _register_options,
    _update_market,
    _SPOT_TOKENS,
    _STRIKE_STEPS,
    _TOKEN_BASE,
    _NUM_STRIKES,
)
from src.backtest.historical_engine import _load_spot_csv, _load_vix_csv
from src.backtest.metrics import calculate_metrics
from src.backtest.simulator import FillSimulator
from src.broker.paper.client import PaperBrokerClient
from src.core.constants import INDIA_VIX_TOKEN, LOT_SIZES
from src.core.events import EventBus
from src.core.models import Greeks, OptionData, Order, PnL, Tick
from src.core.types import OptionType, OrderStatus, ProductType
from src.market_data.aggregator import OHLCAggregator
from src.market_data.feed import TickFeedManager
from src.market_data.option_chain import OptionChainBuilder
from src.portfolio.charges import calculate_charges
from src.portfolio.manager import PortfolioManager
from src.strategy.context import StrategyContext
from src.strategy.registry import create_strategy

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")


def _load_chain_snapshots(snapshot_dir: Path, trading_days: list[date]) -> dict:
    """Load chain snapshot CSVs for the given trading days.

    Returns: {date: {minute_key: {(strike, option_type): row_dict}}}
    where minute_key = "HH:MM" for O(1) lookup per tick.
    """
    snapshots: dict[date, dict[str, dict[tuple[float, str], dict]]] = {}

    for day in trading_days:
        filepath = snapshot_dir / f"chain_{day.isoformat()}.csv"
        if not filepath.exists():
            continue

        day_data: dict[str, dict[tuple[float, str], dict]] = defaultdict(dict)
        with open(filepath) as f:
            reader = csv.DictReader(f)
            for row in reader:
                dt = datetime.fromisoformat(row["time"])
                minute_key = dt.strftime("%H:%M")
                strike = float(row["strike"])
                opt_type = row["option_type"]
                day_data[minute_key][(strike, opt_type)] = {
                    "ltp": float(row["ltp"]),
                    "iv": float(row.get("iv", 0)),
                    "delta": float(row.get("delta", 0)),
                    "gamma": float(row.get("gamma", 0)),
                    "theta": float(row.get("theta", 0)),
                    "vega": float(row.get("vega", 0)),
                    "oi": int(row.get("oi", 0)),
                    "volume": int(row.get("volume", 0)),
                    "bid_price": float(row.get("bid_price", 0)),
                    "ask_price": float(row.get("ask_price", 0)),
                }

        if day_data:
            snapshots[day] = dict(day_data)

    return snapshots


class ReplayBacktestEngine:
    """Replays recorded chain snapshots through any registered strategy.

    For ticks with snapshot data: uses real recorded option prices.
    For ticks without: falls back to BS pricing via _update_market().
    """

    async def run(
        self,
        strategy_name: str,
        snapshot_dir: str | Path,
        spot_csv: str | Path,
        vix_csv: str | Path | None = None,
        strategy_id: str = "",
        strategy_params: dict | None = None,
        num_days: int | None = None,
        start_date: date | None = None,
        initial_capital: float = 1_000_000,
    ) -> dict:
        """Run backtest with recorded chain snapshot data.

        Args:
            strategy_name: Registered strategy name.
            snapshot_dir: Directory containing chain_YYYY-MM-DD.csv files.
            spot_csv: Path to spot price CSV.
            vix_csv: Path to VIX CSV (optional).
            strategy_id: Unique ID.
            strategy_params: Strategy parameter overrides.
            num_days: Limit trading days.
            start_date: Start from this date.
            initial_capital: Starting capital.

        Returns:
            Dict with metrics, daily_results, equity_curve.
        """
        strategy_id = strategy_id or f"{strategy_name}_replay"
        params = strategy_params or {}
        underlying = params.get("underlying", "NIFTY")
        snapshot_dir = Path(snapshot_dir)

        _import_strategies()

        # Load spot data
        spot_data = _load_spot_csv(spot_csv)
        if not spot_data:
            return {"error": "No spot data loaded"}

        if vix_csv is None:
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

        # Load chain snapshots
        chain_snaps = _load_chain_snapshots(snapshot_dir, trading_days)
        days_with_snapshots = len(chain_snaps)
        total_snapshot_points = sum(len(v) for v in chain_snaps.values())

        logger.info(
            f"[REPLAY_BACKTEST] Loaded {len(trading_days)} days, "
            f"{days_with_snapshots} with snapshots ({total_snapshot_points} timepoints)"
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

        strategy = create_strategy(strategy_name, strategy_id, params)

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

        first_candle = spot_data[trading_days[0]][0]
        spot = first_candle["close"]
        clock.set_time(IST.localize(datetime.combine(trading_days[0], time(9, 15))))
        expiry = clock.next_expiry(underlying)
        _register_options(chain_builder, underlying, spot, step, _NUM_STRIKES, expiry, alloc_token)

        # ─── Order callback ─────────────────────────────────────
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

        # ─── Main replay loop ──────────────────────────────────
        equity_curve = []
        daily_results = []
        running_pnl = Decimal("0")
        vix = 14.5
        snapshot_hits = 0
        bs_fallbacks = 0

        for day_idx, day in enumerate(trading_days):
            if day_idx > 0:
                strategy.reset_day_state()
                portfolio.reset_daily()
                aggregator._builders.clear()

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
            day_snaps = chain_snaps.get(day, {})

            for candle in candles:
                now = candle["datetime"]
                if now.tzinfo is None:
                    now = IST.localize(now)
                clock.set_time(now)

                spot = candle["close"]

                while vix_idx < len(vix_candles) and vix_candles[vix_idx]["datetime"] <= now:
                    vix = vix_candles[vix_idx]["close"]
                    vix_idx += 1

                minutes_into_day = max(0, (now.hour - 9) * 60 + now.minute - 15)
                T = max(
                    1 / (365 * 24),
                    (expiry - day).days / 365 - minutes_into_day / (375 * 365),
                )
                iv_base = vix / 100

                # Try to use recorded snapshot for this minute
                minute_key = now.strftime("%H:%M")
                snap_data = day_snaps.get(minute_key)

                if snap_data:
                    # Apply real recorded prices to chain
                    _apply_snapshot(
                        feed, broker, chain_builder, portfolio,
                        underlying, expiry, spot, vix, now,
                        spot_token, step, _NUM_STRIKES, T, iv_base,
                        option_tokens, alloc_token,
                        snap_data, day_open,
                    )
                    snapshot_hits += 1
                else:
                    # Fall back to BS pricing with IV dynamics
                    _update_market(
                        feed, broker, chain_builder, portfolio,
                        underlying, expiry, spot, vix, now,
                        spot_token, step, _NUM_STRIKES, T, iv_base,
                        option_tokens, alloc_token,
                        day_open=day_open,
                    )
                    bs_fallbacks += 1

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
                    logger.debug(f"[REPLAY_BACKTEST] Strategy tick error: {e}")

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
                "data_source": "snapshot" if day in chain_snaps else "bs_fallback",
            })
            equity_curve.append({
                "date": day.isoformat(),
                "equity": round(initial_capital + float(running_pnl), 2),
                "pnl": round(float(day_pnl), 2),
            })

            if day_idx % 10 == 0 or day_idx == len(trading_days) - 1:
                logger.info(
                    f"[REPLAY_BACKTEST] Day {day_idx + 1}/{len(trading_days)}: {day} "
                    f"spot={spot:.0f} vix={vix:.1f} pnl={float(day_pnl):+.0f} "
                    f"cumulative={float(running_pnl):+.0f}"
                )

        await strategy.on_stop()

        pnl_values = [d["pnl"] for d in daily_results]
        metrics = calculate_metrics(pnl_values, broker._trades, initial_capital)
        metrics["total_charges"] = round(sum(d["charges"] for d in daily_results), 2)
        metrics["num_days"] = len(trading_days)
        metrics["snapshot_hits"] = snapshot_hits
        metrics["bs_fallbacks"] = bs_fallbacks
        metrics["snapshot_coverage_pct"] = round(
            100 * snapshot_hits / max(1, snapshot_hits + bs_fallbacks), 1
        )

        result = {
            "strategy": strategy_name,
            "strategy_id": strategy_id,
            "underlying": underlying,
            "data_source": "replay",
            "snapshot_dir": str(snapshot_dir),
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
            f"[REPLAY_BACKTEST] Complete: {strategy_name} {len(trading_days)}d "
            f"P&L={float(running_pnl):+,.0f} "
            f"Sharpe={metrics.get('sharpe_ratio', 0):.2f} "
            f"Snapshot coverage={metrics['snapshot_coverage_pct']}%"
        )

        return result


def _apply_snapshot(
    feed, broker, chain_builder, portfolio,
    underlying, expiry, spot, vix, now,
    spot_token, step, num_strikes, T, iv_base,
    option_tokens, alloc_token,
    snap_data: dict[tuple[float, str], dict],
    day_open: float,
):
    """Apply recorded snapshot data to chain, falling back to BS for missing strikes.

    First runs _update_market for full BS baseline, then overwrites with real data.
    """
    # Start with BS pricing as baseline (covers all strikes)
    _update_market(
        feed, broker, chain_builder, portfolio,
        underlying, expiry, spot, vix, now,
        spot_token, step, num_strikes, T, iv_base,
        option_tokens, alloc_token,
        day_open=day_open,
    )

    # Overwrite with real recorded data where available
    chain = chain_builder.get_chain(underlying, expiry)
    if not chain:
        return

    exp_str = expiry.strftime("%y%b").upper()

    for (strike, opt_str), data in snap_data.items():
        strike_dec = Decimal(str(int(strike))) if strike == int(strike) else Decimal(str(strike))

        entry = chain_builder._find_or_create_entry(chain, strike_dec)

        key = (underlying, strike, opt_str)
        if key not in option_tokens:
            token = alloc_token(underlying, strike, opt_str)
            opt_type_enum = OptionType.CE if opt_str == "CE" else OptionType.PE
            sym = f"{underlying}{exp_str}{int(strike)}{opt_str}"
            chain_builder.register_option(
                token, underlying, expiry, strike_dec, opt_type_enum, sym,
            )
        else:
            token = option_tokens[key]

        symbol = f"{underlying}{exp_str}{int(strike)}{opt_str}"
        price_dec = Decimal(str(round(data["ltp"], 2)))
        bid_dec = Decimal(str(round(data["bid_price"], 2))) if data["bid_price"] > 0 else price_dec
        ask_dec = Decimal(str(round(data["ask_price"], 2))) if data["ask_price"] > 0 else price_dec
        opt_type_enum = OptionType.CE if opt_str == "CE" else OptionType.PE

        greeks = Greeks(
            delta=round(data["delta"], 4),
            gamma=round(data["gamma"], 6),
            theta=round(data["theta"], 4),
            vega=round(data["vega"], 4),
            rho=0.0,
            iv=round(data["iv"], 4),
        )

        opt_data = OptionData.model_construct(
            tradingsymbol=symbol,
            instrument_token=token,
            strike=strike_dec,
            option_type=opt_type_enum,
            expiry=expiry,
            ltp=price_dec,
            bid_price=bid_dec,
            ask_price=ask_dec,
            volume=data["volume"],
            oi=data["oi"],
            greeks=greeks,
        )

        if opt_str == "CE":
            entry.ce = opt_data
        else:
            entry.pe = opt_data

        # Update feed and broker
        feed._latest_ticks[token] = Tick.model_construct(
            instrument_token=token,
            tradingsymbol=symbol,
            timestamp=now,
            ltp=price_dec,
            bid_price=bid_dec,
            ask_price=ask_dec,
            volume=data["volume"],
            oi=data["oi"],
            bid_qty=100, ask_qty=100,
            high=price_dec, low=price_dec,
            open=price_dec, close=price_dec,
        )
        broker.set_ltp(symbol, round(data["ltp"], 2))

    # Update portfolio LTPs
    for key, pos in portfolio._positions._positions.items():
        cached = feed._latest_ticks.get(pos.instrument_token)
        if cached:
            pos.ltp = cached.ltp
