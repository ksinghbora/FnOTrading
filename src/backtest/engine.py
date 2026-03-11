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

from src.backtest.metrics import calculate_metrics
from src.backtest.simulator import FillSimulator
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
from src.options.greeks import compute_greeks
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

    def set_time(self, dt: datetime) -> None:
        self._sim_now = dt

    def now(self) -> datetime:
        if self._sim_now:
            return self._sim_now
        return super().now()

    def is_market_open(self) -> bool:
        return True  # Always open during backtest


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
    ) -> dict:
        """Run a backtest with synthetic data.

        Args:
            strategy_name: Registered strategy name (e.g., 'short_straddle').
            strategy_id: Unique ID (auto-generated if empty).
            strategy_params: Strategy parameter overrides.
            num_days: Number of trading days to simulate.
            start_date: First trading day (defaults to ~45 days ago).
            initial_capital: Starting capital.
            seed: Random seed for reproducibility.
            tick_interval_minutes: Minutes between ticks (1=accurate, 5=fast).

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
        broker = PaperBrokerClient(initial_capital=initial_capital)
        fill_sim = FillSimulator()
        feed = TickFeedManager(event_bus)
        aggregator = OHLCAggregator(event_bus)
        chain_builder = OptionChainBuilder(event_bus, clock)
        portfolio = PortfolioManager(event_bus, broker, chain_builder)

        await broker.connect()

        # ─── Create strategy ──────────────────────────────────────
        strategy = create_strategy(strategy_name, strategy_id, params)

        # ─── Trading days ─────────────────────────────────────────
        trading_days = _trading_days(start_date, num_days, clock)
        if not trading_days:
            return {"error": "No trading days in range"}

        # ─── Market setup ─────────────────────────────────────────
        spot_token = _SPOT_TOKENS.get(underlying, NIFTY_SPOT_TOKEN)
        step = _STRIKE_STEPS.get(underlying, 50)
        spot = _DEFAULT_SPOTS.get(underlying, 22500.0)
        rng = np.random.default_rng(seed)
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

        logger.info(
            f"[BACKTEST] Starting: strategy={strategy_name} underlying={underlying} "
            f"days={num_days} capital={initial_capital:,.0f}"
        )

        for day_idx, day in enumerate(trading_days):
            # Day boundary reset (except first day)
            if day_idx > 0:
                strategy.reset_day_state()
                portfolio.reset_daily()

            # Expiry rollover
            clock.set_time(IST.localize(datetime.combine(day, time(9, 15))))
            new_expiry = clock.next_expiry(underlying)
            if new_expiry != expiry:
                expiry = new_expiry
                _register_options(
                    chain_builder, underlying, spot, step, _NUM_STRIKES, expiry, alloc_token
                )

            daily_drift = float(rng.normal(0, 0.003))
            day_open = spot
            day_trades_start = len(broker._trades)

            # Minute-by-minute replay
            start_dt = datetime.combine(day, time(9, 15))

            for i in range(0, 375, tick_interval_minutes):
                now = IST.localize(start_dt + timedelta(minutes=i))
                clock.set_time(now)

                # GBM walk for spot
                vol_mult = 1.0 + 0.5 * (
                    math.exp(-i / 30) + math.exp(-(375 - i) / 30)
                )
                ret = daily_drift / 375 + float(rng.normal(0, 0.0003)) * vol_mult
                spot *= (1 + ret)

                # OU walk for VIX
                vix += 0.02 * (14.5 - vix) + float(rng.normal(0, 0.15))
                vix = max(8.0, min(40.0, vix))

                # Time to expiry (decays intraday)
                T = max(
                    1 / (365 * 24),
                    (expiry - day).days / 365 - i / (375 * 365),
                )
                iv_base = vix / 100

                # Update all market data
                _update_market(
                    feed, broker, chain_builder, portfolio,
                    underlying, expiry, spot, vix, now,
                    spot_token, step, _NUM_STRIKES, T, iv_base,
                    option_tokens, alloc_token,
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


def _update_market(
    feed, broker, chain_builder, portfolio,
    underlying, expiry, spot, vix, now,
    spot_token, step, num_strikes, T, iv_base,
    option_tokens, alloc_token,
):
    """Update all market data for one simulated minute.

    Directly populates feed cache, broker LTP, option chain entries,
    and portfolio position LTPs. No EventBus involved.
    """
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
    fut_price = round(spot * (1 + RISK_FREE_RATE * T), 2)  # cost-of-carry
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

    # ─── Option chain ─────────────────────────────────────────
    chain = chain_builder.get_chain(underlying, expiry)
    if not chain:
        return

    chain.spot_price = spot_dec
    atm = round(spot / step) * step
    chain.atm_strike = Decimal(str(atm))

    for i in range(-num_strikes, num_strikes + 1):
        strike = atm + i * step
        strike_dec = Decimal(str(strike))

        entry = chain_builder._find_or_create_entry(chain, strike_dec)

        for opt_str in ("CE", "PE"):
            key = (underlying, strike, opt_str)
            if key not in option_tokens:
                # Dynamically register if ATM shifted
                token = alloc_token(underlying, strike, opt_str)
                opt_type_enum = OptionType.CE if opt_str == "CE" else OptionType.PE
                exp_str = expiry.strftime("%y%b").upper()
                sym = f"{underlying}{exp_str}{int(strike)}{opt_str}"
                chain_builder.register_option(
                    token, underlying, expiry, strike_dec, opt_type_enum, sym,
                )
            else:
                token = option_tokens[key]

            exp_str = expiry.strftime("%y%b").upper()
            symbol = f"{underlying}{exp_str}{int(strike)}{opt_str}"

            # BS price
            price = _bs_price(spot, strike, T, r, iv_base, opt_str)
            price = max(0.05, price)
            price_dec = Decimal(str(round(price, 2)))

            # Greeks (direct computation — no IV solver needed)
            greeks = compute_greeks(spot, strike, T, r, iv_base, opt_str)

            # Synthetic OI (higher near ATM)
            distance = abs(strike - spot) / spot if spot > 0 else 0
            oi = int(max(1000, 50000 * math.exp(-distance * 30)))

            spread = max(Decimal("0.05"), price_dec * Decimal("0.01"))
            opt_type_enum = OptionType.CE if opt_str == "CE" else OptionType.PE

            opt_data = OptionData.model_construct(
                tradingsymbol=symbol,
                instrument_token=token,
                strike=strike_dec,
                option_type=opt_type_enum,
                expiry=expiry,
                ltp=price_dec,
                bid_price=max(Decimal("0.05"), price_dec - spread),
                ask_price=price_dec + spread,
                volume=oi // 3,
                oi=oi,
                greeks=greeks,
            )

            if opt_str == "CE":
                entry.ce = opt_data
            else:
                entry.pe = opt_data

            # Feed cache
            feed._latest_ticks[token] = Tick.model_construct(
                instrument_token=token,
                tradingsymbol=symbol,
                timestamp=now,
                ltp=price_dec,
                bid_price=max(Decimal("0.05"), price_dec - spread),
                ask_price=price_dec + spread,
                volume=oi // 3,
                oi=oi,
                bid_qty=100, ask_qty=100,
                high=price_dec, low=price_dec,
                open=price_dec, close=price_dec,
            )
            broker.set_ltp(symbol, round(price, 2))

    # Chain aggregates
    chain.total_ce_oi = sum(e.ce.oi for e in chain.strikes if e.ce)
    chain.total_pe_oi = sum(e.pe.oi for e in chain.strikes if e.pe)
    if chain.total_ce_oi > 0:
        chain.pcr_oi = chain.total_pe_oi / chain.total_ce_oi
    chain.updated_at = now

    # Update portfolio LTPs for open positions
    for key, pos in portfolio._positions._positions.items():
        cached = feed._latest_ticks.get(pos.instrument_token)
        if cached:
            pos.ltp = cached.ltp


def _bs_price(S: float, K: float, T: float, r: float, sigma: float, opt_type: str) -> float:
    """Black-Scholes option price."""
    if sigma <= 0 or T <= 0:
        return max(0.0, S - K) if opt_type == "CE" else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt_type == "CE":
        return max(0.0, S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2))
    return max(0.0, K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1))


def _norm_cdf(x: float) -> float:
    """Standard normal CDF (Abramowitz & Stegun)."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))
