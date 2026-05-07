# Long Calendar v3 — Indian-Optimized Design

**Status:** Re-design from first principles using practitioner research
on Indian post-SEBI options markets. Replaces the failed v2/v2b
approach (which adapted the IC v2 orthogonal-traditions framework to
long-vol — wrong framework for calendars).

**Approach:** Adopt the Indian-quant-canonical playbook for calendar
spreads: term-structure-dislocation entry, event-driven timing, IV
Rank as the long-vol filter, day-of-week awareness, recalibrated
profit/stop targets for Indian cost basis.

## Why we're redesigning

LC v2 / LC v2b failed because the design framework was wrong:

| LC v2 / v2b | Industry-canonical for calendars |
|---|---|
| VRP < 0 absolute level | IV Rank < 30 (relative to own 1y range) |
| CI + VRP regime gate (theory-grounded but wrong instrument) | Term-structure / IV-differential |
| Generic intraday entry | Event-driven (RBI MPC, CPI, budget) |
| Profit target on debit basis | Profit target on max-profit basis |
| 90-min front-close buffer | 60 min (post-Feb-2025 SEBI margin rule) |
| US-baseline VIX band (12-22) | Indian-baseline VIX band (12-17) |
| No day-of-week awareness | Wed/Thu/Fri preferred (IV declining) |

## v3 full design (Indian-optimized)

### Strike + structure
- **Strike:** ATM (50-strike rounded to nearest)
- **Leg type:** CE (single calendar; can extend to "double calendar" CE+PE later)
- **Front leg:** Current weekly Tuesday expiry (NIFTY only post-SEBI; BankNifty has no weeklies)
- **Back leg:** Next monthly expiry (≥ 21 days after front)
- **Net debit:** typically ₹40-120 per share (₹3,000-9,000 per 75-lot)

### Entry filters (mandatory ALL)
- **VIX absolute level:** [12, 17] — Indian baseline ~15 (US 18); calendar wants moderate vol with room to expand. >17 = late, <12 = no expansion expected.
- **VIX percentile (1-year trailing):** ≤ 30 — IV Rank proxy. Buy vol when it's at the bottom 30% of its own range. *(REQUIRES CODE)*
- **Day of week:** Wed/Thu/Fri only. Empirical Indian VIX seasonality:
  - Monday: significant positive effect (IV rising) — avoid
  - Tuesday: post-expiry vol normalization noise — avoid
  - Wed/Thu/Fri: significant negative effect (IV declining toward week's-end) — favorable
  *(REQUIRES CODE)*
- **IV differential:** Front-week IV − back-monthly IV ≥ +1.0% (front is richer; mean-reversion favors calendar). *(REQUIRES CODE)*
- **Pre-event window** (boost): ≤ 2 trading days before scheduled RBI MPC, CPI release, or budget. Front IV elevated due to event premium → IV crush after the event. *(REQUIRES CODE)*

### Risk management
- **Profit target:** 40% of MAX PROFIT (not debit). Max profit on a calendar = front-leg full decay × position size. For NIFTY ATM weekly with ~₹100 net debit, max profit is roughly ₹150-250 per share, so target ≈ ₹60-100 per share captured. *(REQUIRES CODE — strategy currently uses debit basis)*
- **Stop loss:** 100% of net debit (max possible loss; explicit policy)
- **Underlying-move stop:** 1.5% from strike (calendar's narrow profit zone)
- **Forced exit:** 60 min before front-week Tuesday expiry close (avoids SEBI Feb-2025 margin spike on calendar expiry day — keep just enough theta to monetize)

### Position sizing
- **Default:** 1 lot (75 shares) = ~₹3,000-9,000 net debit per trade
- **Lot scaling:** linear up to 5 lots; capacity declines beyond ~10 lots due to spread crossing on monthly back-leg

### Cost model expectations (post-SEBI)
- Round-trip cost per round trip per lot:
  - STT 0.10% (rising to 0.15% Apr 2026) on options-sell (front-leg sell at entry + back-leg sell at exit)
  - Brokerage: ₹40 (₹20 cap × 2 sides × 2 legs)
  - Transaction charges: 0.05% on options
  - GST 18% on (brokerage + txn + sebi)
  - Stamp duty: 0.003% on buy side only (back-leg buy at entry, front-leg buy at exit)
- **Total estimated round-trip cost: ₹150-300 per lot** at NIFTY 24000

### Timing
- **Decision check:** Once per day at 09:30 IST (matches v2). Could move to 09:45 IST to let opening-auction noise settle, but 09:30 captures pre-event entries cleanly.
- **Entry placement:** market-on-open or LIMIT-at-mid; calendar net debit is the order's basis.
- **Exit placement:** market-on-close at 15:00 IST or earlier on profit/stop trigger.

## Comparison matrix (v2 / v2b / v3a / v3 full)

| Parameter | v2 | v2b | **v3a (config-only)** | **v3 full** |
|---|---|---|---|---|
| VIX band | 14-25 | 14-25 | **12-17** | 12-17 + percentile<30 |
| CI gate | ≥61.8 | dropped | **dropped** | optional |
| VRP gate | <0 | <0 | **dropped** | replaced by IV Rank<30 |
| IV differential check | none | none | **none** | front IV − back IV ≥ +1% |
| Day-of-week filter | none | none | **none** | Wed/Thu/Fri only |
| Event-day awareness | none | none | **none** | RBI MPC + CPI + budget |
| Profit target | 30% debit | 30% debit | **40% debit** | 40% max profit |
| Stop loss | 50% debit | 50% debit | **100% debit** | 100% debit |
| Front-close buffer | 90 min | 90 min | **60 min** | 60 min |
| max_underlying_move_pct | 1.5% | 1.5% | **1.5%** | 1.5% |

## v3a vs v3 full

**v3a** is the configuration-only Indian-optimized variant — it changes
defaults that don't require code changes. We can run this smoke
immediately to see if just the config-level Indian-optimization
gets us positive PnL.

**v3 full** requires four code additions to the existing
`LongCalendarStrategy`:
1. India VIX percentile from a `_daily_vix` deque sized at 252 days
   (warmup loader fetches 252 daily VIX values)
2. Day-of-week filter param + entry guard
3. Per-strike IV computation (Black-Scholes inverse) for
   front-IV vs back-IV differential check
4. Event-day calendar (RBI MPC + CPI + budget dates) loaded from
   `data/event_days.csv` (file already exists for portfolio_strategy)
5. Profit-target-on-max-profit calculation in `_check_exit_conditions`

## Expected outcomes

### v3a (config-only)
- **Sample size:** likely fewer trades than v2b (~20-30 round trips on
  173 days) because the VIX 12-17 band excludes ~50% of days where
  v2b would have fired. Approximately matches v2's 17 trades.
- **PnL:** unclear — VIX band tightening might select better days
  but the absence of CI/IV-Rank/term-structure filters means we're
  still firing on days that don't meet calendar's structural needs
- **Likely verdict:** marginally better than v2b, possibly worse than
  v2 (since v2 had the CI range filter)

### v3 full (with code)
- **Sample size:** even smaller — perhaps 5-15 round trips on 173 days
  with the IV-Rank + DoW + IV-differential AND filters
- **PnL:** higher per-trade EV because each entry meets all proven
  Indian-quant filters; but sample-thinness is a real concern
- **Decision criterion:** if even 5 trades produce > +0.3 Sharpe
  pre-cost, the framework is alive and can scale via:
  - Multi-underlying (BANKNIFTY when monthly is the only option)
  - Double calendar (CE + PE)
  - Event-driven sizing (larger size on RBI/CPI days)

## Indian-quant data references used in this design

| Source | Indian-specific finding | Used for |
|---|---|---|
| Talkoptions / OptionView | NIFTY IV typical range 12-18% | VIX band recalibration |
| Sahi.com | "Before RBI policy, VIX jumps 5-8 points; after announcement, IV crushes" | Event-driven entry timing |
| ResearchGate VIX-behavior paper | Significant Mon+ / Wed-Fri- effect on India VIX | Day-of-week filter |
| ICICI Direct | SEBI Feb 2025: no margin offset on calendar expiry day | 60-min front-close buffer |
| Zerodha Z-Connect | NIFTY lot 25→75 (Nov 2024); STT 0.0625%→0.10% | Cost model |
| Quantpedia VRP | VRP is harvested by SELLING vol, not buying | Confirmed our v2/v2b were upside-down |
| Indian ATM IV averages 12-18% | Tight band vs US 15-25% | VIX 12-17 vs 15-22 |
| ApexVol / OptionStack | Profit target 25-50% of max profit; stop -100% debit | Recalibrated targets |
| OptionX Journal | "IV differential at entry is single most predictive metric" | IV-differential filter |

## Why this design might still fail

1. **Sample size:** even the full v3 design produces few entries on a
   173-day window. WF + holdout require 30+ trades for stable
   inference; we may not get that.
2. **Indian post-SEBI cost wall:** even at the right entry, the
   round-trip cost on a 75-lot is non-trivial. The edge per trade
   needs to clear ~₹200-300.
3. **Long-vol on this market is genuinely rare:** the Quantpedia
   research and our own LC v2/v2b smokes both confirm Indian
   post-SEBI doesn't have many "cheap IV" days. Even an
   IV-Rank-based filter will be sample-thin.
4. **Event-driven calendar requires precise timing:** mistime the
   exit and you eat the IV crush AS the option you bought. Manual
   discretion may beat mechanical exit on event days.

## What v3a smoke will prove or disprove

- **If v3a +PnL:** config-level Indian-optimization is sufficient;
  proceed to v3 full as a refinement.
- **If v3a 0 trades or near-0:** VIX 12-17 is too narrow on this
  corpus; relax to 12-19 or use VIX percentile instead.
- **If v3a -PnL similar to v2:** the regime filter wasn't doing
  anything material; v3 full's IV-differential + event-day
  awareness are the only path forward.

## v3a actual smoke result (May 7 2026, post-completion)

**Verdict: ❌ Worst of the three — Indian-config-only made it WORSE
than v2 / v2b.**

| Variant | Trips | Net PnL | Win Rate | Sharpe | Mean / trade |
|---|---|---|---|---|---|
| LC v2 (CI+VRP) | 17 | -₹1,707 | 47.1% | -0.95 | -₹100 |
| LC v2b (VRP only) | 31 | -₹120,734 | 19.4% | -3.02 | -₹3,895 |
| **LC v3a** | **35** | **-₹247,337** | **42.9%** | **-3.78** | **-₹7,067** |

### Three mechanisms that made v3a worst

1. **Removing the CI gate caught more trending days.** The CI≥61.8
   condition in v2 was accidentally doing essential filtering work —
   excluding exactly the days where calendar's narrow profit zone is
   violated. v3a removes it → catches trending days → repeated
   stop-outs.

2. **Stop loss raised to 100% means losing trades realize FULL debit
   loss.** v2's 50% stop cut losers earlier; 100% policy lets them
   run to max loss. With 57.1% loser rate, this is catastrophic.

3. **More trades = more cost-wall friction.** 35 vs 17 trades = 2×
   the cost basis without proportional edge improvement.

### What this empirically proves

**Even with best Indian-quant industry knowledge applied at the
configuration level, the calendar STRUCTURE is fundamentally unfit
for Indian post-SEBI options.**

The structural mismatch (calendar's narrow profit zone of ±1% from
strike for ~21 days vs Indian post-SEBI's typical 1-2% intraday
range and ~5-7% multi-day moves) is the binding constraint, not
the filter design. No configuration-level optimization saves it.

### Implications for "v3 full" with code changes

The v3 full design (IV Rank + DoW + IV-differential + event-day
filters) would produce:
- **Smaller sample** (5-10 trades instead of 35) due to AND-gates
- **Better per-trade entry quality** (each entry meets all proven
  filters)
- **Same structural exposure** to spot-leaves-strike risk on the
  21-day hold

Even with all the right filters, the calendar's structural
fragility on Indian intraday vol means the per-trade EV likely
remains thin. Building v3 full would require:
1. Black-Scholes inverse for per-strike IV (significant code)
2. Day-of-week + event calendar plumbing (medium code)
3. Profit-target-on-max-profit refactor (small code)
4. ~1 week of focused implementation

For a strategy whose structural mismatch is now empirically
confirmed across three variants, this investment is not justified.

### The honest cumulative verdict on long calendar

Across LC default (Apr 30 cross-strategy validation), LC v2, LC v2b,
and LC v3a — **four distinct configurations spanning the full range
from "no filter" to "Indian-optimized config" to "theory-grounded
orthogonal-traditions gate"** — every variant has produced negative
PnL on the 173-day post-SEBI corpus.

**Long calendar on Indian post-SEBI options is not tradeable** with
any filter framework we have access to. The structure is the
constraint. This closes the long-calendar research arc definitively.

### What this doesn't rule out

- **Long straddle** (LS v2b) showed `at cost wall` rather than
  catastrophic — different structure (gamma-positive, no
  spot-near-strike requirement). The structural advantage exists
  but cost wall still binds.
- **Trend daily** (commit b34aefe) showed positive PnL — directional
  alpha on multi-day timescale escapes both intraday cost wall AND
  the calendar's structural fragility.
- **IC v2** remains the only options-side strategy with positive
  holdout — premium-selling during range+IV-rich regime captures
  Indian VRP harvestably.

### Methodology lesson

**TastyTrade-style "industry-canonical" calendar parameters
(recalibrated for Indian VIX baseline) don't solve the Indian
post-SEBI calendar problem.** The problem is structural, not
parameter-tuning. The right next move (already done) was to pivot
to a different strategy class entirely (TrendDaily) — which we did
in commits 1636f2b and 2f45839.
