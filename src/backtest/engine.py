"""Backtesting engine — replays synthetic data through any registered strategy.

Uses the same components as live trading (TickFeedManager, OptionChainBuilder,
PaperBrokerClient, PortfolioManager) but drives them synchronously via direct
method calls instead of the EventBus. Strategies run identically through the
same StrategyContext interface.

Usage:
    engine = BacktestEngine()
    results = await engine.run("short_straddle", num_days=30)
"""

import logging
import math
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import numpy as np
import pytz
from scipy.stats import norm as sp_norm

from src.backtest.metrics import calculate_metrics
from src.broker.paper.client import PaperBrokerClient
from src.core.clock import MarketClock
from src.core.constants import INDIA_VIX_TOKEN, LOT_SIZES, RISK_FREE_RATE
from src.core.events import EventBus
from src.core.models import Greeks, OptionData, Order, PnL, Tick
from src.core.types import OptionType, OrderStatus, ProductType
from src.market_data.aggregator import OHLCAggregator
from src.market_data.feed import TickFeedManager
from src.market_data.option_chain import OptionChainBuilder
from src.market_data.simulator import BANKNIFTY_SPOT_TOKEN, NIFTY_SPOT_TOKEN
from src.options.skew import ParametricSkew
from src.portfolio.charges import calculate_charges
from src.portfolio.manager import PortfolioManager
from src.strategy.context import StrategyContext
from src.strategy.registry import create_strategy

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

_TOKEN_BASE = 600_000
_SPOT_TOKENS = {"NIFTY": NIFTY_SPOT_TOKEN, "BANKNIFTY": BANKNIFTY_SPOT_TOKEN}
_STRIKE_STEPS = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50}
_DEFAULT_SPOTS = {"NIFTY": 22500.0, "BANKNIFTY": 48000.0}
_NUM_STRIKES = 30  # ±30 strikes from ATM (wider range for iron condor wings)


class BacktestClock(MarketClock):
    """Market clock returning simulated time for backtesting."""

    def __init__(self):
        super().__init__()
        self._sim_now: datetime | None = None
        self._expiry_override: dict[str, list[date]] = {}  # underlying -> available expiries

    def set_time(self, dt: datetime) -> None:
        self._sim_now = dt

    def set_expiries(self, underlying: str, expiries: list[date]) -> None:
        """Override the calendar-based expiry lookup with real listed expiries.

        Used when GDFL (or other real data) dictates which expiries exist,
        which can differ from the default Tuesday weekly cadence (e.g. due
        to holidays or unscheduled shifts).
        """
        self._expiry_override[underlying] = sorted(expiries)

    def now(self) -> datetime:
        if self._sim_now:
            return self._sim_now
        return super().now()

    def is_market_open(self) -> bool:
        return True  # Always open during backtest

    def next_expiry(self, underlying: str) -> date:
        override = self._expiry_override.get(underlying)
        if override:
            today = self.now().date()
            future = [e for e in override if e >= today]
            if future:
                return future[0]
        return super().next_expiry(underlying)


class BacktestEngine:
    """Replays synthetic market data through any registered strategy."""

    async def run(
        self,
        strategy_name: str,
        strategy_id: str = "",
        strategy_params: dict | None = None,
        num_days: int = 30,
        start_date: date | None = None,
        initial_capital: float = 1_000_000,
        seed: int = 42,
        tick_interval_minutes: int = 1,
        fat_tails: bool = False,
        market_source=None,
    ) -> dict:
        """Run a backtest with synthetic data (or real GDFL data if market_source given).

        Args:
            strategy_name: Registered strategy name (e.g., 'short_straddle').
            strategy_id: Unique ID (auto-generated if empty).
            strategy_params: Strategy parameter overrides.
            num_days: Number of trading days to simulate.
            start_date: First trading day (defaults to ~45 days ago).
            initial_capital: Starting capital.
            seed: Random seed for reproducibility.
            tick_interval_minutes: Minutes between ticks (1=accurate, 5=fast).
            fat_tails: Use Student-t(df=5) instead of Gaussian for returns (heavier tails).
            market_source: Optional GDFLMarketSource. When provided, real tick
                data drives the backtest instead of synthetic BS prices. Trading
                days come from the parquet index unless start_date is explicit.

        Returns:
            Dict with metrics, daily_results, equity_curve, trades.
        """
        strategy_id = strategy_id or f"{strategy_name}_bt"
        params = strategy_params or {}
        underlying = params.get("underlying", "NIFTY")
        start_date = start_date or (date.today() - timedelta(days=45))

        # Ensure strategy modules are registered
        _import_strategies()

        # ─── Create infrastructure ────────────────────────────────
        event_bus = EventBus()  # Not started — only for constructor params
        clock = BacktestClock()
        feed = TickFeedManager(event_bus)
        aggregator = OHLCAggregator(event_bus)
        chain_builder = OptionChainBuilder(event_bus, clock)

        # Quote provider for the paper broker (F2).
        # Only wired when we have a real market_source (GDFL). Returns
        # (bid, ask) for a tradingsymbol by reverse-looking-up the
        # instrument token via chain_builder's symbol_map and reading
        # the latest tick's bid/ask. When the paper broker sees
        # non-zero bid/ask it fills BUY @ ask and SELL @ bid, mirroring
        # how live Kite market orders cross the spread. For the
        # synthetic BS path we leave quote_provider=None so the tiered
        # SlippageModel (premium tier × time × VIX × size) is used as
        # before — BS bid/ask are cosmetic ±1% placeholders and not a
        # realistic fill model.
        quote_provider = None
        if market_source is not None:
            # Build reverse symbol → token map lazily; the chain
            # builder populates _symbol_map as options are registered.
            # Cache the inverse and refresh when we see a symbol we
            # don't know yet (new strikes appear daily in GDFL).
            _sym_to_token: dict[str, int] = {}

            def _resolve_token(tradingsymbol: str) -> int | None:
                tok = _sym_to_token.get(tradingsymbol)
                if tok is not None:
                    return tok
                for t, s in chain_builder._symbol_map.items():
                    if s == tradingsymbol:
                        _sym_to_token[tradingsymbol] = t
                        return t
                return None

            def _quote_provider(tradingsymbol: str) -> tuple[float | None, float | None]:
                tok = _resolve_token(tradingsymbol)
                if tok is None:
                    return None, None
                tick = feed._latest_ticks.get(tok)
                if tick is None:
                    return None, None
                bid = float(tick.bid_price) if tick.bid_price else 0.0
                ask = float(tick.ask_price) if tick.ask_price else 0.0
                if bid <= 0 or ask <= 0 or ask < bid:
                    return None, None
                return bid, ask

            quote_provider = _quote_provider

        broker = PaperBrokerClient(
            initial_capital=initial_capital,
            quote_provider=quote_provider,
        )
        portfolio = PortfolioManager(event_bus, broker, chain_builder)

        # IV skew — default NIFTY-typical. Refreshed daily from observed
        # chain IVs via `_fit_daily_skew` if chain has ≥5 valid IV points.
        skew_model: ParametricSkew = ParametricSkew.nifty_typical()

        await broker.connect()

        # ─── Create strategy ──────────────────────────────────────
        strategy = create_strategy(strategy_name, strategy_id, params)

        # ─── Trading days ─────────────────────────────────────────
        if market_source is not None:
            available = market_source.available_days()
            if not available:
                return {"error": "GDFL market_source has no available days"}
            if start_date is not None:
                available = [d for d in available if d >= start_date]
            trading_days = available[:num_days]
            if not trading_days:
                return {"error": "No GDFL days in requested range"}
        else:
            trading_days = _trading_days(start_date, num_days, clock)
            if not trading_days:
                return {"error": "No trading days in range"}

        # ─── Market setup ─────────────────────────────────────────
        spot_token = _SPOT_TOKENS.get(underlying, NIFTY_SPOT_TOKEN)
        step = _STRIKE_STEPS.get(underlying, 50)
        spot = _DEFAULT_SPOTS.get(underlying, 22500.0)
        rng = np.random.default_rng(seed)
        # Fat tails: Student-t(df=5) scaled to match normal variance
        _T_DF = 5
        _T_SCALE = math.sqrt((_T_DF - 2) / _T_DF)  # ~0.775
        def _rand(sigma: float) -> float:
            if fat_tails:
                return float(rng.standard_t(_T_DF)) * _T_SCALE * sigma
            return float(rng.normal(0, sigma))
        vix = 14.5

        chain_builder.register_spot(spot_token, underlying)

        # Token allocator
        next_token = [_TOKEN_BASE]
        option_tokens: dict[tuple[str, float, str], int] = {}

        def alloc_token(ul: str, strike: float, ot: str) -> int:
            key = (ul, strike, ot)
            if key not in option_tokens:
                option_tokens[key] = next_token[0]
                next_token[0] += 1
            return option_tokens[key]

        # Initial expiry and options
        clock.set_time(IST.localize(datetime.combine(trading_days[0], time(9, 15))))
        if market_source is not None:
            # Load day 1 now so we can register options with REAL expiries/strikes
            market_source.load_day(trading_days[0])
            option_tokens = market_source.register_options(chain_builder, alloc_token)
            expiries = market_source.expiries_for_day()
            clock.set_expiries(underlying, expiries)
            expiry = expiries[0] if expiries else clock.next_expiry(underlying)
        else:
            expiry = clock.next_expiry(underlying)
            _register_options(chain_builder, underlying, spot, step, _NUM_STRIKES, expiry, alloc_token)

        # ─── Wire order callback ──────────────────────────────────
        # F2: No pre-slippage here. The paper broker applies the single
        # slippage source (cross-spread if quote_provider returns a real
        # bid/ask, else SlippageModel). Using the fill price reported
        # back by the broker keeps entry/exit bookkeeping consistent
        # with what the broker actually booked.
        async def order_callback(signal_obj):
            orders = []
            for leg in signal_obj.legs:
                ltp = feed.get_ltp(leg.instrument_token)
                price = float(leg.price) if float(leg.price) > 0 else float(ltp or 0)
                if price <= 0:
                    continue

                # Keep broker's LTP cache in sync with the theoretical
                # mark so its fallback path has a value to work with.
                broker.set_ltp(leg.tradingsymbol, price)

                order_id = await broker.place_order(
                    tradingsymbol=leg.tradingsymbol,
                    exchange="NFO",
                    side=leg.order_side,
                    quantity=leg.quantity,
                    order_type=leg.order_type,
                    price=price,
                )

                # Read back the actual fill price the broker booked so
                # positions, P&L and charges all use the real post-
                # slippage number (not the pre-slippage LTP).
                fill_price = price
                if broker._trades:
                    last_trade = broker._trades[-1]
                    if last_trade.get("order_id") == order_id:
                        fill_price = float(last_trade.get("average_price", price))

                order = Order(
                    broker_order_id=order_id,
                    strategy_id=signal_obj.strategy_id,
                    instrument_token=leg.instrument_token,
                    tradingsymbol=leg.tradingsymbol,
                    order_side=leg.order_side,
                    order_type=leg.order_type,
                    product=ProductType.NRML,
                    quantity=leg.quantity,
                    fill_price=Decimal(str(round(fill_price, 2))),
                    fill_quantity=leg.quantity,
                    status=OrderStatus.FILLED,
                )
                portfolio._positions.update_from_fill(order)

                inst = "CE" if "CE" in leg.tradingsymbol else (
                    "PE" if "PE" in leg.tradingsymbol else "FUT"
                )
                charges = calculate_charges(
                    Decimal(str(round(fill_price, 2))), leg.quantity, leg.order_side, inst
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

        logger.info(
            f"[BACKTEST] Starting: strategy={strategy_name} underlying={underlying} "
            f"days={num_days} capital={initial_capital:,.0f}"
        )

        for day_idx, day in enumerate(trading_days):
            # Day boundary reset (except first day)
            if day_idx > 0:
                strategy.reset_day_state()
                portfolio.reset_daily()
                # Clear intraday candle builders so morning range is fresh
                aggregator._builders.clear()

            # Expiry rollover
            clock.set_time(IST.localize(datetime.combine(day, time(9, 15))))
            if market_source is not None:
                if day_idx > 0:  # day 0 already loaded above
                    market_source.load_day(day)
                    new_tokens = market_source.register_options(chain_builder, alloc_token)
                    option_tokens.update(new_tokens)
                exps = market_source.expiries_for_day()
                if exps:
                    clock.set_expiries(underlying, exps)
                    expiry = exps[0]
            else:
                new_expiry = clock.next_expiry(underlying)
                if new_expiry != expiry:
                    expiry = new_expiry
                    _register_options(
                        chain_builder, underlying, spot, step, _NUM_STRIKES, expiry, alloc_token
                    )

            daily_drift = _rand(0.003)
            day_open = spot
            day_trades_start = len(broker._trades)

            # Attempt to refit skew from yesterday's observed chain IVs.
            # Falls back to nifty_typical() if chain has <5 valid IVs.
            skew_model = _fit_daily_skew(chain_builder, underlying, expiry, spot, skew_model)

            # Minute-by-minute replay
            start_dt = datetime.combine(day, time(9, 15))

            for i in range(0, 375, tick_interval_minutes):
                now = IST.localize(start_dt + timedelta(minutes=i))
                clock.set_time(now)

                if market_source is not None:
                    # Real tick data path — market_source writes all caches
                    gdfl_spot, gdfl_vix = market_source.apply(
                        now, feed, broker, chain_builder, portfolio, option_tokens,
                    )
                    if gdfl_spot is None:
                        continue  # No data this minute (pre-open, holiday gap)
                    spot = gdfl_spot
                    vix = gdfl_vix if gdfl_vix is not None else vix
                else:
                    # Synthetic path — unchanged
                    vol_mult = 1.0 + 0.5 * (
                        math.exp(-i / 30) + math.exp(-(375 - i) / 30)
                    )
                    ret = daily_drift / 375 + _rand(0.0003) * vol_mult
                    spot *= (1 + ret)

                    vix += 0.02 * (14.5 - vix) + float(rng.normal(0, 0.15))
                    vix = max(8.0, min(40.0, vix))

                    T = max(
                        1 / (365 * 24),
                        (expiry - day).days / 365 - i / (375 * 365),
                    )
                    iv_base = vix / 100

                    _update_market(
                        feed, broker, chain_builder, portfolio,
                        underlying, expiry, spot, vix, now,
                        spot_token, step, _NUM_STRIKES, T, iv_base,
                        option_tokens, alloc_token,
                        skew_model=skew_model,
                    )

                # Dispatch to strategy
                spot_tick = Tick.model_construct(
                    instrument_token=spot_token,
                    tradingsymbol=underlying,
                    timestamp=now,
                    ltp=Decimal(str(round(spot, 2))),
                    volume=0, oi=0,
                    bid_price=Decimal("0"), ask_price=Decimal("0"),
                    bid_qty=0, ask_qty=0,
                    high=Decimal("0"), low=Decimal("0"),
                    open=Decimal("0"), close=Decimal("0"),
                )
                # Feed spot tick into candle aggregator so breakout / M5
                # indicators receive real OHLC data (else trend leg never fires).
                aggregator.process_tick_direct(spot_tick)

                try:
                    signal = await strategy.on_tick(spot_tick)
                    if signal:
                        await order_callback(signal)
                except Exception as e:
                    logger.debug(f"[BACKTEST] Strategy tick error: {e}")

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

            if day_idx % 5 == 0 or day_idx == len(trading_days) - 1:
                logger.info(
                    f"[BACKTEST] Day {day_idx + 1}/{num_days}: {day} "
                    f"spot={spot:.0f} pnl={float(day_pnl):+.0f} "
                    f"cumulative={float(running_pnl):+.0f} trades={day_trades}"
                )

        # ─── Finalize ─────────────────────────────────────────────
        await strategy.on_stop()

        pnl_values = [d["pnl"] for d in daily_results]
        metrics = calculate_metrics(pnl_values, broker._trades, initial_capital)
        metrics["total_charges"] = round(sum(d["charges"] for d in daily_results), 2)
        metrics["num_days"] = num_days

        result = {
            "strategy": strategy_name,
            "strategy_id": strategy_id,
            "underlying": underlying,
            "period": f"{trading_days[0]} to {trading_days[-1]}",
            "num_days": num_days,
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
            f"[BACKTEST] Complete: {strategy_name} {num_days}d "
            f"P&L={float(running_pnl):+,.0f} "
            f"Sharpe={metrics.get('sharpe_ratio', 0):.2f} "
            f"WinRate={metrics.get('win_rate', 0):.1f}%"
        )

        return result


# ─── Module-level helpers (avoid instance method overhead) ────────


def _import_strategies():
    """Import strategy modules to trigger @register_strategy decorators."""
    import src.strategy.implementations.short_straddle  # noqa: F401
    import src.strategy.implementations.short_strangle  # noqa: F401
    try:
        import src.strategy.implementations.iron_condor  # noqa: F401
        import src.strategy.implementations.delta_neutral  # noqa: F401
        import src.strategy.implementations.trend_debit_spread  # noqa: F401
        import src.strategy.implementations.portfolio_strategy  # noqa: F401
    except ImportError:
        pass


def _trading_days(start: date, num_days: int, clock: MarketClock) -> list[date]:
    """Generate a list of trading days starting from `start`."""
    days: list[date] = []
    current = start
    while len(days) < num_days:
        if current.weekday() < 5 and not clock.is_trading_holiday(current):
            days.append(current)
        current += timedelta(days=1)
    return days


def _register_options(chain_builder, underlying, spot, step, num_strikes, expiry, alloc_token):
    """Register option instruments in the chain builder."""
    atm = round(spot / step) * step
    for i in range(-num_strikes, num_strikes + 1):
        strike = atm + i * step
        strike_dec = Decimal(str(strike))
        for opt_type in (OptionType.CE, OptionType.PE):
            token = alloc_token(underlying, strike, opt_type.value)
            expiry_str = expiry.strftime("%y%b").upper()
            symbol = f"{underlying}{expiry_str}{int(strike)}{opt_type.value}"
            chain_builder.register_option(
                token, underlying, expiry, strike_dec, opt_type, symbol,
            )


_FUT_TOKEN = 500_000  # Fixed token for synthetic futures


def _fit_daily_skew(
    chain_builder,
    underlying: str,
    expiry: date,
    spot: float,
    fallback: "ParametricSkew",
) -> "ParametricSkew":
    """Fit `ParametricSkew` from the current chain's observed IVs.

    The backtest currently synthesises IVs from a flat ATM and the skew
    itself, so fitting from synthetic data is a no-op (recovers fallback).
    The hook is here for the GDFL/replay paths which feed real observed
    IVs into the chain builder — they will get a per-day calibrated skew.

    Falls back to `fallback` (or `nifty_typical`) if the chain has fewer
    than 5 valid OTM strike IVs.
    """
    try:
        chain = chain_builder.get_chain(underlying, expiry)
    except Exception:
        return fallback or ParametricSkew.nifty_typical()
    if chain is None:
        return fallback or ParametricSkew.nifty_typical()

    strikes: list[float] = []
    ivs: list[float] = []
    atm_iv_samples: list[float] = []
    atm_strike = float(chain.atm_strike) if chain.atm_strike else spot
    for entry in getattr(chain, "strikes", []):
        try:
            k = float(entry.strike)
        except Exception:
            continue
        for opt in (entry.ce, entry.pe):
            if opt is None or not getattr(opt, "greeks", None):
                continue
            iv = float(opt.greeks.iv or 0.0)
            if iv <= 0:
                continue
            strikes.append(k)
            ivs.append(iv)
            if abs(k - atm_strike) < 1e-6:
                atm_iv_samples.append(iv)

    if not strikes:
        return fallback or ParametricSkew.nifty_typical()
    atm_iv = (
        sum(atm_iv_samples) / len(atm_iv_samples)
        if atm_iv_samples
        else sum(ivs) / len(ivs)
    )
    return ParametricSkew.fit_from_observed(
        strikes=strikes,
        ivs=ivs,
        atm_iv=atm_iv,
        spot=spot,
    )


def _update_market(
    feed, broker, chain_builder, portfolio,
    underlying, expiry, spot, vix, now,
    spot_token, step, num_strikes, T, iv_base,
    option_tokens, alloc_token,
    *,
    skew_model: "ParametricSkew | None" = None,
):
    """Vectorized market update — batch BS + Greeks via numpy.

    Directly populates feed cache, broker LTP, option chain entries,
    and portfolio position LTPs. No EventBus involved.

    Args:
        skew_model: Optional `ParametricSkew` for per-strike IV. Defaults
            to `ParametricSkew.nifty_typical()` if None (same shape as the
            legacy `1 + 8m^2 - 3m` formula, so the arg is a drop-in).
    """
    if skew_model is None:
        skew_model = ParametricSkew.nifty_typical()
    spot_dec = Decimal(str(round(spot, 2)))
    r = RISK_FREE_RATE

    # ─── Spot price ───────────────────────────────────────────
    chain_builder._spot_prices[underlying] = spot_dec
    feed._latest_ticks[spot_token] = Tick.model_construct(
        instrument_token=spot_token,
        tradingsymbol=underlying,
        timestamp=now,
        ltp=spot_dec,
        volume=0, oi=0,
        bid_price=Decimal("0"), ask_price=Decimal("0"),
        bid_qty=0, ask_qty=0,
        high=Decimal("0"), low=Decimal("0"),
        open=Decimal("0"), close=Decimal("0"),
    )
    broker.set_ltp(underlying, round(spot, 2))

    # ─── Synthetic futures (for delta_neutral hedging) ──────
    month_map = {
        1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
        7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
    }
    yy = now.year % 100
    mmm = month_map[now.month]
    fut_symbol = f"{underlying}{yy}{mmm}FUT"
    fut_price = round(spot * (1 + RISK_FREE_RATE * T), 2)
    fut_dec = Decimal(str(fut_price))
    feed._latest_ticks[_FUT_TOKEN] = Tick.model_construct(
        instrument_token=_FUT_TOKEN,
        tradingsymbol=fut_symbol,
        timestamp=now,
        ltp=fut_dec,
        volume=0, oi=0,
        bid_price=fut_dec, ask_price=fut_dec,
        bid_qty=0, ask_qty=0,
        high=fut_dec, low=fut_dec,
        open=fut_dec, close=fut_dec,
    )
    broker.set_ltp(fut_symbol, fut_price)

    # ─── VIX ──────────────────────────────────────────────────
    vix_dec = Decimal(str(round(vix, 2)))
    feed._latest_ticks[INDIA_VIX_TOKEN] = Tick.model_construct(
        instrument_token=INDIA_VIX_TOKEN,
        tradingsymbol="INDIA VIX",
        timestamp=now,
        ltp=vix_dec,
        volume=0, oi=0,
        bid_price=Decimal("0"), ask_price=Decimal("0"),
        bid_qty=0, ask_qty=0,
        high=Decimal("0"), low=Decimal("0"),
        open=Decimal("0"), close=Decimal("0"),
    )

    # ─── Option chain (vectorized) ────────────────────────────
    chain = chain_builder.get_chain(underlying, expiry)
    if not chain:
        return

    chain.spot_price = spot_dec
    atm = round(spot / step) * step
    chain.atm_strike = Decimal(str(atm))

    # Build strike array
    n = 2 * num_strikes + 1
    strikes_arr = np.empty(n)
    for idx in range(n):
        strikes_arr[idx] = atm + (idx - num_strikes) * step

    # Vectorized BS pricing for all strikes at once.
    # Per-strike IV from parametric skew (iv_atm anchor = iv_base).
    S_arr = np.full(n, spot)
    T_safe = max(T, 1e-10)
    atm_iv = max(iv_base, 1e-10)
    sigma_arr = np.maximum(skew_model.apply_vec(atm_iv, strikes_arr, spot), 1e-10)
    sqrt_T = math.sqrt(T_safe)
    exp_rT = math.exp(-r * T_safe)

    d1_arr = (np.log(S_arr / strikes_arr) + (r + 0.5 * sigma_arr ** 2) * T_safe) / (
        sigma_arr * sqrt_T
    )
    d2_arr = d1_arr - sigma_arr * sqrt_T
    N_d1 = sp_norm.cdf(d1_arr)
    N_d2 = sp_norm.cdf(d2_arr)
    N_neg_d1 = 1.0 - N_d1
    N_neg_d2 = 1.0 - N_d2
    n_d1_pdf = sp_norm.pdf(d1_arr)

    ce_prices_arr = np.maximum(0.05, S_arr * N_d1 - strikes_arr * exp_rT * N_d2)
    pe_prices_arr = np.maximum(0.05, strikes_arr * exp_rT * N_neg_d2 - S_arr * N_neg_d1)

    # Vectorized Greeks — no gamma cap (see src/options/greeks.py).
    gamma_denom = S_arr * sigma_arr * sqrt_T
    gamma_arr = np.where(
        gamma_denom < 1e-8, np.inf, n_d1_pdf / np.where(gamma_denom < 1e-8, 1.0, gamma_denom)
    )
    common_theta = -(S_arr * n_d1_pdf * sigma_arr) / (2 * sqrt_T)
    ce_theta_arr = (common_theta - r * strikes_arr * exp_rT * N_d2) / 365
    pe_theta_arr = (common_theta + r * strikes_arr * exp_rT * N_neg_d2) / 365
    vega_arr = S_arr * n_d1_pdf * sqrt_T / 100
    ce_rho_arr = strikes_arr * T_safe * exp_rT * N_d2 / 100
    pe_rho_arr = -strikes_arr * T_safe * exp_rT * N_neg_d2 / 100

    # Synthetic OI (vectorized)
    distance_arr = np.abs(strikes_arr - spot) / spot if spot > 0 else np.zeros(n)
    oi_arr = np.maximum(1000, 50000 * np.exp(-distance_arr * 30)).astype(int)

    # Expiry string (computed once)
    exp_str = expiry.strftime("%y%b").upper()
    _ZERO = Decimal("0")
    _SPREAD_MIN = Decimal("0.05")
    _SPREAD_FACTOR = Decimal("0.01")

    # Populate chain entries from vectorized results
    for idx in range(n):
        strike = float(strikes_arr[idx])
        strike_dec = Decimal(str(int(strike))) if strike == int(strike) else Decimal(str(strike))

        entry = chain_builder._find_or_create_entry(chain, strike_dec)
        oi_val = int(oi_arr[idx])

        for opt_str, price_val, delta_val, theta_val, rho_val in (
            ("CE", float(ce_prices_arr[idx]), float(N_d1[idx]), float(ce_theta_arr[idx]), float(ce_rho_arr[idx])),
            ("PE", float(pe_prices_arr[idx]), float(N_d1[idx] - 1), float(pe_theta_arr[idx]), float(pe_rho_arr[idx])),
        ):
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
            price_dec = Decimal(str(round(price_val, 2)))
            spread = max(_SPREAD_MIN, price_dec * _SPREAD_FACTOR)
            bid = max(_SPREAD_MIN, price_dec - spread)
            ask = price_dec + spread
            opt_type_enum = OptionType.CE if opt_str == "CE" else OptionType.PE

            gamma_val = float(gamma_arr[idx])
            greeks = Greeks(
                delta=round(delta_val, 4),
                gamma=gamma_val if math.isinf(gamma_val) else round(gamma_val, 6),
                theta=round(theta_val, 4),
                vega=round(float(vega_arr[idx]), 4),
                rho=round(rho_val, 4),
                iv=round(float(sigma_arr[idx]), 4),
            )

            opt_data = OptionData.model_construct(
                tradingsymbol=symbol,
                instrument_token=token,
                strike=strike_dec,
                option_type=opt_type_enum,
                expiry=expiry,
                ltp=price_dec,
                bid_price=bid,
                ask_price=ask,
                volume=oi_val // 3,
                oi=oi_val,
                greeks=greeks,
            )

            if opt_str == "CE":
                entry.ce = opt_data
            else:
                entry.pe = opt_data

            feed._latest_ticks[token] = Tick.model_construct(
                instrument_token=token,
                tradingsymbol=symbol,
                timestamp=now,
                ltp=price_dec,
                bid_price=bid,
                ask_price=ask,
                volume=oi_val // 3,
                oi=oi_val,
                bid_qty=100, ask_qty=100,
                high=price_dec, low=price_dec,
                open=price_dec, close=price_dec,
            )
            broker.set_ltp(symbol, round(price_val, 2))

    # Chain aggregates
    chain.total_ce_oi = sum(e.ce.oi for e in chain.strikes if e.ce)
    chain.total_pe_oi = sum(e.pe.oi for e in chain.strikes if e.pe)
    if chain.total_ce_oi > 0:
        chain.pcr_oi = chain.total_pe_oi / chain.total_ce_oi
    from src.options.chain_analyzer import compute_max_pain
    chain.max_pain = compute_max_pain(chain)
    chain.updated_at = now

    # Update portfolio LTPs for open positions
    for key, pos in portfolio._positions._positions.items():
        cached = feed._latest_ticks.get(pos.instrument_token)
        if cached:
            pos.ltp = cached.ltp
