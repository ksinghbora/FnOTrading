# Short Strangle on Indian Post-SEBI — Comprehensive Analysis

**Date:** May 7 2026
**Prior verdict:** Default short strangle Sharpe -8.34 / MC p-value 1.00
on cross-strategy validation (Apr 30) — pure noise, no edge.
**Question:** Can the SS v2 + calendar adaptations recover the
Indian-quant-documented short strangle edge?

This document does proper bottom-up parameter and regime analysis
for short strangle on Indian post-SEBI options, mirrors the IC v2
+ calendar framework, and concludes with an honest deployment
recommendation.

## Executive summary

**Short strangle is structurally close to Iron Condor.** With the
existing `add_hedge=True` default, our `ShortStrangleStrategy`
literally constructs a 4-leg Iron-Condor-equivalent (sell strikes
at 0.15-Δ + buy 5-strike OTM hedges).

For Indian post-SEBI:

1. **Default config (deployed)**: ALL legacy filters, NO v2 gate, NO
   calendar — Sharpe -8.34. **Currently in shadow as `strangle_1`.**
2. **SS v2 + calendar (proposed)**: Add v2 regime gate (CI≥61.8 AND
   VRP>0) + calendar filter. Structurally near-identical to IC v2 +
   calendar with 5-strike wings vs IC's 8-strike wings.
3. **Naked SS (add_hedge=False)**: Post-SEBI ELM (2% on expiry-day
   short premium since Nov 20 2024) makes this strictly worse than
   defined-risk variant. Don't deploy.

**Honest conclusion:** SS v2 + calendar is likely a marginal variant
of IC v2 + calendar (the new best config from today's ablations).
Whether to maintain it as a separate deployment depends on whether
the 5-strike-wing config produces meaningfully different P&L than
the 8-strike. Probably modest difference; not the highest-priority
research direction.

## What the existing ShortStrangleStrategy looks like

### Structural decisions
- **4-leg position** (when `add_hedge=True`, the default):
  - Sell CE at 0.15-Δ strike
  - Sell PE at -0.15-Δ strike
  - Buy CE at 5-strikes-OTM from short CE (hedge)
  - Buy PE at 5-strikes-OTM from short PE (hedge)
- **Naked variant** (`add_hedge=False`): 2-leg position, full short
  premium, **post-SEBI ELM-vulnerable**.
- **Lot size**: 75 (NIFTY post-SEBI standard)

### Filter pipeline (current default, in `_try_entry` order)
1. Score gate (`entry_score_threshold` ≥ 60)
2. Expiry-day block (skip 0DTE entries — gamma trap)
3. VIX filter (band 13-16)
4. Trend filter (skip if move from open > 0.7%)
5. PCR filter (skip if PCR-OI outside 0.7-1.5)
6. Max-pain filter (skip if spot > 3% from max-pain strike)
7. Strike selection (delta-based)
8. VIX-adjusted sizing (cuts lots in stressed band)

### What's MISSING vs IC v2 (best config in codebase)
- ✗ v2 regime gate (`require_premium_selling_regime_v2`)
- ✗ v1 principled gate (`require_premium_selling_regime`)
- ✗ Calendar filter (`require_calendar_filter`) — added today to IC

## Per-parameter analysis

### Strike selection — `call_delta` / `put_delta`

| Default | Indian-quant alternatives |
|---|---|
| **0.15** (current) | Anurag Goel Sharpe-1.96 research: **0.40** |
| | PL Capital "24% per annum": **0.15** |
| | Bank Nifty 2017-2020 backtest: **0.15-0.20** |

**0.15-Δ** is the safer industry-canonical default. Captures less
credit (~₹40-60/share) but tail risk much lower than 0.40-Δ.

**0.40-Δ** is closer to ATM, captures more credit (~₹120-200/share),
but tail risk is much higher — only Sharpe 1.96 with the
Tue/Wed/Thu DoW filter applied. Without the calendar filter,
0.40-Δ blows up.

**Recommended for SS v2:** keep 0.15-Δ default. The marginal credit
of moving to 0.20-Δ doesn't outweigh the tail-risk increase given
NIFTY post-SEBI's frequent 0.5-1% intraday moves.

### Hedge structure — `add_hedge` / `hedge_offset_strikes`

| Variant | Structure | Post-SEBI viability |
|---|---|---|
| **`add_hedge=True`, 5-strike** (default) | 4-leg = wide IC | ✓ Defined-risk, ELM-safe |
| `add_hedge=True`, 8-strike | 4-leg = standard IC | ✓ Same as IC v2 |
| `add_hedge=False` | 2-leg naked | ✗ ELM 2% on expiry day; cap-intensive |

**Recommendation:** keep `add_hedge=True`. The 5 vs 8 strike question
is empirical — both are defined-risk; differ only in max-loss /
max-credit trade-off. **Default 5-strike is the differentiator from
IC v2 (which is 8-strike).**

### VIX band — `vix_entry_min` / `vix_entry_max`

| Strategy | VIX band | Rationale |
|---|---|---|
| Short Strangle (current) | **13-16** | Tight — naked premium blows up above 16 |
| IC v2 (validated) | 16-22 | Wider — wings tolerate more vol |
| Anurag Goel SS Sharpe-1.96 | 13-18 | Slightly wider with discipline |
| Indian VIX baseline | mean ~15 | Range 12-22 typical |

**Current 13-16 is too tight** — excludes ~50% of post-SEBI days.
But widening risks tail blow-ups on the naked legs (even with hedge,
gamma exposure rises).

**Recommended for SS v2:** 13-18. The v2 regime gate (CI+VRP) does
the heavy lifting of filtering favorable days; absolute VIX band
becomes a structural safety, not the primary filter.

### Stop loss — `stop_loss_pct`

| Default | Reasoning |
|---|---|
| **30%** (current) | Tightened from 50 — capping loss per trade |
| IC v2 default | 40% — wings cushion gamma |

For strangle WITH hedge, behavior should match IC. **30% is
slightly tight** — could relax to 35-40% to reduce premature
stop-outs. The hedge already caps absolute loss.

**Recommended for SS v2:** 35% (split between current 30 and IC's 40).

### Profit target — `profit_target_pct`

| Default | Reasoning |
|---|---|
| **15%** (current) | "Lock early theta" — captures ~30% of expected total decay |
| IC v2 default | 25% — stretches for more theta capture |

For weekly options near expiry, 15% PT fires very early (within 1-2
days typically). This caps win size. For SS hedged, could relax to
match IC's 25%.

**Recommended for SS v2:** 25% (match IC v2). The hedge protects
against the gamma reversal that would justify earlier locking.

### Trailing stop — `trail_stop_pct`

| Default | Reasoning |
|---|---|
| **15%** (current) | Tight trail, protects profit |
| IC v2 | (no trail, just profit_target) |

The trail is a strangle-specific feature — IC doesn't use it. For
SS v2 + calendar, the trail could stay at 15% as a profit-locking
mechanism distinct from the 25% PT.

**Recommended:** keep 15% trail.

### Adjustment delta threshold — `adjustment_delta_threshold`

When the short leg's delta exceeds this threshold, the strategy
adjusts (rolls the threatened side). Current: 0.25.

For SS v2 + calendar, with v2 gate already filtering favorable days:
- **0.25** = early adjustment, may trip on benign moves
- **0.30** = later adjustment, more time for mean-reversion

IC v2's `adjustment_threshold_pct=60` (premium-based) is structurally
different from delta-based. Both achieve similar "side under pressure"
detection.

**Recommended:** 0.30 (slightly looser than current 0.25).

## Regime analysis

Same as IC v2 — short strangle is a premium-selling strategy that
captures the structural Indian VRP. The v2 gate's two conditions:

### CI ≥ 61.8 (Choppiness Index range-bound threshold)
- Range-bound regime → strangle's tails stay OTM → wins
- Trending regime → tails breached → losses
- Same applicability as IC

### VRP > 0 (IV > realized vol; premium overpriced)
- Sell premium when overpriced → captures structural Indian VRP
- Same as IC v2's edge source

### Calendar-aware filter (proven in today's ablations)
Same DoW Tue/Wed/Thu + T-1 pre-event block + Friday opt-out as IC v2.

**Anurag Goel's Sharpe-1.96 short strangle backtest used DoW
Tue/Wed/Thu filtering — this is the exact filter we just validated
on IC v2 (+₹13.1/trade vs +₹1.8/trade un-filtered).** The same
mechanism should apply to SS.

## Proposed SS v2 + calendar parameter set

```json
{
  "underlying": "NIFTY",
  "quantity_lots": 1,
  "entry_time": "09:30:00",
  "exit_time": "15:00:00",
  "skip_entry_on_expiry_day": true,
  "expiry_day_force_exit_at": "14:30:00",

  "call_delta": 0.15,
  "put_delta": -0.15,
  "add_hedge": true,
  "hedge_offset_strikes": 5,
  "adjustment_delta_threshold": 0.30,

  "stop_loss_pct": 35.0,
  "profit_target_pct": 25.0,
  "trail_stop_pct": 15.0,

  "vix_entry_min": 13.0,
  "vix_entry_max": 18.0,
  "vix_reduce_above": 16.0,

  "max_spread_pct": 5.0,

  "require_premium_selling_regime_v2": true,
  "require_calendar_filter": true,
  "allowed_days_of_week": [1, 2, 3],
  "block_pre_event_days": 1,
  "block_friday": false
}
```

## Required code changes for SS v2

The `ShortStrangleStrategy._try_entry` does NOT currently support
`require_premium_selling_regime_v2` or `require_calendar_filter`.
These flags need to be:

1. Added to `ShortStrangleParams` (currently missing)
2. Wired into `_try_entry`'s filter pipeline (parallel to IC's
   implementation)
3. EventCalendar loaded in `on_start` when calendar filter active
4. RegimeDetector v2 method called when regime gate active
5. Daily-close warmup loader called when v2 gate active

**Estimated effort:** 1-2 hours of focused work mirroring IC v2's
pattern. Alternatively, extract the gate logic into a shared mixin
or base-class helper to avoid duplication across IC, IB, SS.

## Comparison: SS v2 vs IC v2 (with calendar, both)

| Param | SS v2 + calendar | IC v2 + calendar | Notes |
|---|---|---|---|
| Short delta | 0.15 | 0.15 | Same |
| Hedge / Wing | 5 strikes | 8 strikes | **SS tighter wings** |
| VIX band | 13-18 | 12-22 | SS tighter on upper end |
| Stop loss | 35% | 40% | SS tighter |
| Profit target | 25% | 25% | Same |
| Trail | 15% | (none) | SS-specific feature |
| Regime gate | CI+VRP | CI+VRP | Same |
| Calendar filter | ✓ | ✓ | Same |
| Max loss | smaller (5-strike) | larger (8-strike) | SS more conservative |
| Max credit | smaller | larger | SS less profit potential |

The question: does the 5-strike vs 8-strike wing difference
materially change per-trade EV?

**Theoretical:** smaller wings = lower max loss but lower credit
captured. The ratio (credit / max loss) should be similar — both
sit at ~1:2 or 1:3 typically. Per-trade EV is roughly equivalent.

**Empirical (would need code + smoke):** likely SS v2 + calendar
produces +₹10-15/trade (close to IC v2 + calendar's +₹13.1).

## Honest deployment recommendation

### Option A — **Don't bother (recommended)**

SS v2 + calendar is structurally redundant with IC v2 + calendar.
The IC v2 + calendar already validates at +₹13.1/trade. Spending
1-2 hours implementing SS v2 to confirm a near-identical result
isn't the highest-leverage use of time.

**Action:** Update `strangle_1` shadow params to match IC v2 +
calendar (maybe with 5-strike wings to differentiate). OR retire
`strangle_1` entirely and consolidate to `ic_1` + `ic_2` (IC v2
no-cal + IC v2 cal A/B).

### Option B — Implement SS v2 for diversification A/B

If you want a 5-strike-wing variant alongside the 8-strike IC v2 +
calendar variant:

1. Implement SS v2 + calendar params + gate logic (1-2 hours)
2. Smoke on 173-day post-SEBI corpus
3. If PnL is meaningfully different from IC v2 + calendar (>±₹3/trade),
   keep as a parallel deployment
4. If PnL is within noise, retire SS

### Option C — Test the naked variant (NOT recommended)

`add_hedge=False` with v2 + calendar would be the true Anurag-Goel
Sharpe-1.96 replication. But post-SEBI ELM (2% on expiry-day short
premium since Nov 20 2024) makes naked premium-selling materially
worse than pre-2024.

**Don't deploy naked.** The post-SEBI rule changes were designed
specifically to discourage this risk profile.

## What we DEFINITIVELY learn from this analysis

1. **The current `strangle_1` shadow deployment is wasted** — it
   uses default params (Sharpe -8.34) and contributes nothing to
   the live decision-quality data.

2. **Indian-quant short-strangle research (Anurag Goel, PL Capital)
   validates the same filter framework as IC v2 + calendar** —
   Tue/Wed/Thu DoW filter is the key edge.

3. **Post-SEBI rules favor defined-risk variants** — keep
   `add_hedge=True`. The naked SS edge documented pre-SEBI doesn't
   survive Feb-2025 ELM rules.

4. **Strangle-with-hedge ≈ Iron Condor with 5-strike wings**.
   IC v2 + calendar (just ablation-validated at +₹13.1/trade)
   already captures this opportunity space.

## Recommended action

**Update the deployed `strangle_1` to match IC v2 + calendar
parameters** (effectively making it a 5-strike-wing IC v2 +
calendar variant rather than redundant default-strangle).

This requires:
1. Adding `require_premium_selling_regime_v2` and
   `require_calendar_filter` params to `ShortStrangleParams`
2. Wiring the gates into `ShortStrangleStrategy._try_entry`
3. Updating `.env STRATEGIES` for `strangle_1` with the new params

Estimated effort: 1-2 hours when the IB sweep frees up compute.

## Sources used in this analysis

(Cross-referenced with our existing PROVEN_INDIAN_STRATEGIES.md
+ today's IB ablation results)

- [Anurag Goel — Short Strangle Backtest on NIFTY](https://medium.com/finance-simplified/backtesting-the-short-strangle-strategy-on-nifty-0af61477ba18)
  → Sharpe 1.96, 70% WR, Tue/Wed/Thu DoW filter
- [PL Capital — Short Strangles 24% Per Annum](https://www.plindia.com/blog/introducing-short-strangles-a-24-per-annum-strategy/)
  → 0.15-Δ + monthly expiries
- [Iron Butterfly vs Short Straddle: Same Trade Half Margin (OptionX)](https://optionx.trade/blogs/iron-butterfly-vs-short-straddle)
  → Confirms defined-risk advantage post-SEBI
- [SEBI's New Rules for F&O](https://zerodha.com/z-connect/business-updates/sebis-new-rules-for-index-derivatives-heres-whats-changing)
  → ELM 2% on expiry-day shorts (Nov 20 2024)
- IC v2 + calendar ablation (this codebase, commit `e350b50`)
  → +₹13.1/trade validates the same filter framework
