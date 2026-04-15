"""Strategy parameter schemas — Pydantic models for strategy configuration."""

from datetime import time
from decimal import Decimal

from pydantic import BaseModel, Field


class BaseStrategyParams(BaseModel):
    """Base parameters common to all strategies."""

    underlying: str = "NIFTY"
    quantity_lots: int = 1
    entry_time: time = time(9, 20)
    exit_time: time = time(15, 15)
    max_loss: Decimal = Decimal("5000")
    product: str = "NRML"
    use_weekly_expiry: bool = True
    # VIX filter — skip entry when VIX exceeds threshold
    vix_entry_max: float = 18.0       # Don't enter if VIX > this (tightened from 22 after real data)
    vix_reduce_above: float = 15.0    # Halve position size if VIX > this

    # PCR filter — skip entry when PCR_OI is outside healthy range
    pcr_filter_enabled: bool = True    # Enabled — blocks entries in dangerous OI regimes
    pcr_oi_min: float = 0.7           # Skip if PCR_OI < this (call-heavy, bearish/volatile)
    pcr_oi_max: float = 1.5           # Skip if PCR_OI > this (extreme put hedging)

    # Max pain filter — skip entry when spot is far from max pain
    max_pain_filter_enabled: bool = True   # Enabled — skip when spot drifts from max pain
    max_pain_proximity_pct: float = 3.0    # Skip if spot > X% from max pain


class ShortStraddleParams(BaseStrategyParams):
    """Parameters for Short Straddle strategy."""

    adjustment_threshold_pct: float = 40.0  # Adjust when premium moves X% against (tightened)
    stop_loss_pct: float = 30.0             # Exit at X% of total premium collected (tightened from 50)
    trail_stop_pct: float = 15.0            # Trail stop by X% of peak premium (tightened from 20)
    profit_target_pct: float = 10.0          # Exit when 10% premium decayed — captures early theta
    add_hedge: bool = True                   # Add far OTM protection
    hedge_offset_strikes: int = 6            # How far OTM for hedge legs (closer from 10 for real protection)


class ShortStrangleParams(BaseStrategyParams):
    """Parameters for Short Strangle strategy."""

    call_delta: float = 0.15                 # Sell CE at this delta (wider OTM = less gamma)
    put_delta: float = -0.15                 # Sell PE at this delta
    adjustment_delta_threshold: float = 0.25 # Adjust when delta exceeds this
    stop_loss_pct: float = 30.0              # Tightened from 50 — cap loss per trade
    trail_stop_pct: float = 15.0             # Tightened from 22 — lock profits faster
    profit_target_pct: float = 15.0          # Exit when 15% premium decayed — lock early theta
    add_hedge: bool = True
    hedge_offset_strikes: int = 5            # Closer from 8 — meaningful protection at ~250pts


class IronCondorParams(BaseStrategyParams):
    """Parameters for Iron Condor strategy."""

    # Iron condor has defined risk (wings cap max loss), so it tolerates higher VIX
    vix_entry_max: float = 25.0              # Higher than base 18 — wings protect against VIX spikes
    vix_reduce_above: float = 20.0           # Halve lots above this
    short_call_delta: float = 0.15
    short_put_delta: float = -0.15
    wing_width_strikes: int = 5              # Distance between short and long strikes
    adjustment_threshold_pct: float = 60.0   # Tightened from 70 — adjust earlier
    stop_loss_pct: float = 40.0              # Tightened from 60 — faster exit on losers
    profit_target_pct: float = 25.0          # Tightened from 30 — lock profits earlier


class DeltaNeutralParams(BaseStrategyParams):
    """Parameters for Delta Neutral strategy."""

    initial_strategy: str = "straddle"       # 'straddle' or 'strangle'
    delta_threshold: float = 200.0           # Hedge when abs(delta) exceeds this
    hedge_with: str = "futures"              # 'futures' or 'options'
    rebalance_interval_minutes: int = 30     # Check delta every N minutes
    strangle_delta: float = 0.25             # If using strangle as base
    stop_loss_pct: float = 60.0             # Exit when option premium up X% (0=disabled)
    max_hedge_lots: int = 3                 # Cap futures hedge to avoid runaway


class MomentumParams(BaseStrategyParams):
    """Parameters for Momentum strategy."""

    lookback_candles: int = 20
    entry_threshold_pct: float = 0.5         # Enter on X% move
    timeframe: str = "5m"
    use_futures: bool = True
    protective_option_delta: float = 0.30
    trailing_stop_pct: float = 1.0


class MeanReversionParams(BaseStrategyParams):
    """Parameters for Mean Reversion strategy."""

    bollinger_period: int = 20
    bollinger_std: float = 2.0
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    timeframe: str = "15m"


class CalendarSpreadParams(BaseStrategyParams):
    """Parameters for Calendar Spread strategy."""

    near_expiry_offset_weeks: int = 0        # 0 = current week
    far_expiry_offset_weeks: int = 4         # 4 weeks out
    strike_offset_from_atm: int = 0          # 0 = ATM
    option_type: str = "PE"                  # CE or PE


class ExpiryScalperParams(BaseStrategyParams):
    """Parameters for Expiry Day Scalper strategy."""

    entry_time: time = time(13, 0)           # Start late on expiry day
    scalp_type: str = "straddle"             # 'straddle', 'strangle', or 'directional'
    stop_loss_points: float = 20.0           # Tight SL in index points
    target_points: float = 30.0
    max_trades: int = 5
    min_premium: float = 5.0                 # Don't sell below this premium


class PortfolioParams(BaseStrategyParams):
    """Parameters for the unified Portfolio strategy.

    Regime-aware orchestrator: only trades on strong signals.
    Premium sellers on range-bound days, trend follower on trending days.
    """

    # Signal gating — minimum score (out of 100) to trigger a trade
    signal_threshold: int = 60           # Phase 2 minimum (trend or premium fallback)
    phase1_threshold: int = 75           # Phase 1 (9:30-10:00): only high-conviction premium
    entry_time: time = time(9, 30)       # Wait for morning range to form
    exit_time: time = time(15, 15)

    # Mode selection — IC is default (defined risk), strangle only when very calm
    strangle_vix_max: float = 14.0       # Use strangle below this VIX; above = IC (better in high-vol)
    trend_switch_threshold: int = 70     # Close IC and switch to trend if trend score >= this mid-day
    trend_switch_enabled: bool = True    # Allow closing IC to enter trend on strong breakout

    # Premium mode (strangle) — used when VIX < 18
    premium_call_delta: float = 0.15
    premium_put_delta: float = -0.15
    premium_stop_loss_pct: float = 25.0     # Tighter SL — cut losses fast (was 30, sweep-validated)
    premium_trail_stop_pct: float = 10.0     # Reduced trail — still protects reversals (was 15)
    premium_profit_target_pct: float = 12.0   # Earlier theta capture (was 15, OOS-validated)
    premium_hedge: bool = True
    premium_hedge_offset: int = 5

    # Iron condor mode — used when VIX 18-25
    ic_short_call_delta: float = 0.15
    ic_short_put_delta: float = -0.15
    ic_wing_width_strikes: int = 5
    ic_stop_loss_pct: float = 40.0
    ic_profit_target_pct: float = 60.0     # Let IC decay more — defined risk (was 50, OOS-validated)

    # Gamma-aware exit — tighten stop when gamma exposure is high
    gamma_exit_threshold: float = 60.0     # Gamma exposure (gamma × lots × spot × 1%) — tighten stop above this
    gamma_expiry_multiplier: float = 2.0   # Multiply gamma sensitivity on expiry day

    # Theta efficiency — exit when theta/gamma ratio drops (diminishing returns, rising risk)
    theta_gamma_min_ratio: float = 0.0     # Exit when |theta/gamma| < this (0 = disabled)

    # Trend mode (debit spread) — used when trending
    trend_spread_width_strikes: int = 3      # 150pts wide (was 2/100pts) — more room for move to reach max value
    trend_stop_loss_pct: float = 20.0       # Tighter — cut trend losers fast (was 25, OOS-validated)
    trend_profit_target_pct: float = 55.0    # Let winners run slightly bigger (was 50)
    trend_trailing_stop_pct: float = 15.0    # Reduced trail — still protects gains (was 25)
    breakout_confirmation_pct: float = 0.5


class TrendDebitSpreadParams(BaseStrategyParams):
    """Parameters for Trend Debit Spread strategy.

    Buys debit spreads (bull call or bear put) on morning range breakouts.
    Profits from trending markets that hurt premium sellers.
    """

    entry_time: time = time(10, 30)           # 10:30 avoids peak morning trap window (10:00-10:30)
    exit_time: time = time(13, 0)            # 13:00 time stop — afternoon theta too punishing at 5 DTE
    breakout_confirmation_pct: float = 0.7   # Fallback % — overridden by ATR-normalized threshold in code
    spread_width_strikes: int = 3            # 150pts on NIFTY (3 x 50pt) — more room to reach max value
    stop_loss_pct: float = 35.0              # Cut losers faster (was 50 — too wide)
    profit_target_pct: float = 50.0          # Unchanged — 50% of max spread value
    trailing_stop_pct: float = 15.0          # Tighter trail (was 25), activation threshold also fixed
    max_trades_per_day: int = 1              # Reduced from 2 — avoid whipsaw re-entries
    oi_confirm: bool = True                  # Require OI level breach to confirm breakout
    log_only: bool = False                   # Enabled for trading (validated on real data)
