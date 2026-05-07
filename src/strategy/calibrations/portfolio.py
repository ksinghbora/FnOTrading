"""Portfolio (legacy unified) calibration — params only.

Edit ONLY this file to recalibrate the legacy portfolio strategy.
PortfolioStrategy is a self-contained switching strategy (premium-or-
trend mode); it has no compute_regime_confidence hook because it is
not orchestrated under the V5 OrchestratorStrategy.
"""

from __future__ import annotations

from datetime import time

from src.strategy.params import BaseStrategyParams


REGIME_FAMILY = "unknown"


class PortfolioParams(BaseStrategyParams):
    """Parameters for the unified Portfolio strategy.

    Self-contained regime-switching strategy: trades premium-sellers
    on range-bound days, trend follower on trending days. Predates
    the V5 orchestrator and remains as a standalone deployment option.
    """

    # Signal gating — minimum score (0-100) to trigger a trade
    signal_threshold: int = 60           # Premium leg
    trend_signal_threshold: int = 50     # Trend leg (rebalanced max=90)
    phase1_threshold: int = 75           # 9:30-10:00: high-conviction premium only
    entry_time: time = time(9, 30)       # Wait for morning range to form
    exit_time: time = time(15, 15)

    # Mode selection — Indian VIX bands
    strangle_vix_min: float = 13.0       # Below: complacency
    strangle_vix_max: float = 16.0       # Above: switch to IC
    ic_vix_max: float = 22.0             # Above: no premium leg
    trend_switch_threshold: int = 70     # Close IC if trend score >= this
    trend_switch_enabled: bool = True

    # Premium mode (strangle, used when VIX < 18)
    premium_call_delta: float = 0.15
    premium_put_delta: float = -0.15
    premium_stop_loss_pct: float = 25.0
    premium_trail_stop_pct: float = 10.0
    premium_profit_target_pct: float = 12.0
    premium_hedge: bool = True
    premium_hedge_offset: int = 5

    # Entry guards (calibrated from chain-replay decision logs Apr 18 2026)
    premium_min_entry_credit: float = 5.0
    premium_max_trades_per_day: int = 1
    premium_blocked_hours: tuple[int, ...] = (11, 12)

    # Hard filters master switch (default OFF)
    portfolio_filters_enabled: bool = False

    # Iron condor mode (used when VIX 18-25)
    ic_short_call_delta: float = 0.15
    ic_short_put_delta: float = -0.15
    ic_wing_width_strikes: int = 8
    ic_stop_loss_pct: float = 40.0
    ic_profit_target_pct: float = 60.0
    ic_min_entry_credit: float = 10.0

    # Gamma-aware exit
    gamma_exit_threshold: float = 60.0
    gamma_expiry_multiplier: float = 2.0
    theta_gamma_min_ratio: float = 0.0     # 0 = disabled

    # Trend mode (debit spread, used when trending)
    trend_spread_width_strikes: int = 2
    trend_stop_loss_pct: float = 25.0
    trend_profit_target_pct: float = 50.0
    trend_trailing_stop_pct: float = 15.0
    breakout_confirmation_pct: float = 0.5
    trend_vix_min: float = 0.0
    breakout_atr_multiplier: float = 1.25

    # Event-day hard block + Friday square-off (P1 #11, #12)
    event_day_hard_block_enabled: bool = True
    event_day_soft_penalty_only: bool = False
    event_calendar_path: str = "data/event_days.csv"

    friday_premium_squareoff_enabled: bool = True
    friday_squareoff_time: time = time(14, 55)

    # P1.5 regime gate — DISABLED by default after Apr 25 2026 validation
    # showed the gate is net-negative on the 82-day window. Infrastructure
    # retained: flip to ["high_vix","trending"] to re-enable.
    premium_blocked_regimes: list[str] = []
    trend_blocked_regimes: list[str] = []
