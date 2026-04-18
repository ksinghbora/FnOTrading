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

    # Shadow-only mode — strategy generates signals (full decision pipeline
    # runs, decisions logged to CSV) but the runner short-circuits before
    # calling the OMS. Used to A/B challenger strategies against a single
    # live "champion" strategy on the same paper-trading capital pool, so
    # P&L attribution stays clean. The decision-log captures
    # entry/exit/score so the challenger's hypothetical performance can be
    # reconstructed offline. Default False — strategies trade as usual.
    shadow_only: bool = False
    # VIX filter — Indian-calibrated bands (see src/core/constants.py).
    # Strategy subclasses override these with band-appropriate values.
    vix_entry_min: float = 0.0        # Skip entry if VIX < this (e.g., 13 = no premium below complacency)
    vix_entry_max: float = 22.0       # Skip entry if VIX > this (default = IC stressed boundary)
    vix_reduce_above: float = 18.0    # Halve position size if VIX > this

    # PCR filter — skip entry when PCR_OI is outside healthy range
    pcr_filter_enabled: bool = True    # Enabled — blocks entries in dangerous OI regimes
    pcr_oi_min: float = 0.7           # Skip if PCR_OI < this (call-heavy, bearish/volatile)
    pcr_oi_max: float = 1.5           # Skip if PCR_OI > this (extreme put hedging)

    # Max pain filter — skip entry when spot is far from max pain
    max_pain_filter_enabled: bool = True   # Enabled — skip when spot drifts from max pain
    max_pain_proximity_pct: float = 3.0    # Skip if spot > X% from max pain

    # Expiry-day 0DTE safety (Tuesday on NIFTY weekly per SEBI Nov 2024).
    # Entering naked premium with same-day expiry = 0DTE gamma trap (14:30-15:15
    # gamma vertical can move ATM 100% in minutes; STT on auto-exercise eats wins).
    skip_entry_on_expiry_day: bool = True   # Block new entries when today == expiry
    expiry_day_force_exit_at: time = time(14, 30)  # Exit ALL legs before gamma vertical

    # Trail-stop activation gates (Apr 17 trader-analysis fix).
    # First 30 min of session is auction-imbalance noise — premium can swing
    # 10-20% on a directionless day. Trailing during that window locks losses
    # on whipsaws. Two gates must both pass before trail-stop can fire:
    #   1. Time gate: now >= trail_stop_activate_after_time
    #   2. Move gate: premium has decayed at least trail_stop_min_decay_pct
    #      from entry (proxy for "real move > N x ATR" until intraday ATR
    #      tracker lands).
    trail_stop_activate_after_time: time = time(10, 15)
    trail_stop_min_decay_pct: float = 5.0


class ShortStraddleParams(BaseStrategyParams):
    """Parameters for Short Straddle strategy."""

    # ATM exposure — most gamma-fragile of all premium strategies
    vix_entry_min: float = 13.0              # Skip below complacency (premium too thin)
    vix_entry_max: float = 15.0              # Tight upper bound — straddle blows up above this
    adjustment_threshold_pct: float = 40.0  # Adjust when premium moves X% against (tightened)
    stop_loss_pct: float = 30.0             # Exit at X% of total premium collected (tightened from 50)
    trail_stop_pct: float = 15.0            # Trail stop by X% of peak premium (tightened from 20)
    profit_target_pct: float = 10.0          # Exit when 10% premium decayed — captures early theta
    add_hedge: bool = True                   # Add far OTM protection
    hedge_offset_strikes: int = 6            # How far OTM for hedge legs (closer from 10 for real protection)


class ShortStrangleParams(BaseStrategyParams):
    """Parameters for Short Strangle strategy."""

    # Strangle ideal band on Indian VIX: 13-16 only
    vix_entry_min: float = 13.0              # Skip below — premium too cheap
    vix_entry_max: float = 16.0              # Skip above — naked short premium blows up
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

    # IC ideal band on Indian VIX: 16-22 (16-20 ideal, 20-22 stressed but tradable, >25 no trade).
    # Defined risk via wings tolerates more VIX than naked strangle.
    vix_entry_min: float = 16.0              # Below: premium too thin — strangle wins
    vix_entry_max: float = 22.0              # Above: stressed beyond IC discipline
    vix_reduce_above: float = 20.0           # Halve lots in stressed band (20-22)
    short_call_delta: float = 0.15
    short_put_delta: float = -0.15
    wing_width_strikes: int = 8              # Distance between short and long strikes (8 = ~400pt wing on NIFTY, ~1:2 risk/reward vs 1:4 at 5)
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

    # Signal gating — minimum score (out of 100) to trigger a trade.
    # Trend got its own threshold Apr 18 because the score_trend_following
    # rebalance reduced max base from 100 → 90 (factor 4 VIX-level halved
    # to remove double-count with factor 5 VIX-direction). A scenario-bounded
    # analysis on 4,958 historical TREND ENTERs (factor4_only scenario, the
    # honest apples-to-apples view since old rule_score predates factors 5/6)
    # showed 32.7% would be blocked at threshold=60 — too restrictive — but
    # only 8.0% at threshold=50, which lands in the "instrument and decide
    # after 30 days of shadow data" band. Blocked entries cluster on high-VIX
    # days where the factor 4 reduction is intentionally selective.
    # See scripts/analyze_score_rebalance_impact.py.
    signal_threshold: int = 60           # Premium leg (score_premium_selling unchanged → 100 max)
    trend_signal_threshold: int = 50     # Trend leg (score_trend_following rebalanced → 90 max base)
    phase1_threshold: int = 75           # Phase 1 (9:30-10:00): only high-conviction premium
    entry_time: time = time(9, 30)       # Wait for morning range to form
    exit_time: time = time(15, 15)

    # Mode selection — Indian VIX bands: strangle 13-16, IC 16-22, no trade outside.
    strangle_vix_min: float = 13.0       # Below: complacency — premium too cheap, no premium leg
    strangle_vix_max: float = 16.0       # Above: switch to IC (defined risk handles 16-22)
    ic_vix_max: float = 22.0             # Above: no premium leg at all (event risk)
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

    # Entry guards — calibrated from chain-replay decision logs (Apr 18 2026):
    #   • premium_min_entry_credit (₹/lot): A 2026-04-08 strangle exited
    #     with "Stop loss: premium up 26282.1%" (loss = ₹76,875 on a ₹10L
    #     pool). Root cause: entry-leg LTP captured as ~₹0.30 when one leg's
    #     last trade was stale, then SL math divided by ~zero. IC already has
    #     this guard at strategy line 676 (Decimal("10")) — strangle didn't.
    #   • premium_max_trades_per_day: Apr 17 took 14 strangle entries at
    #     hour=11 all hitting the same -29.9% stop. Trend leg had a per-day
    #     cap (TrendDebitSpreadParams.max_trades_per_day=1); premium leg
    #     incremented _prem_trades_today but never guarded on it. n=1 mirrors
    #     trend's logic — no whipsaw re-entry on the same day.
    #   • premium_blocked_hours: 2-week decision-log slice shows hour-11
    #     entries win 12% of the time (n=48, avg -₹415) and hour-12 win 4%
    #     (n=21, avg -₹531). Mid-day (13:00-14:00) is the golden window
    #     (100% win, n=23). Block 11+12 hard until n>40 days lets us tune.
    premium_min_entry_credit: float = 5.0    # ₹/lot floor; below = stale-leg suspicion
    premium_max_trades_per_day: int = 1
    premium_blocked_hours: tuple[int, ...] = (11, 12)

    # Hard filters — Apr 18 2026 audit found that BaseStrategyParams sets
    # pcr_filter_enabled=True and max_pain_filter_enabled=True by default,
    # but portfolio_strategy.py never CALLS _check_pcr_filter or
    # _check_max_pain_filter (only iron_condor / short_strangle /
    # short_straddle do). PCR and max-pain are only used as soft inputs to
    # score_premium_selling. This flag is the master switch that wires the
    # hard filters into the portfolio premium leg, mirroring iron_condor.py
    # lines 162-177. Default OFF until A/B replay validates the impact —
    # turning it on risks blocking legitimate entries on bullish days
    # (PCR < 0.7 was 11/32 of recorded decisions, all on Apr 8/9 mornings).
    portfolio_filters_enabled: bool = False

    # Iron condor mode — used when VIX 18-25
    ic_short_call_delta: float = 0.15
    ic_short_put_delta: float = -0.15
    ic_wing_width_strikes: int = 8           # Widened from 5: improves 1:4 → 1:2 risk/reward on weekly NIFTY (Apr 17 trader analysis)
    ic_stop_loss_pct: float = 40.0
    ic_profit_target_pct: float = 60.0     # Let IC decay more — defined risk (was 50, OOS-validated)

    # Gamma-aware exit — tighten stop when gamma exposure is high
    gamma_exit_threshold: float = 60.0     # Gamma exposure (gamma × lots × spot × 1%) — tighten stop above this
    gamma_expiry_multiplier: float = 2.0   # Multiply gamma sensitivity on expiry day

    # Theta efficiency — exit when theta/gamma ratio drops (diminishing returns, rising risk)
    theta_gamma_min_ratio: float = 0.0     # Exit when |theta/gamma| < this (0 = disabled)

    # Trend mode (debit spread) — used when trending
    trend_spread_width_strikes: int = 2
    # SL/PT blended back to 25/50 (Apr 18) after Apr 17 aggressive tightening
    # (20/55) was found to clip too many spreads during normal intraday
    # whipsaws — paper-cut death rate exceeded the marginal PT gain.
    # Trail stays at 15 — that piece passed the second look.
    trend_stop_loss_pct: float = 25.0        # was 20 (Apr 17), reverted to OOS-validated mid
    trend_profit_target_pct: float = 50.0    # was 55 (Apr 17), reverted to OOS-validated mid
    trend_trailing_stop_pct: float = 15.0    # unchanged — protects gains without choking winners
    breakout_confirmation_pct: float = 0.5


class TrendDebitSpreadParams(BaseStrategyParams):
    """Parameters for Trend Debit Spread strategy.

    Buys debit spreads (bull call or bear put) on morning range breakouts.
    Profits from trending markets that hurt premium sellers.
    """

    # Trend benefits from elevated vol (16-25) — debit spreads cheaper as IV rises.
    # Override base 22 cap because trend works through stressed regimes too.
    vix_entry_min: float = 12.0              # Below 12: too calm for breakouts
    vix_entry_max: float = 25.0              # Above 25: event risk overwhelms direction
    entry_time: time = time(10, 0)            # Reverted Apr 18 from 10:30 — BankNifty signal
                                              # lives in PortfolioStrategy, not here, so the
                                              # 10:30 delay had no real benefit and gave up the
                                              # high-energy 9:30-10:30 window (NIFTY ATR in that
                                              # window is ~1.4× the 10:30-13:00 window). If/when
                                              # BN confirmation gets wired into this strategy,
                                              # revisit the entry-time delay separately.
    exit_time: time = time(15, 0)            # Exit before close
    breakout_confirmation_pct: float = 0.7   # Stronger breakout required (was 0.5 — too many false signals)
    spread_width_strikes: int = 2            # Apr 18: 3 → 2 to align with PortfolioParams trend
                                             # block. Narrower spread = lower max profit but
                                             # also lower max loss; back-tests showed the
                                             # 3-strike width hit max value <8% of trades.
    stop_loss_pct: float = 50.0              # Apr 18: 35 → 50. Apr-17 audit overshot here —
                                             # debit spreads need slack through the 11:00-13:00
                                             # chop window or the trail-stop never gets a chance
                                             # to lock real profit. Trailing_stop is the active
                                             # exit; this SL is the disaster cap.
    profit_target_pct: float = 50.0          # Unchanged — good balance
    trailing_stop_pct: float = 15.0          # Tighter trail (was 25), activation threshold also fixed
    max_trades_per_day: int = 1              # Reduced from 2 — avoid whipsaw re-entries
    oi_confirm: bool = True                  # Require OI level breach to confirm breakout
    log_only: bool = False                   # Enabled for trading (validated on real data)
