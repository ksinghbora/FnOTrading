"""Tiered slippage model for the paper broker.

Closes the paper-to-live P&L gap. Real Indian option fills diverge from LTP based on:
  1. Liquidity (proxied by option premium — high premium = ATM = tight spread)
  2. Time of day (afternoon gamma + closing rush widen spreads)
  3. VIX regime (stressed vol widens market-maker quotes)
  4. Order size (large orders walk the order book)

All four are multiplicative. Direction-aware: BUY pays more, SELL receives less.
"""

from dataclasses import dataclass
from datetime import time

from src.core.types import OrderSide


# ─── Liquidity tiers (option premium in INR per share → bps) ────────────
# High premium = ATM/ITM = tight market-maker spreads.
# Low premium = far OTM = thin book, wide spreads.
PREMIUM_TIERS = (
    # (min_premium, slippage_bps, label)
    (100.0, 5, "atm_itm"),       # Very liquid (NIFTY ATM ~150-300)
    (30.0, 20, "near_otm"),      # 1-3 strikes out
    (10.0, 50, "moderate_otm"),  # 3-5 strikes out
    (3.0, 100, "far_otm"),       # 5-10 strikes out, thin
    (0.0, 200, "deep_otm"),      # >10 strikes out, very thin
)


# ─── Time-of-day multiplier (IST) ───────────────────────────────────────
# Morning calm < midday < afternoon gamma < closing rush.
def _time_multiplier(t: time) -> float:
    if t < time(11, 0):
        return 0.8        # Morning calm — MMs still warming up
    if t < time(14, 0):
        return 1.0        # Midday baseline
    if t < time(15, 0):
        return 1.3        # Afternoon — gamma pressure builds
    return 1.8            # Closing rush (15:00-15:30) — widest spreads


# ─── VIX regime multiplier (Indian-calibrated bands) ────────────────────
def _vix_multiplier(vix: float) -> float:
    if vix <= 0:
        return 1.0        # Unknown → assume normal
    if vix < 16.0:
        return 1.0        # NORMAL band
    if vix < 22.0:
        return 1.3        # HIGH band — MMs widen
    return 1.8            # EXTREME — event risk premium


# ─── Order size multiplier (NIFTY lot=75, large orders walk the book) ───
def _size_multiplier(quantity: int) -> float:
    if quantity <= 75:
        return 1.0        # 1 NIFTY lot
    if quantity <= 225:
        return 1.1        # 2-3 lots
    if quantity <= 375:
        return 1.25       # 4-5 lots
    return 1.5            # >5 lots — meaningful market impact


@dataclass
class SlippageModel:
    """Tiered slippage applied to paper-broker fills.

    Defaults are calibrated for Indian NIFTY options. Override the multipliers
    in tests by injecting a subclass.
    """

    min_option_price: float = 0.05  # NSE tick size floor — fills can't go below this

    def calculate_bps(
        self,
        fill_price: float,
        vix: float,
        quantity: int,
        now: time,
    ) -> float:
        """Total slippage in basis points (always positive)."""
        if fill_price <= 0:
            return 0.0
        base_bps = self._liquidity_bps(fill_price)
        return (
            base_bps
            * _time_multiplier(now)
            * _vix_multiplier(vix)
            * _size_multiplier(quantity)
        )

    def apply(
        self,
        fill_price: float,
        side: OrderSide,
        vix: float,
        quantity: int,
        now: time,
    ) -> tuple[float, float]:
        """Apply slippage to fill_price. Returns (slipped_price, slippage_bps).

        BUY: pays more than LTP. SELL: receives less than LTP.
        """
        bps = self.calculate_bps(fill_price, vix, quantity, now)
        slip = fill_price * bps / 10000.0
        if side == OrderSide.BUY:
            return fill_price + slip, bps
        return max(self.min_option_price, fill_price - slip), bps

    def _liquidity_bps(self, premium: float) -> int:
        for min_prem, bps, _label in PREMIUM_TIERS:
            if premium >= min_prem:
                return bps
        return PREMIUM_TIERS[-1][1]
