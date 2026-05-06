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

    # Phase 3b Gate B — intraday VIX-spike filter (PRE-REGISTERED, default off).
    # Blocks new entries on days where VIX has risen >= threshold% from morning
    # open after a configurable activation time. Designed for iron_condor based
    # on the May 8 2025 spike (VIX 15.6 → 22.8 in last 90 min) which produced
    # the wf_coverage failure. See reports/phase3b_research/regime_gate_proposal.md.
    # Default disabled — operator must opt-in to test on holdout. Per discipline
    # §VII.7, this gate has not been calibrated on validation data.
    intraday_vix_spike_enabled: bool = False
    intraday_vix_spike_threshold_pct: float = 15.0    # +15% from morning open
    intraday_vix_spike_activate_after: time = time(11, 30)  # IST, gate active after this

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

    # ─── Vol-scaled exits (opt-in, OFF by default) ─────────────────────
    # Problem: hardcoded SL/PT/trail percentages are tuned to one VIX
    # regime. At VIX=11 a 25% SL = ~2 ticks of normal noise; at VIX=22 it
    # = ~25 ticks of normal noise. Same parameter, contradictory behavior
    # across regimes — a known curve-fit seed.
    #
    # Fix: scale the effective % by expected one-sigma premium move over
    # DTE, using `sl_vol_k × (vix/100) × sqrt(dte/365)` where `k` is the
    # multiplier calibrated to reproduce current behavior at VIX=15 weekly.
    #
    # Calibration (VIX=15, weekly expiry mid T=7/365):
    #   sigma * sqrt(T) = 0.15 * sqrt(7/365)
    #                   = 0.15 * 0.1384
    #                   = 0.02077
    # Current 25% SL => k = 0.25 / 0.02077 ~= 12.0
    # Current 12% PT => k_pt ~= 5.8
    # Current 10% trail => k_trail ~= 4.8
    # These defaults reproduce the existing behavior at VIX=15 weekly.
    #
    # Effective % is clamped to [0.10, 0.60] to prevent absurd values on
    # expiry-day VIX spikes or near-zero DTE denominators.
    #
    # Opt-in via `vol_scaled_exits=True` on the concrete params instance.
    # A/B in shadow mode BEFORE flipping the default — this is a behavior
    # change on every existing backtest result in memory files.
    vol_scaled_exits: bool = False
    sl_vol_k: float = 12.0
    pt_vol_k: float = 5.8
    trail_vol_k: float = 4.8

    # ─── Apr 29 2026 Phase 2 — uniform cross-strategy gates ──────────
    # Promoted from IronCondorParams so strangle/straddle/calendar share
    # the same liquidity filter and score threshold. Subclasses can
    # override via their own field definitions if a strategy needs a
    # tighter or looser default (none currently do — calibrated values
    # were the same constant repeated in IC's _try_entry).

    # Reject any candidate strike whose bid-ask spread exceeds this
    # fraction of mid. The chain-gap diagnostic showed spreads on
    # deep-OTM wings can easily eat the IC's edge; the same risk
    # applies to far-OTM strangle / straddle / calendar legs. 0
    # disables the filter (kept for bisection / regression-test use).
    max_spread_pct: float = 5.0
    # Minimum signal score (0-100) required to fire entry. Strategies
    # that compute their own multi-factor score gate against this.
    # Default 60 reproduces the prior hardcoded literal at three
    # different sites (iron_condor.py:244, short_strangle.py:130,
    # short_straddle.py:137) which were never sweepable until now.
    entry_score_threshold: int = 60

    # ─── P1.5 regime gate ─────────────────────────────────────────────
    # Block new entries when the current tick classifies into any of these
    # regime labels. Labels use the EXACT thresholds from the harness
    # stratifier (src/backtest/validation/regime.py `bucket_row`):
    #   high_vix  : VIX > 15
    #   mid_vix   : 13 <= VIX <= 15
    #   low_vix   : VIX < 13
    #   expiry_week : dte <= 2 or is_expiry
    #   event_day : today ∈ data/event_days.csv (HARD_BLOCK|SOFT_CAUTION)
    #   trending  : |move_from_open_pct| > 1.0
    #   range_bound : |move_from_open_pct| <= 0.5
    # Empty list (default) = no regime gating, backward-compatible.
    # Set to e.g. ["high_vix", "trending"] to have the strategy skip
    # entries when either label is active — mirrors the harness regime
    # gate 1:1 so a blocked-at-runtime bucket cannot appear in the
    # stratified report.
    blocked_regimes: list[str] = []


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


class LongStraddleParams(BaseStrategyParams):
    """Parameters for Long Straddle strategy — long-vol via long ATM CE+PE.

    May 6 2026 (post-LC v2 / LC v2b verdict): the long calendar
    structure failed because spot moved away from strike on most
    Indian post-SEBI days. A long straddle's structural advantage:
    one of the two legs ALWAYS goes ITM on a directional move, so
    "spot leaves strike" is the source of profit, not catastrophe.

    Structure:
      BUY ATM CE  +  BUY ATM PE  (same expiry, same strike)

    Net debit position. Profits when:
      1. Spot moves away from strike before expiry (gamma)
      2. Implied vol expands (positive vega on both legs)

    Loses when:
      1. Spot stays near strike (theta decay on both legs)
      2. IV collapses (negative vega on both legs)

    Long straddle is the "anti-LC" for the same gate: where LC needs
    spot to stay near strike, LS needs spot to leave it. Identical
    VRP<0 gate (IV cheap, room to expand) tests whether the gate or
    the structure was the binding constraint in LC's failure.
    """

    # VIX entry band — straddle wants moderate-to-high vol with room to expand
    # Below 13 = no expansion expected; above 25 = already expanded, late
    vix_entry_min: float = 13.0
    vix_entry_max: float = 25.0
    vix_reduce_above: float = 22.0

    # Strike selection
    strike_offset_pct: float = 0.0           # 0 = ATM, ±X = slightly OTM
    delta_target: float = 0.5                # ATM = ~0.5 delta; informational

    # Expiry — long straddle on weekly works for 1-3 day intraday holds
    use_weekly_expiry: bool = True

    # Risk management — long-debit position, max loss = net debit paid
    profit_target_pct: float = 50.0          # Exit when straddle gains X% (high — needs big move)
    stop_loss_pct: float = 50.0              # Exit when straddle drops X% (limit theta bleed)

    # Timing — must be `time` types (pydantic does NOT auto-convert
    # str→time in subclass overrides; an earlier `str = "09:30:00"`
    # default silently broke `now.time() >= self.params.entry_time`
    # comparison and produced 0 entries on the v2b smoke).
    entry_time: time = time(9, 30)
    exit_time: time = time(15, 0)
    skip_entry_on_expiry_day: bool = True
    expiry_day_force_exit_at: time = time(14, 30)

    # Defaults — long-vol structure, default OFF for legacy filters
    pcr_filter_enabled: bool = False         # PCR less informative for long-vega
    max_pain_filter_enabled: bool = False    # Same — straddle profit zone differs from short premium

    # May 6 2026 — v2b regime gate (single-condition VRP < 0).
    # Long straddle uses ONLY the v2b form (no CI requirement), since:
    #  1. Long straddle profits from MOVEMENT, opposite of LC's "spot
    #     stays near strike" preference. CI≥61.8 (range-bound) would
    #     gate against the very regime where LS wins.
    #  2. The pure VRP<0 gate captures the "IV is cheap, room to
    #     expand" alpha — LS's primary edge source.
    # Mutually exclusive with the legacy VIX/PCR/MP filters.
    require_long_vol_regime_v2b: bool = False


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
    # max_spread_pct moved to BaseStrategyParams (Apr 29 Phase 2) so
    # strangle/straddle/calendar share the filter. Override here if IC
    # ever needs a different default.

    # May 2 2026: Indian-market range-detection gate. When True, IC
    # entries require ADX(14)<22 AND BB-squeeze active AND RV/IV<0.80
    # — proven institutional indicators calibrated for NIFTY/BANKNIFTY
    # 5-min spot. See ``RegimeDetector.is_premium_selling_favorable``.
    # Default False so existing validation reports remain reproducible.
    # Strong-signal configs opt in via params override.
    require_premium_selling_regime: bool = False

    # Apr 30 2026 v2: orthogonal Choppiness Index + VRP gate. The v1
    # (ADX + BB-squeeze + RV/IV) AND-gate fires 0/2590 valid samples
    # because its three conditions are negatively correlated on Indian
    # post-SEBI data. The v2 gate uses two orthogonal literature-
    # canonical signals: CI ≥ 61.8 (Fibonacci range threshold; Bill
    # Dreiss formula) AND VRP > 0 (Bollerslev-Tauchen-Zhou 2009 RFS
    # break-even). See ``RegimeDetector.is_premium_selling_favorable_v2``
    # for the derivation. Mutually exclusive with v1; do not enable both.
    require_premium_selling_regime_v2: bool = False


class IronButterflyParams(IronCondorParams):
    """Parameters for Iron Butterfly strategy — Iron Condor with ATM body."""

    # ATM short body (delta ~0.5) instead of OTM. Larger credit, tighter
    # break-even zone, higher gamma/vega than IC. All other defaults
    # inherited from IronCondorParams; tune via STRATEGIES env var if needed.
    short_call_delta: float = 0.5
    short_put_delta: float = -0.5


class OrchestratorParams(BaseStrategyParams):
    """Parameters for the OrchestratorStrategy meta-controller.

    Phase 3c (Apr 27 2026): a registry-discovered, fully-decoupled meta
    strategy that runs N child strategies in parallel as scorers and
    routes execution to the highest-scoring child each tick. Children
    must NOT be hardcoded — they're listed by name and looked up via
    ``src.strategy.registry``. See memory/orchestrator_decoupling.md for
    the design contract.

    Adding a strategy = put its registry name in ``children``. Removing
    = take it out. No orchestrator code changes either way.
    """

    # Names from the registry. Strategy must register itself via
    # @register_strategy(name, ParamsCls) to be discoverable here.
    # Order is informational — selection is by score, not list position.
    children: list[str] = Field(default_factory=lambda: [
        "iron_condor",
        "iron_butterfly",
        "short_strangle",
        "short_straddle",
        "long_calendar",
    ])

    # Per-child param overrides. Key = child registry name, value = dict
    # of param overrides for that child's params class. Children NEVER
    # see each other's params; this dict is split apart at instantiation.
    children_params: dict = Field(default_factory=dict)

    # Selection thresholds
    min_score_to_trade: int = 60        # Skip trading if no child scores >= this
    score_margin_to_switch: int = 10    # Don't switch from active child unless candidate beats by this much

    # Lifecycle
    propagate_exits_to_children: bool = True  # When orchestrator exits, force children to clear state too


class LongCalendarParams(BaseStrategyParams):
    """Parameters for Long Calendar strategy — long vega, theta differential.

    Phase 3b candidate (Apr 27): the only positive-vega strategy in the
    roster. Sells front-week ATM option, buys back-week (or back-month)
    same-strike option. Profits when:
      1. Underlying stays near ATM strike (theta differential)
      2. Implied vol expands (positive vega)
      3. Front-month decays faster than back-month

    This is the strategy that PROFITS on the Apr 7 / May 8 type vol
    spike events that killed iron_condor in Phase 3a-revised.
    """

    # VIX entry band — calendar wants moderate vol with room to expand.
    # Below 14 = no expansion expected. Above 25 = already expanded, late.
    vix_entry_min: float = 14.0
    vix_entry_max: float = 25.0
    vix_reduce_above: float = 22.0

    # Calendar structure
    leg_type: str = "CE"                     # "CE" or "PE" — single calendar; "BOTH" = double
    strike_offset_pct: float = 0.0           # 0.0 = ATM; ±X% = slightly OTM/ITM

    # Back-expiry selection. v2 (weekly back, ~7 day differential) failed:
    # bid-ask costs ate the small theta differential. v3 ups the gap:
    # back must be at least min_back_days after front, which on NIFTY
    # weeklies typically picks the NEXT MONTHLY expiry (~21-35 days out).
    # Longer time differential = larger theta-decay edge per round trip.
    min_back_days: int = 21                  # Minimum gap (calendar days) between front and back expiry

    # Risk management — long-debit position, max loss = net debit paid
    profit_target_pct: float = 30.0          # Exit when spread value gains X%
    stop_loss_pct: float = 50.0              # Exit when spread value drops X% (max -100% = full debit)
    max_underlying_move_pct: float = 1.5     # Hard stop if spot moves >X% from strike

    # Timing
    front_close_buffer_minutes: int = 90     # Close N min before front-week expiry (avoid 0DTE gamma trap on front leg)
    pcr_filter_enabled: bool = False         # PCR less informative for long-vega — disable by default
    max_pain_filter_enabled: bool = False    # Same — calendar profit zone differs from short-premium

    # May 6 2026 — long-vol regime gate (orthogonal to IC v2 gate).
    #
    # When True the strategy bypasses every legacy heuristic filter (VIX,
    # intraday-spike, PCR, max-pain) and gates entries SOLELY on the
    # theory-grounded long-vol detector:
    #
    #   CI ≥ 61.8  (range-bound by Choppiness Index — same as IC v2)
    #   VRP < 0    (IV is CHEAP relative to realized — opposite of IC v2)
    #
    # Why range AND vol-cheap (not trending AND vol-cheap as a naive
    # "inversion" would suggest): LongCalendar profits from spot staying
    # near the ATM strike (theta differential) AND IV expanding (positive
    # vega on the back leg). The first condition demands range; the
    # second demands cheap-current-IV-with-room-to-expand. So the
    # orthogonality with IC v2 is on the vol axis only:
    #
    #   IC v2:  range  AND  VRP > 0  (IV rich)
    #   LC v2:  range  AND  VRP < 0  (IV cheap)
    #
    # The two gates are mutually exclusive (vol condition flips) — never
    # both fire on the same day. In trending markets NEITHER fires, which
    # is correct: trending kills LC (spot leaves the strike) and IC alike.
    #
    # See ``RegimeDetector.is_long_vol_favorable_v2`` for the canonical
    # gate implementation. Default False so existing reports remain
    # reproducible. Mutually exclusive with the legacy VIX/PCR/MP filters.
    require_long_vol_regime_v2: bool = False

    # May 6 2026 — LC v2b: drop the CI condition, pure VRP < 0 gate.
    #
    # The May 6 2026 173-day smoke of LC v2 (CI≥61.8 AND VRP<0) fired
    # only 17 entries with -₹1,707 train+val PnL. The CI requirement is
    # the binding constraint — Indian post-SEBI markets rarely register
    # CI≥61.8 (it's the Fibonacci threshold for "range-bound", and
    # 5-min NIFTY 5-min spot is too microstructure-noisy to hit it
    # often). Theory says LC's PnL is dominated by the back-leg vega
    # (long calendar with ~21-day differential is mostly a "long-vega
    # trade" not a "theta-decay trade"), so dropping the CI condition
    # gives a larger sample and may surface a different edge.
    #
    # Risk: trending markets drag spot away from strike → max
    # underlying-move stop trips before vega expansion materialises.
    # The strategy's existing max_underlying_move_pct (default 1.5%)
    # remains as the structural defence.
    #
    # Single-condition gate:  VRP < 0 (IV cheap, room to expand)
    #
    # Mutually exclusive with require_long_vol_regime_v2; do not enable
    # both. See ``RegimeDetector.is_long_vol_favorable_v2b`` and
    # ``reports/standalone_post_sebi/LC_v2_FINDINGS.md`` for the
    # rationale.
    require_long_vol_regime_v2b: bool = False


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
    # IC min entry credit (₹/lot, total of CE+PE shorts minus wings).
    # Mirrors strangle's premium_min_entry_credit guard. Stale wing fills
    # produce sub-₹10 "credits" that aren't real — without rollback the leg
    # state stays half-set and pollutes the next entry. Pure defensive bug
    # fix; doesn't change the entry-decision distribution. Survived the
    # Apr 18 partial-revert because it's structural, not statistical.
    ic_min_entry_credit: float = 10.0

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
    # F5 revert (Apr 23 afternoon): the 0.5→0.7 tuning and trend_vix_min=12 were
    # curve-fit to Jul-Aug 2025 loss days. Expert review + F1/F2 fill fixes made
    # the holdout loss honest; the next legitimate lever is a regime gate (P1.5),
    # not a fixed-pct threshold that means different things at VIX=10 vs VIX=18.
    # Reverted to pre-Apr-23 0.5 and removed the trend_vix_min cutoff so the
    # regime detector (not a hardcoded param) carries the gating load.
    breakout_confirmation_pct: float = 0.5
    trend_vix_min: float = 0.0
    # F5b: self-calibrating ATR floor. momentum_breakout uses
    # max(confirmation_pct%, atr_multiplier × ATR14) so the effective
    # threshold scales with realized vol — tight on VIX=10 days, wide on
    # VIX=20 days. 1.25× matches the NIFTY opening-range convention
    # (expert review, trader view). Replaces the fixed-pct curve-fit.
    breakout_atr_multiplier: float = 1.25

    # ─── Event-day hard block + Friday square-off (P1 #11, #12) ──────
    # Apr 23 expert review: RBI/Fed/Budget/CPI days currently receive only a
    # score penalty (-5/-15/-25) in _evaluate_premium. Soft penalties have not
    # prevented the 3-5 blow-up days/year where short premium loses 5-10×
    # daily expected P&L. `event_day_hard_block_enabled` flips the behaviour
    # to a hard skip on HARD_BLOCK-severity events (see
    # `src/strategy/event_calendar.py`). Trend leg is not blocked — directional
    # debit spreads benefit from event-day moves.
    #
    # Set `event_day_soft_penalty_only = True` to keep the legacy score-
    # penalty-only path for A/B testing or emergency revert.
    event_day_hard_block_enabled: bool = True
    event_day_soft_penalty_only: bool = False
    event_calendar_path: str = "data/event_days.csv"

    # Friday premium square-off (P1 #12): weekend gap risk is unmodelled by
    # minute-cadence backtest. Force-flat all premium legs at
    # `friday_squareoff_time` on Fridays. Trend debit spreads are exempt
    # because their risk is directional, not weekend-gap-driven, and their
    # max loss is capped at debit paid.
    friday_premium_squareoff_enabled: bool = True
    friday_squareoff_time: time = time(14, 55)

    # ─── P1.5 regime gate (per-leg blocklists) ────────────────────────
    # Short-baseline validation flagged high_vix (Sharpe -0.95, n=513) and
    # trending (Sharpe -5.83, n=160) as decisively losing buckets — see
    # reports/validation/short_baseline_portfolio.md. Those losses are
    # driven by the premium leg (short theta bleeds on every tail move);
    # the trend leg's economics are the opposite — debit spreads are BUY-
    # premium, directional, and the whole point of the leg is to profit
    # when the market trends. Blocking both legs on "trending" would kill
    # the intended hedge.
    #
    # Default: block premium on high_vix + trending; leave trend open.
    # Empty list on either leg = no gating for that leg.
    #
    # Labels must match src/backtest/validation/regime.bucket_row exactly.
    # BaseStrategy._current_regime_labels replicates those thresholds; if
    # the harness shifts the VIX band or trend cutoff, update both places
    # together or the runtime gate stops corresponding to the measured
    # bucket.
    # P1.5 regime gate — DISABLED by default after Apr 25 2026 validation
    # showed gate is net-negative on 82-day window (median Sharpe +0.99 →
    # -0.14, cost@+0.00 total pnl +9,692 → +2,340). The blocked premium
    # trades in high_vix/trending were "less bad" than the trend-leg
    # trades in the same regimes, so removing premium left only the worst
    # performers. The binding constraint is the trend leg losing -7.38
    # Sharpe in "trending" (its designed regime) — NOT regime selection.
    # Infrastructure retained: flip to ["high_vix","trending"] to re-enable.
    premium_blocked_regimes: list[str] = []
    trend_blocked_regimes: list[str] = []


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


class TrendITMParams(BaseStrategyParams):
    """Parameters for Trend ITM strategy — Donchian breakout, single-leg deep-ITM CE/PE.

    May 1 2026 pivot from premium-selling. The post-SEBI cross-strategy
    validation (reports/standalone_post_sebi/SUMMARY.md) showed every
    premium-seller (IC / strangle / straddle / calendar) loses 3-8
    Sharpe with MC p-value ≥ 0.9997 (worse than random) on the
    regime-clean 173-day corpus. The pivot direction is *trend
    following* — profits on the breakouts that destroyed premium
    sellers; opposite cost structure (single leg vs 4 legs).

    GDFL corpus has no futures ticks — using deep-ITM single-leg
    options as a futures proxy. Delta ~0.95 mimics futures price
    action; theta is small relative to intrinsic value. See
    PIVOT_DESIGN_trend_futures.md "May 1 update" for the full rationale.

    Signal:
      - 20-bar Donchian channel breakout on 1-min spot bars
      - ATR(14) floor (require minimum tradeable range)
      - VIX 12-22 band (avoid extreme complacency AND extreme stress)
      - 09:30 → 14:30 IST entry window (skip auction noise + close squaring)

    Execution:
      - Long bias → BUY a CE strike `itm_offset_pts` BELOW spot
      - Short bias → BUY a PE strike `itm_offset_pts` ABOVE spot
      - Single leg, BUY at ask (debit position)

    Exit:
      - 2× ATR trailing stop on spot
      - 14:45 IST hard time stop (square off intraday — no overnight gap)
      - Reverse on opposite-side Donchian breakout (rare with time stop)
    """

    # ─── VIX band ─────────────────────────────────────────────────
    # Below 10: pathological calm (rare on NIFTY). Above 22: stressed,
    # mean-reverts the other way. May 1 2026 calibration: lowered floor
    # from 12 to 10 after smoke showed VIX 10-12 gates ~half the corpus
    # despite being normal NIFTY calm (typical post-SEBI VIX ranges
    # 12-18; floor of 12 was too tight a "complacency" definition).
    vix_entry_min: float = 10.0
    vix_entry_max: float = 22.0
    vix_reduce_above: float = 20.0

    # ─── Time gates (IST) ────────────────────────────────────────
    entry_time: time = time(9, 30)            # Skip auction-imbalance noise (9:15-9:30)
    exit_time: time = time(14, 45)            # Hard square-off; avoid 14:45-15:30 squaring vol
    # entry window CLOSE — don't enter new positions late even if signal triggers
    last_entry_time: time = time(14, 30)

    # ─── Donchian breakout ───────────────────────────────────────
    donchian_lookback: int = 20               # Use last 20 bars; today's close compared to high/low of 20 prior bars
    breakout_confirmation_pts: float = 5.0    # Spot must close MORE than this many points beyond the channel — filters tick noise
    # v2 (May 1 2026): also require breakout to exceed N × ATR — gates
    # out marginal breakouts that have low forward-edge. Set to 0.0 to
    # disable (v1 behaviour). v1 smoke showed PF 1.14 at zero cost but
    # very fragile; stricter entry should fire fewer but higher-quality
    # trades.
    breakout_atr_mult: float = 1.0            # 0.0 = disabled (v1)

    # ─── ATR(14) Wilder smoothing ────────────────────────────────
    # May 1 2026 calibration: GDFL spot ticks once per minute (375 unique
    # values per day); within-minute true range is 0; inter-minute median
    # TR is 3-7 pts on NIFTY 23-24K spot = 0.015-0.025% of spot. The
    # original 0.4% floor (≈90 pts) gated 100% of trades. New floor
    # 0.025% (≈6 pts on NIFTY 24K) keeps out only the dead-quiet days
    # while admitting median-and-above activity.
    atr_period: int = 14                      # Standard
    atr_floor_pct_of_spot: float = 0.025      # Skip entry if ATR/spot < this — market too calm to trend
    # v2 (May 1 2026): widened from 2.0 → 3.5. v1 smoke had trades exiting
    # within seconds of entry on small post-breakout giveback; 3.5×ATR
    # gives the position room to breathe through normal noise while still
    # capping disaster moves.
    atr_stop_mult: float = 3.5                # Trailing stop = peak_favorable_price ± atr_stop_mult × ATR
    # v2 (May 1 2026): minimum hold period — block trail-stop firing for
    # the first N minutes after entry. Even with a wider stop, fresh
    # positions bounce around the entry price; a hard minimum-hold
    # prevents the "exit within seconds" pattern v1 logs showed.
    min_hold_minutes: int = 5
    # v2: skip window for the choppy intra-day range (11:30-13:00 IST is
    # historically the lowest-realised-vol window on NIFTY). Set both
    # to ``time(0,0)`` to disable.
    skip_chop_window_start: time = time(11, 30)
    skip_chop_window_end: time = time(13, 0)

    # ─── ITM strike selection ────────────────────────────────────
    # 500 pts ITM at NIFTY 22500 = ~2.2% intrinsic. Delta ~0.95.
    # Spread % is ~1-3% on these strikes (tight); theta is small
    # relative to the ₹500 intrinsic.
    itm_offset_pts: int = 500                 # Strike offset from spot in points
    # Don't enter if no ITM strike is available within tolerance (e.g., very thin chain)
    itm_max_strike_search_pts: int = 100      # Allowed +/- from the ideal strike

    # ─── Risk management ─────────────────────────────────────────
    profit_target_pct: float = 100.0          # Exit when option premium gains 100% (debit doubled)
    stop_loss_pct: float = 40.0               # Exit when option premium drops 40% — disaster cap
    max_trades_per_day: int = 3               # Cap whipsaw re-entries

    # ─── Entry filters inherited from base ───────────────────────
    # PCR / max-pain filters mostly informative for premium-sellers; for
    # directional ITM longs they're less directly relevant. Disable
    # by default; opt-in if you want to A/B them.
    pcr_filter_enabled: bool = False
    max_pain_filter_enabled: bool = False
