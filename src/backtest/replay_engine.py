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


def _load_chain_snapshots(
    snapshot_dir: Path,
    trading_days: list[date],
    underlying: str | None = None,
    skip_degraded: bool = True,
    accept_partial: bool = True,
) -> tuple[dict, dict]:
    """Load chain snapshot CSVs for the given trading days.

    Args:
        snapshot_dir: Directory of chain_YYYY-MM-DD.csv files.
        trading_days: Days to load.
        underlying: If set, only rows whose `underlying` column matches are
            loaded. Required when the chain CSVs mix multiple underlyings
            (the recorder writes NIFTY+BANKNIFTY into the same file by date).
            If None, loads everything (legacy behavior).
        skip_degraded: When True (default), days classified as `degraded` or
            `weekend` by `audit_chain_quality.audit_file` are skipped. These
            days produce wrong P&L if replayed (Apr 17 audit found 6 weekend
            days and 1 zero-IV day silently corrupting a 23-day replay).
        accept_partial: When True (default), `partial` days (LTP only, no
            bid/ask) are loaded with a warning. The replay engine compensates
            by routing fills through LTP-only logic. Set False to gate them
            out entirely.

    Returns:
        (snapshots, quality_report) where snapshots is the chain map
        {date: {minute_key: {(strike, option_type): row_dict}}} and
        quality_report is {date: status_string}. The replay engine uses the
        report to surface skipped/degraded days in its result dict.
    """
    # Local import to avoid a heavy module load when this function is unused
    from scripts.audit_chain_quality import audit_file

    snapshots: dict[date, dict[str, dict[tuple[float, str], dict]]] = {}
    quality_report: dict[date, str] = {}
    bid_gt_ask_count = 0

    for day in trading_days:
        filepath = snapshot_dir / f"chain_{day.isoformat()}.csv"
        if not filepath.exists():
            quality_report[day] = "missing"
            continue

        # Quality gate (Apr 17 audit): refuse to replay degraded / weekend
        # days unless the caller explicitly opts in.
        rep = audit_file(filepath)
        quality_report[day] = rep.status
        if rep.status == "weekend":
            logger.warning(
                f"[REPLAY_BACKTEST] SKIP {day} ({rep.weekday}): weekend file "
                f"(should already be in _quarantine — check recorder gate)"
            )
            continue
        if rep.status == "degraded" and skip_degraded:
            logger.warning(
                f"[REPLAY_BACKTEST] SKIP {day} ({rep.weekday}): DEGRADED "
                f"(price={rep.priceable_pct:.0f}% iv={rep.iv_pct:.0f}% "
                f"bidask={rep.bidask_pct:.0f}%)"
            )
            continue
        if rep.status == "partial":
            if not accept_partial:
                logger.warning(
                    f"[REPLAY_BACKTEST] SKIP {day} ({rep.weekday}): PARTIAL "
                    f"and accept_partial=False"
                )
                continue
            logger.warning(
                f"[REPLAY_BACKTEST] PARTIAL {day} ({rep.weekday}): bidask=0% — "
                f"fills will use LTP only"
            )

        day_data: dict[str, dict[tuple[float, str], dict]] = defaultdict(dict)
        with open(filepath) as f:
            reader = csv.DictReader(f)
            for row in reader:
                if underlying and row.get("underlying") != underlying:
                    continue
                dt = datetime.fromisoformat(row["time"])
                minute_key = dt.strftime("%H:%M")
                strike = float(row["strike"])
                opt_type = row["option_type"]
                bid = float(row.get("bid_price", 0) or 0)
                ask = float(row.get("ask_price", 0) or 0)

                # Sanity assertion: bid <= ask. A crossed book (bid > ask)
                # is broker junk. We don't drop the row (LTP may still be
                # usable) but we count it for the post-run report.
                if bid > 0 and ask > 0 and bid > ask:
                    bid_gt_ask_count += 1

                day_data[minute_key][(strike, opt_type)] = {
                    "ltp": float(row["ltp"]),
                    "iv": float(row.get("iv", 0)),
                    "delta": float(row.get("delta", 0)),
                    "gamma": float(row.get("gamma", 0)),
                    "theta": float(row.get("theta", 0)),
                    "vega": float(row.get("vega", 0)),
                    "oi": int(row.get("oi", 0)),
                    "volume": int(row.get("volume", 0)),
                    "bid_price": bid,
                    "ask_price": ask,
                }

        if day_data:
            snapshots[day] = dict(day_data)

    if bid_gt_ask_count > 0:
        logger.warning(
            f"[REPLAY_BACKTEST] {bid_gt_ask_count} crossed-book rows (bid > ask) "
            f"loaded — these will be skipped by the price-resolution helper"
        )

    return snapshots, quality_report


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
        skip_degraded: bool = True,
        accept_partial: bool = True,
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
            skip_degraded: Skip days flagged `degraded` by audit_chain_quality
                (Apr 17 audit default — prevents the kind of zero-IV / zero
                bid-ask contamination that produced bogus 23-day P&L).
            accept_partial: Allow `partial` days (LTP only, no bid/ask depth).
                Set False for the strictest run.

        Returns:
            Dict with metrics, daily_results, equity_curve, plus
            `chain_quality` (per-day status) and `skipped_days` (list).
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

        # Load chain snapshots — filter by underlying so e.g. BANKNIFTY rows
        # in the same file don't pollute NIFTY strike selection. The quality
        # gate skips degraded/weekend days inside the loader.
        chain_snaps, quality_report = _load_chain_snapshots(
            snapshot_dir, trading_days,
            underlying=underlying,
            skip_degraded=skip_degraded,
            accept_partial=accept_partial,
        )
        days_with_snapshots = len(chain_snaps)
        total_snapshot_points = sum(len(v) for v in chain_snaps.values())
        skipped_days = sorted(
            d for d, status in quality_report.items()
            if status in ("weekend", "missing")
            or (status == "degraded" and skip_degraded)
            or (status == "partial" and not accept_partial)
        )
        # Drop skipped days from the trading_days list so the engine doesn't
        # try to step through them and produce phantom no-trade days.
        trading_days = [d for d in trading_days if d not in skipped_days]

        logger.info(
            f"[REPLAY_BACKTEST] Loaded {len(trading_days)} days, "
            f"{days_with_snapshots} with snapshots ({total_snapshot_points} timepoints) — "
            f"skipped {len(skipped_days)} days for quality"
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

        # May 1 2026 fix: key includes ``expiry`` so the same strike
        # across multiple expiries gets distinct tokens. See engine.py
        # for the full bug rationale.
        next_token = [_TOKEN_BASE]
        option_tokens: dict[tuple[str, float, str, date], int] = {}

        def alloc_token(ul: str, strike: float, ot: str, expiry: date) -> int:
            key = (ul, strike, ot, expiry)
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
        # Realistic-fill rule: cross the spread.
        #   BUY  → fill at ASK   (you take the offer)
        #   SELL → fill at BID   (you hit the bid)
        # Falls back to LTP + 5bps simulator slippage if bid/ask aren't usable
        # (zero or inverted — happens when the underlying recorder had no quote).
        # This is the dominant friction in Indian options. ATM spreads are
        # 30-100 bps; OTM wings are 200-500 bps. The previous flat 5 bps
        # understated real fills by ~10x for OTM trades.
        from src.core.types import OrderSide as _OS
        fill_method_counts = {"bid_ask": 0, "ltp_slip": 0}

        async def order_callback(signal_obj):
            orders = []
            for leg in signal_obj.legs:
                tick = feed.get_tick(leg.instrument_token)
                ltp = float(tick.ltp) if tick and tick.ltp else 0
                bid = float(tick.bid_price) if tick and tick.bid_price else 0
                ask = float(tick.ask_price) if tick and tick.ask_price else 0

                if bid > 0 and ask > 0 and bid < ask:
                    # Cross the spread — realistic for market orders
                    price = ask if leg.order_side == _OS.BUY else bid
                    fill_method_counts["bid_ask"] += 1
                else:
                    base = float(leg.price) if float(leg.price) > 0 else ltp
                    if base <= 0:
                        continue
                    price = fill_sim.simulate_fill(base, leg.order_side)
                    fill_method_counts["ltp_slip"] += 1

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
                # May 7 2026: pass simulated trade date so STT picks the
                # correct rate (0.10% pre-Apr-2026 vs 0.15% after).
                charges = calculate_charges(
                    Decimal(str(round(price, 2))), leg.quantity, leg.order_side, inst,
                    trade_date=clock.now().date(),
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
        metrics["fills_via_bid_ask"] = fill_method_counts["bid_ask"]
        metrics["fills_via_ltp_slip"] = fill_method_counts["ltp_slip"]
        total_fills = fill_method_counts["bid_ask"] + fill_method_counts["ltp_slip"]
        metrics["bid_ask_fill_pct"] = round(
            100 * fill_method_counts["bid_ask"] / max(1, total_fills), 1
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
            # Apr 17 audit: surface data-quality decisions in the result so
            # downstream tooling can warn / refuse to publish results from
            # contaminated runs.
            "chain_quality": {d.isoformat(): s for d, s in quality_report.items()},
            "skipped_days": [d.isoformat() for d in skipped_days],
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
    # Start with BS pricing as baseline (covers all strikes).
    # day_open is consumed by the caller (replay_engine main loop) for
    # the daily_results record but not used by _update_market itself.
    _update_market(
        feed, broker, chain_builder, portfolio,
        underlying, expiry, spot, vix, now,
        spot_token, step, num_strikes, T, iv_base,
        option_tokens, alloc_token,
    )

    # Overwrite with real recorded data where available
    chain = chain_builder.get_chain(underlying, expiry)
    if not chain:
        return

    # May 1 2026 fix part 2: full date in symbol — see common.py.
    exp_str = expiry.strftime("%y%b%d").upper()

    for (strike, opt_str), data in snap_data.items():
        strike_dec = Decimal(str(int(strike))) if strike == int(strike) else Decimal(str(strike))

        entry = chain_builder._find_or_create_entry(chain, strike_dec)

        # May 1 2026 fix: 4-tuple key (with expiry) so same-strike across
        # multiple expiries doesn't collide on a single token.
        key = (underlying, strike, opt_str, expiry)
        if key not in option_tokens:
            token = alloc_token(underlying, strike, opt_str, expiry)
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

    # ─── Recompute chain aggregates strictly over THIS MINUTE'S data ────────
    # Two separate sources of pollution would otherwise leak into pcr_oi:
    #   (1) _update_market (called above) sets pcr_oi from BS-baseline OI
    #       (CE==PE per strike → PCR=1.0). Real recorded OI overwrites
    #       individual entries below, but the aggregate stays stale.
    #   (2) chain.strikes accumulates entries across minutes. Strikes that
    #       were recorded in a prior minute but NOT in the current snap_data
    #       still carry their stale real-OI values (in the millions). Naïve
    #       sum-over-all-strikes mixes current real OI with historical real
    #       OI plus current BS OI → produces PCR in the 4-17 range (Apr 18
    #       audit: 805/950 portfolio_replay decisions).
    # Fix: compute PCR strictly over strikes that appear in *this minute's*
    # snapshot. Those entries hold the freshly-overwritten real OI; everything
    # else is residue from other minutes and must be excluded.
    recorded_strikes = {s for s, _ in snap_data.keys()}
    fresh = [
        e for e in chain.strikes
        if (float(e.strike) in recorded_strikes
            or int(float(e.strike)) in {int(s) for s in recorded_strikes})
    ]
    chain.total_ce_oi = sum(e.ce.oi for e in fresh if e.ce)
    chain.total_pe_oi = sum(e.pe.oi for e in fresh if e.pe)
    if chain.total_ce_oi > 0:
        chain.pcr_oi = chain.total_pe_oi / chain.total_ce_oi
    else:
        chain.pcr_oi = 0.0
    # Volume + max_pain use the same restricted set for consistency. We can't
    # call compute_pcr_volume / compute_max_pain (they iterate chain.strikes
    # unconditionally) so inline the restricted sums instead.
    ce_vol = sum(e.ce.volume for e in fresh if e.ce)
    pe_vol = sum(e.pe.volume for e in fresh if e.pe)
    chain.pcr_volume = (pe_vol / ce_vol) if ce_vol > 0 else 0.0
    # Max-pain over fresh strikes only — same logic as compute_max_pain
    # but iterating the restricted set.
    if fresh:
        min_pain = None
        max_pain_strike = fresh[0].strike
        for target in fresh:
            tot = Decimal("0")
            for e in fresh:
                if e.ce and target.strike > e.strike:
                    tot += (target.strike - e.strike) * e.ce.oi
                if e.pe and target.strike < e.strike:
                    tot += (e.strike - target.strike) * e.pe.oi
            if min_pain is None or tot < min_pain:
                min_pain = tot
                max_pain_strike = target.strike
        chain.max_pain = max_pain_strike
    chain.updated_at = now

    # Update portfolio LTPs
    for key, pos in portfolio._positions._positions.items():
        cached = feed._latest_ticks.get(pos.instrument_token)
        if cached:
            pos.ltp = cached.ltp
