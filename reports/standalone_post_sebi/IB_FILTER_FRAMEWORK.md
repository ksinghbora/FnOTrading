# Filters Best Suited for Iron Butterfly + Optimal PT/SL

**Date:** May 7 2026 (sweep results pending)
**Context:** IB v2 + calendar's -₹41/trade baseline; IC v2 + calendar's
+₹13.1/trade target. Goal: identify filter and exit-policy improvements
that close the ₹54/trade gap or determine IB is structurally untradeable.

## Diagnostic framework — why IB fails

IB's structural disadvantages relative to IC:

| Dimension | IC (Δ=0.15, w=8) | IB (Δ=0.50, w=2) | Implication |
|---|---|---|---|
| Gamma at ATM | Low (OTM short) | **High (ATM short)** | Quick reversals hurt |
| Profit zone width | ±400 pts (~1.7%) | **±100 pts (~0.4%)** | Easily violated by NIFTY's 0.4-0.8% intraday |
| Credit per lot | ~₹2-3K | ~₹6-9K | Bigger absolute SL hits |
| Vega per lot | Lower | **Higher** | Rising IV during position hurts more |
| Theta per day | Slow & steady | **Front-loaded near expiry** | More time-sensitive |

**Diagnosis:** IB's edge is theta+vega capture in a TIGHT profit zone.
Failure modes are:
1. Spot moves >100pts intraday → wing breach → SL fires
2. IV expands while position open → vega loss
3. Multi-day holds compound gamma exposure

## Filter categories — what helps gamma-fragile structures

### Category 1 — Volatility-state filters (highest priority for IB)

These directly address gamma fragility by restricting entries to
genuinely-quiet-vol days.

#### 1a. India VIX absolute level (tighter band)
**Logic:** IB needs LOW vol with stable expected move. Indian VIX
mean ~15; IB-favorable range is **12-16** (vs IC's broader 12-22).

#### 1b. India VIX percentile (IV Rank proxy)
**Logic:** IV Rank < 30 = IV at bottom 30% of trailing 1y range.
Confirms "low IV regime, not just low absolute VIX". Requires code
(VIX percentile from extended `_daily_vix` deque).

#### 1c. Realized intraday vol (5-min ATR)
**Logic:** Even if VIX is low, if morning ATR is high, today's chop
will violate IB's profit zone. Filter: ATR(14)/spot < 0.05% on 5-min.
**Already partially implemented** as `_check_intraday_vix_spike_filter`
in BaseStrategy — opt-in via params.

#### 1d. Morning range filter
**Logic:** First 30 min range < 0.3% predicts the rest of the day
will be quiet. Skip entries on days where 09:15-09:45 range is wide.
*Requires code — add to RegimeDetector.*

### Category 2 — Time-of-day filters

#### 2a. Skip volatile windows
**Logic:** Indian markets have peak volatility at:
- 09:15-09:30 (auction-imbalance) — already excluded by `entry_time=09:30`
- 13:00-13:15 (lunch-break liquidity gap) — minor
- 14:30-15:30 (closing positioning) — exclude via earlier exit

**Recommendation:** Move IB exit_time from 15:00 → 14:30. Trade-off:
loses ~1.5 hours of theta but avoids closing-positioning chaos.

#### 2b. Restrict entries to a narrow morning window
**Logic:** Enter only between 10:30-12:00 IST. Skip the first hour
volatility AND the afternoon positioning.
*Requires code — `entry_time` is single-point not window.*

### Category 3 — Microstructure filters

#### 3a. Bid-ask spread tightness
**Already implemented:** `max_spread_pct=5%` rejects illiquid strikes.

#### 3b. OI concentration (pinning detection)
**Logic:** When OI is heavily concentrated at ATM, market makers
"pin" spot to that strike → tight range → IB-favorable.
*Requires code — OI distribution from chain.*

#### 3c. Max-pain proximity
**Logic:** When spot is within 0.5% of max-pain strike, expiry-day
pinning is likely → IB-favorable for intraday.
**Already implemented:** `_check_max_pain_filter` — disabled in v2
mode but could re-enable as third gate.

### Category 4 — Calendar / event filters (already stacked)

These work for BOTH IC and IB, validated by ablation:
- ✓ DoW Tue/Wed/Thu only
- ✓ T-1 pre-event block (RBI/CPI/FOMC/Budget)
- ✓ Skip expiry-day entries (structural)
- (opt-in) Friday block

## Profit-target / Stop-loss optimization framework

### Why the IC v2 defaults (25/40) don't transfer to IB

IC v2's calibrated 25% profit target / 40% stop loss reflect:
- Wide profit zone → 25% of credit captures ~70% of expected profit
- Slow gamma → 40% stop fires only when underlying really moves

IB's tighter zone reverses both:
- Narrow profit zone → 25% of LARGER credit may need spot at strike all day
- Faster gamma → 40% stop fires too late after quick reversals

### Three exit-policy hypotheses

#### H1: Tighter profit, tighter stop (15/30) — "scalper IB"
**Logic:** Lock 15% (₹1-1.5K on 75-lot) profits early before gamma
reverses. 30% stop accepts faster losses.
**Trade-off:** Lower per-trade EV, higher win rate possible.

#### H2: Tasty-canonical (35/50) — "let it run"
**Logic:** 35% PT + 50% SL closer to TastyTrade's 50%-of-max-profit
guidance. Asymmetric in IB's favor.
**Trade-off:** Lower win rate, bigger absolute swings.

#### H3: Symmetric (25/25)
**Logic:** Average winner ≈ average loser. Reduces bias toward
either tail. Easy to A/B test.

The sweep (`smoke_ib_optimize.py`) tests all three plus two
strike variants to measure which combination — if any — produces
positive per-trade EV.

## Strike / wing variants tested

#### Narrow IC (Δ=0.35, wing=3)
**Logic:** Half-way between IC v2 (0.15/8) and IB v2 (0.50/2).
Sacrifices some credit (vs IB) for gamma cushion (vs IC).

#### Wide IB (Δ=0.50, wing=4)
**Logic:** Keep ATM body for max credit, but wider wings give
gamma protection. Trade-off: higher SPAN margin than narrow IB.

## Decision rule for the sweep

**Promising variant criteria:**
- Net per-trade EV > +₹15 (matches IC v2 + calendar baseline)
- AND ≥ 15 trips on the 173-day window (sample-sufficient)
- AND positive Sharpe

If no IB variant clears this bar, the verdict stands: IB is
structurally untradeable on Indian post-SEBI options regardless
of filter tuning.

If a variant DOES clear this bar, it becomes the production IB
config and would deploy alongside IC v2 + calendar (`ic_2`).

## Sweep results

(Pending — `scripts/smoke_ib_optimize.py` running PID 80899,
ETA ~75 min for 6 sequential 173-day backtests)

## What this framework rules in / rules out

### Filters we're definitively testing (config-only)
- Tighter VIX band ✗ (deferred to next sweep)
- Tighter PT/SL combinations ✓ (this sweep)
- Strike/wing variants ✓ (this sweep)
- Tighter exit_time ✗ (would need symmetric IC test)

### Filters that need code (next iteration if sweep is positive)
- VIX percentile (IV Rank)
- Morning range filter
- OI concentration / pinning
- Max-pain proximity (re-enable on v2 mode)

### Filters confirmed NOT to apply
- "More aggressive calendar" (more days blocked) — would shrink
  sample below the 15-trip threshold
- Multi-leg adjustments — IB's tight zone makes adjustments mostly
  losing propositions; structural defect not solved by adjustment

## What we'll know post-sweep

1. **Best PT/SL config for IB** (if any clears the bar)
2. **Whether a "narrow IC" middle-ground beats both IC and IB**
3. **Whether tighter PT/SL on IC + calendar improves the +₹13.1/trade
   baseline**

The sweep takes ~75 min. Findings will be appended to this doc.
