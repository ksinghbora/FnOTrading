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
    vix_entry_max: float = 22.0       # Don't enter if VIX > this
    vix_reduce_above: float = 18.0    # Halve position size if VIX > this


class ShortStraddleParams(BaseStrategyParams):
    """Parameters for Short Straddle strategy."""

    adjustment_threshold_pct: float = 50.0  # Adjust when premium moves X% against
    stop_loss_pct: float = 50.0             # Exit at X% of total premium collected
    trail_stop_pct: float = 15.0            # Trail stop by X% of peak premium (0=disabled)
    profit_target_pct: float = 0.0           # Disabled — ATM premium too volatile for target
    add_hedge: bool = True                   # Add far OTM protection
    hedge_offset_strikes: int = 10           # How far OTM for hedge legs


class ShortStrangleParams(BaseStrategyParams):
    """Parameters for Short Strangle strategy."""

    call_delta: float = 0.20                 # Sell CE at this delta
    put_delta: float = -0.20                 # Sell PE at this delta
    adjustment_delta_threshold: float = 0.28 # Adjust when delta exceeds this
    stop_loss_pct: float = 50.0
    trail_stop_pct: float = 22.0            # Trail stop by X% of peak premium
    profit_target_pct: float = 50.0         # Exit when premium decays X% (0=disabled)
    add_hedge: bool = True
    hedge_offset_strikes: int = 8


class IronCondorParams(BaseStrategyParams):
    """Parameters for Iron Condor strategy."""

    short_call_delta: float = 0.15
    short_put_delta: float = -0.15
    wing_width_strikes: int = 5              # Distance between short and long strikes
    adjustment_threshold_pct: float = 70.0
    stop_loss_pct: float = 100.0             # % of max credit
    profit_target_pct: float = 50.0         # Exit when net credit decays X% (0=disabled)


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
