"""Delta Neutral strategy — maintains delta-neutral position with futures hedging.

Opens a short straddle or strangle as the base position, then continuously
monitors portfolio delta. When abs(delta) exceeds a threshold, hedges with
futures to bring net delta back towards zero.
"""

import logging
from datetime import date, datetime
from decimal import Decimal

from src.core.constants import LOT_SIZES
from src.core.models import Order, Signal, SignalLeg, Subscription, Tick
from src.core.types import OrderSide, OrderStatus, OrderType
from src.strategy.base import BaseStrategy
from src.strategy.params import DeltaNeutralParams
from src.strategy.registry import register_strategy
from src.strategy.signals import adjust_signal, entry_signal, exit_signal, make_leg

logger = logging.getLogger(__name__)


@register_strategy("delta_neutral", DeltaNeutralParams)
class DeltaNeutralStrategy(BaseStrategy):
    """Opens a delta-neutral option position and hedges with futures.

    Features:
    - Base position: short straddle (ATM CE + PE) or strangle (OTM by delta)
    - Monitors portfolio delta in real-time
    - Hedges with futures when abs(delta) exceeds threshold
    - Configurable rebalance interval to avoid over-trading
    - Tracks cumulative hedge lots for state persistence
    """

    params: DeltaNeutralParams

    def __init__(self, strategy_id: str, params: DeltaNeutralParams):
        super().__init__(strategy_id, params)
        self._entered = False
        self._stopped_for_day = False
        # Option legs
        self._ce_token: int = 0
        self._pe_token: int = 0
        self._ce_symbol: str = ""
        self._pe_symbol: str = ""
        self._ce_strike: float = 0
        self._pe_strike: float = 0
        self._entry_premium: Decimal = Decimal("0")
        # Futures hedge
        self._fut_token: int = 0
        self._fut_symbol: str = ""
        self._hedge_qty: int = 0  # Net futures quantity (positive=long, negative=short)
        self._pending_hedge_qty: int = 0  # Pending hedge awaiting fill confirmation
        # Tracking
        self._expiry: date | None = None
        self._lot_size: int = LOT_SIZES.get(params.underlying, 75)
        self._quantity: int = params.quantity_lots * self._lot_size
        self._last_rebalance: datetime | None = None

    def get_subscriptions(self) -> Subscription:
        return Subscription(instrument_tokens=[], timeframes=[])

    async def on_start(self) -> None:
        self._expiry = self.ctx.next_expiry(self.params.underlying)
        logger.info(
            f"[{self.strategy_id}] Started: {self.params.underlying} "
            f"expiry={self._expiry} base={self.params.initial_strategy} "
            f"delta_threshold={self.params.delta_threshold} "
            f"rebalance_interval={self.params.rebalance_interval_minutes}m"
        )

    async def on_tick(self, tick: Tick) -> Signal | None:
        now = self.ctx.clock.now()

        # Expiry rollover
        new_expiry = self._check_expiry_rollover(self._expiry, self.params.underlying)
        if new_expiry:
            self._expiry = new_expiry

        if now.time() >= self.params.exit_time and self._entered:
            self._stopped_for_day = True
            return self._create_exit_signal("Exit time reached")

        if not self._entered and not self._stopped_for_day and now.time() >= self.params.entry_time:
            return await self._try_entry()

        if self._entered:
            # Stop loss check on option premium
            if self.params.stop_loss_pct > 0 and self._entry_premium > 0:
                ce_ltp = self.ctx.get_ltp(self._ce_token)
                pe_ltp = self.ctx.get_ltp(self._pe_token)
                current_premium = ce_ltp + pe_ltp
                change_pct = float((current_premium - self._entry_premium) / self._entry_premium * 100)
                if change_pct > self.params.stop_loss_pct:
                    logger.info(
                        f"[{self.strategy_id}] STOP LOSS: premium up {change_pct:.1f}% "
                        f"(threshold: {self.params.stop_loss_pct}%)"
                    )
                    self._stopped_for_day = True
                    return self._create_exit_signal(f"Stop loss: premium +{change_pct:.1f}%")

            return self._check_delta_hedge(now)

        return None

    async def _try_entry(self) -> Signal | None:
        """Enter the base option position (straddle or strangle)."""
        # VIX filter
        vix_block = self._check_vix_filter()
        if vix_block:
            self._log_skip_throttled(
                "ENTRY_SKIP_VIX",
                f"[{self.strategy_id}] Entry skipped: {vix_block}",
            )
            return None

        # VIX-adjusted position sizing
        adjusted_lots = self._get_vix_adjusted_lots()
        self._quantity = adjusted_lots * self._lot_size

        chain = self.ctx.get_option_chain(self.params.underlying, self._expiry)
        if not chain or not chain.strikes:
            return None

        if self.params.initial_strategy == "strangle":
            return self._enter_strangle(chain)
        else:
            return self._enter_straddle(chain)

    def _enter_straddle(self, chain) -> Signal | None:
        """Sell ATM CE + ATM PE."""
        spot = self.ctx.get_spot_price(self.params.underlying)
        if spot <= 0:
            return None

        strike_interval = 50 if self.params.underlying in ("NIFTY", "FINNIFTY") else 100
        atm_strike = round(float(spot) / strike_interval) * strike_interval

        for entry in chain.strikes:
            if float(entry.strike) == atm_strike:
                if entry.ce:
                    self._ce_token = entry.ce.instrument_token
                    self._ce_symbol = entry.ce.tradingsymbol
                    self._ce_strike = atm_strike
                if entry.pe:
                    self._pe_token = entry.pe.instrument_token
                    self._pe_symbol = entry.pe.tradingsymbol
                    self._pe_strike = atm_strike
                break

        if not self._ce_token or not self._pe_token:
            logger.warning(f"[{self.strategy_id}] Could not find ATM options at strike {atm_strike}")
            return None

        return self._finalize_entry(f"Straddle @ {atm_strike}")

    def _enter_strangle(self, chain) -> Signal | None:
        """Sell OTM CE + OTM PE selected by delta."""
        target_ce_delta = self.params.strangle_delta
        target_pe_delta = -self.params.strangle_delta

        best_ce = None
        best_ce_diff = float("inf")
        best_pe = None
        best_pe_diff = float("inf")

        for entry in chain.strikes:
            if entry.ce and entry.ce.greeks.delta > 0:
                diff = abs(entry.ce.greeks.delta - target_ce_delta)
                if diff < best_ce_diff:
                    best_ce_diff = diff
                    best_ce = entry

            if entry.pe and entry.pe.greeks.delta < 0:
                diff = abs(entry.pe.greeks.delta - target_pe_delta)
                if diff < best_pe_diff:
                    best_pe_diff = diff
                    best_pe = entry

        if not best_ce or not best_ce.ce or not best_pe or not best_pe.pe:
            logger.warning(f"[{self.strategy_id}] Could not find suitable strangle strikes")
            return None

        self._ce_token = best_ce.ce.instrument_token
        self._ce_symbol = best_ce.ce.tradingsymbol
        self._ce_strike = float(best_ce.strike)
        self._pe_token = best_pe.pe.instrument_token
        self._pe_symbol = best_pe.pe.tradingsymbol
        self._pe_strike = float(best_pe.strike)

        return self._finalize_entry(f"Strangle CE@{self._ce_strike} PE@{self._pe_strike}")

    def _finalize_entry(self, description: str) -> Signal | None:
        """Build the entry signal and resolve futures instrument."""
        ce_ltp = self.ctx.get_ltp(self._ce_token)
        pe_ltp = self.ctx.get_ltp(self._pe_token)
        self._entry_premium = ce_ltp + pe_ltp

        # Resolve futures instrument for hedging
        self._resolve_futures_instrument()

        # F1: LIMIT-at-mid for the two short option legs. Futures hedge is
        # handled separately below and remains MARKET (index futures are liquid
        # enough that spread crossing is negligible, and hedge execution is
        # time-critical).
        chain = self.ctx.get_option_chain(self.params.underlying, self._expiry)
        ce_opt = pe_opt = None
        if chain and chain.strikes:
            for entry in chain.strikes:
                if entry.ce and entry.ce.instrument_token == self._ce_token:
                    ce_opt = entry.ce
                if entry.pe and entry.pe.instrument_token == self._pe_token:
                    pe_opt = entry.pe
        ce_leg = self._build_option_leg(
            self._ce_symbol, self._ce_token, OrderSide.SELL, self._quantity, opt=ce_opt,
        )
        pe_leg = self._build_option_leg(
            self._pe_symbol, self._pe_token, OrderSide.SELL, self._quantity, opt=pe_opt,
        )
        if ce_leg is None or pe_leg is None:
            logger.warning(
                f"[{self.strategy_id}] DELTA_NEUTRAL BLOCKED: could not price legs"
            )
            return None
        legs = [ce_leg, pe_leg]

        self._entered = True
        self._last_rebalance = self.ctx.clock.now()

        logger.info(
            f"[ENTRY] strategy={self.strategy_id} type=delta_neutral "
            f"base={description} "
            f"ce_strike={self._ce_strike} ce_premium={ce_ltp} "
            f"pe_strike={self._pe_strike} pe_premium={pe_ltp} "
            f"total_premium={self._entry_premium} qty={self._quantity} "
            f"futures={self._fut_symbol or 'none'}"
        )

        return entry_signal(self.strategy_id, legs, description)

    def _resolve_futures_instrument(self) -> None:
        """Find the current-month futures contract for hedging.

        Looks up the futures instrument from the option chain builder's
        registered instruments or from the feed's instrument cache.
        If not found, hedging is disabled (no synthetic/fake tokens).
        """
        underlying = self.params.underlying
        if not self._expiry:
            logger.warning(f"[{self.strategy_id}] No expiry set, cannot resolve futures")
            return

        # Use the monthly expiry for futures (last Thursday of month)
        clock = self.ctx.clock
        monthly_expiry = clock.next_monthly_expiry()

        # Construct symbol: NIFTY26MARFUT format
        month_map = {
            1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
            7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
        }
        yy = monthly_expiry.year % 100
        mmm = month_map[monthly_expiry.month]
        fut_symbol = f"{underlying}{yy}{mmm}FUT"

        # Try to find the token from the chain builder's instrument registry
        chain_builder = self.ctx._chain_builder
        if hasattr(chain_builder, '_instruments'):
            for token, info in chain_builder._instruments.items():
                symbol = info.get('tradingsymbol', '') if isinstance(info, dict) else getattr(info, 'tradingsymbol', '')
                if symbol == fut_symbol:
                    self._fut_token = token
                    self._fut_symbol = fut_symbol
                    logger.info(
                        f"[{self.strategy_id}] Resolved futures: {fut_symbol} "
                        f"token={token} expiry={monthly_expiry}"
                    )
                    return

        # Try to find from feed's latest ticks
        feed = self.ctx._feed
        if hasattr(feed, '_latest_ticks'):
            for token, cached_tick in feed._latest_ticks.items():
                symbol = getattr(cached_tick, 'tradingsymbol', '')
                if symbol == fut_symbol:
                    self._fut_token = token
                    self._fut_symbol = fut_symbol
                    logger.info(
                        f"[{self.strategy_id}] Resolved futures from feed: {fut_symbol} "
                        f"token={token} expiry={monthly_expiry}"
                    )
                    return

        # NOT found — hedging will be disabled until futures instrument is available
        logger.warning(
            f"[{self.strategy_id}] Futures instrument {fut_symbol} not found — "
            f"delta hedging will be unavailable until instrument is registered"
        )

    def _calculate_portfolio_delta(self) -> float:
        """Calculate the net portfolio delta from option positions and futures.

        Returns total delta exposure in units of the underlying.
        """
        chain = self.ctx.get_option_chain(self.params.underlying, self._expiry)
        if not chain:
            return 0.0

        ce_delta = 0.0
        pe_delta = 0.0

        for entry in chain.strikes:
            if entry.ce and entry.ce.instrument_token == self._ce_token:
                # Short CE: negative delta (we sold, so flip sign)
                ce_delta = -entry.ce.greeks.delta * self._quantity
            if entry.pe and entry.pe.instrument_token == self._pe_token:
                # Short PE: negative delta (we sold, so flip sign)
                pe_delta = -entry.pe.greeks.delta * self._quantity

        # Futures hedge delta (1 delta per unit)
        futures_delta = float(self._hedge_qty)

        net_delta = ce_delta + pe_delta + futures_delta

        logger.debug(
            f"[{self.strategy_id}] Delta: CE={ce_delta:.1f} PE={pe_delta:.1f} "
            f"FUT={futures_delta:.1f} NET={net_delta:.1f}"
        )

        return net_delta

    def _check_delta_hedge(self, now: datetime) -> Signal | None:
        """Check if portfolio delta exceeds threshold and hedge if needed."""
        # Respect rebalance interval to avoid over-trading
        if self._last_rebalance:
            elapsed = (now - self._last_rebalance).total_seconds() / 60.0
            if elapsed < self.params.rebalance_interval_minutes:
                return None

        net_delta = self._calculate_portfolio_delta()

        if abs(net_delta) <= self.params.delta_threshold:
            return None

        # Calculate hedge quantity needed (round to lot size)
        hedge_units = -net_delta  # Opposite of current delta to neutralize
        hedge_lots = round(hedge_units / self._lot_size)

        if hedge_lots == 0:
            return None

        # Cap hedge to max_hedge_lots to prevent runaway
        max_lots = self.params.max_hedge_lots
        current_hedge_lots = abs(self._hedge_qty) // self._lot_size
        remaining_capacity = max(0, max_lots - current_hedge_lots)
        capped_lots = min(abs(hedge_lots), remaining_capacity)
        if capped_lots == 0:
            logger.debug(
                f"[{self.strategy_id}] Hedge capped: already at {current_hedge_lots}/{max_lots} lots"
            )
            return None

        hedge_qty = capped_lots * self._lot_size
        hedge_side = OrderSide.BUY if hedge_lots > 0 else OrderSide.SELL

        # We need a valid futures instrument to hedge
        if not self._fut_token or not self._fut_symbol:
            logger.warning(
                f"[{self.strategy_id}] Delta hedge needed ({net_delta:.1f}) "
                f"but no futures instrument resolved"
            )
            # Retry resolution in case instruments were loaded after startup
            self._resolve_futures_instrument()
            if not self._fut_token:
                return None

        self._last_rebalance = now

        # Track pending hedge — will be confirmed in on_order_update
        self._pending_hedge_qty = hedge_qty if hedge_side == OrderSide.BUY else -hedge_qty

        # F1: Index futures are liquid — MARKET is legitimate here (no options
        # spread risk). Pass order_type explicitly because make_leg default is
        # now LIMIT.
        legs = [
            make_leg(
                self._fut_symbol, self._fut_token, hedge_side, hedge_qty,
                order_type=OrderType.MARKET,
            ),
        ]

        logger.info(
            f"[{self.strategy_id}] DELTA HEDGE: net_delta={net_delta:.1f} "
            f"hedging {hedge_side.value} {hedge_qty} futures "
            f"(current hedge_qty={self._hedge_qty}, pending={self._pending_hedge_qty})"
        )

        return adjust_signal(
            self.strategy_id, legs,
            f"Delta hedge: {hedge_side.value} {hedge_qty} FUT (delta was {net_delta:.1f})"
        )

    def _create_exit_signal(self, reason: str) -> Signal:
        """Create signal to close all positions: options + any futures hedge."""
        ce_ltp = self.ctx.get_ltp(self._ce_token)
        pe_ltp = self.ctx.get_ltp(self._pe_token)
        exit_premium = ce_ltp + pe_ltp
        pnl_estimate = self._entry_premium - exit_premium
        logger.info(
            f"[EXIT] strategy={self.strategy_id} reason={reason} "
            f"entry_premium={self._entry_premium} exit_premium={exit_premium} "
            f"estimated_pnl={pnl_estimate} hedge_qty={self._hedge_qty}"
        )
        # F1: LIMIT-at-mid on option exit legs with per-leg MARKET fallback.
        # Futures hedge stays MARKET (index futures are liquid; execution speed
        # matters more than a 0.05-0.10 spread).
        def _opt_exit_leg(sym: str, tok: int, side: OrderSide) -> SignalLeg:
            leg = self._build_option_leg(sym, tok, side, self._quantity)
            if leg is not None:
                return leg
            logger.warning(
                f"[{self.strategy_id}] EXIT fallback to MARKET for {sym} — no bid/ask"
            )
            return SignalLeg(
                tradingsymbol=sym,
                instrument_token=tok,
                order_side=side,
                quantity=self._quantity,
                order_type=OrderType.MARKET,
            )
        legs = [
            _opt_exit_leg(self._ce_symbol, self._ce_token, OrderSide.BUY),
            _opt_exit_leg(self._pe_symbol, self._pe_token, OrderSide.BUY),
        ]

        # Close futures hedge if any — futures keep MARKET (see above).
        if self._hedge_qty != 0 and self._fut_token and self._fut_symbol:
            fut_close_side = OrderSide.SELL if self._hedge_qty > 0 else OrderSide.BUY
            legs.append(SignalLeg(
                tradingsymbol=self._fut_symbol,
                instrument_token=self._fut_token,
                order_side=fut_close_side,
                quantity=abs(self._hedge_qty),
                order_type=OrderType.MARKET,
            ))

        self._entered = False
        self._hedge_qty = 0

        return exit_signal(self.strategy_id, legs, reason)

    async def on_order_update(self, order: Order) -> None:
        """Confirm hedge quantity updates on fill, revert on rejection."""
        if order.instrument_token != self._fut_token:
            return  # Not a futures hedge order

        if order.status == OrderStatus.FILLED:
            self._hedge_qty += self._pending_hedge_qty
            logger.info(
                f"[{self.strategy_id}] Hedge fill confirmed: "
                f"hedge_qty now {self._hedge_qty}"
            )
            self._pending_hedge_qty = 0
        elif order.status == OrderStatus.REJECTED:
            logger.warning(
                f"[{self.strategy_id}] Hedge order REJECTED — "
                f"discarding pending qty {self._pending_hedge_qty}"
            )
            self._pending_hedge_qty = 0

    async def on_stop(self) -> None:
        if self._entered:
            logger.info(
                f"[{self.strategy_id}] Stopping with open position "
                f"(hedge_qty={self._hedge_qty})"
            )

    def get_state_data(self) -> dict:
        return {
            "entered": self._entered,
            "stopped_for_day": self._stopped_for_day,
            "ce_token": self._ce_token,
            "pe_token": self._pe_token,
            "ce_symbol": self._ce_symbol,
            "pe_symbol": self._pe_symbol,
            "ce_strike": self._ce_strike,
            "pe_strike": self._pe_strike,
            "entry_premium": str(self._entry_premium),
            "fut_token": self._fut_token,
            "fut_symbol": self._fut_symbol,
            "hedge_qty": self._hedge_qty,
            "last_rebalance": self._last_rebalance.isoformat() if self._last_rebalance else None,
        }

    def load_state_data(self, data: dict) -> None:
        self._entered = data.get("entered", False)
        self._stopped_for_day = data.get("stopped_for_day", False)
        self._ce_token = data.get("ce_token", 0)
        self._pe_token = data.get("pe_token", 0)
        self._ce_symbol = data.get("ce_symbol", "")
        self._pe_symbol = data.get("pe_symbol", "")
        self._ce_strike = data.get("ce_strike", 0)
        self._pe_strike = data.get("pe_strike", 0)
        self._entry_premium = Decimal(data.get("entry_premium", "0"))
        self._fut_token = data.get("fut_token", 0)
        self._fut_symbol = data.get("fut_symbol", "")
        self._hedge_qty = data.get("hedge_qty", 0)
        ts = data.get("last_rebalance")
        self._last_rebalance = datetime.fromisoformat(ts) if ts else None
