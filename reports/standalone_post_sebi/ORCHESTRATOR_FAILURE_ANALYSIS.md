# Orchestrator V4 — Why Negative Despite All-Positive Standalones

May 7-8 2026 · branch `FnO-v5-orchestration-impl`

User question: "individual strategies were positive but orchestrator
were negative. Actually it should be more positive. Also where is trend?"

This document is the data-driven analysis after running the diagnostic
backtest with `strategy_id`-tagged trades.

## Diagnostic v2 result (per-child fills)

| Child | Fills | Round trips | Net (sell-buy) | Per-trip avg |
|---|---|---|---|---|
| iron_condor | 24 | ~6 | +₹694 | +₹116 |
| iron_butterfly | 24 | ~6 | +₹716 | +₹120 |
| **short_strangle** | 28 | ~7 | **-₹1,462** | **-₹209** |
| trend_daily | 0 | 0 | 0 | n/a |

**Active days: 10 / 173** (5.8% of corpus). Standalone aggregate fires
on ~50+ days.

## Standalone reference benchmarks

| Strategy | Trips | PnL | WR | Sharpe | EV/trip |
|---|---|---|---|---|---|
| IC v2 + cal | 20 | +₹263 | 47.5% | +0.26 | +₹13.1 |
| IB B2 + cal | 38 | +₹222 | 48.7% | +0.06 | +₹5.8 |
| **SS Phase 1.5 V1** | 18 | **+₹788** | 69.4% | +0.57 | **+₹43.8** |

## What broke

### 1. trend_daily never fires (warmup gap in backtest engine)

trend_daily's `evaluate_score()` returns 0 until `len(self._daily_bars)
>= donchian_lookback + 1 = 21`. Daily bars come from
`_warmup_daily_bars()` which calls `ctx.get_historical_data()`. The
backtest engine's `StrategyContext` doesn't wire
`historical_data_callback`, so the call fails silently. TD accumulates
bars from live ticks during the run; with only one daily bar per day,
TD reaches 21 bars at day ~21 and could fire afterwards. But the
realised result is 0 fills — TD never actually entered.

This eliminates the multi-day-slot-lock hypothesis. TD didn't lock
anything because TD never even tried.

### 2. List-order tie-break does NOT starve IB

IB and IC fired equally (24 fills each). The fallback path successfully
routes to IB when IC's `_try_entry` returns None (e.g., wing strike
unavailable on IC's 8-wide configuration). Tie-break is a real
phenomenon but doesn't manifest as an IB starvation in this corpus.

### 3. IC and IB are MORE profitable per trip in orchestrator

Per-trip averages flipped:
  - IC orchestrator: +₹116/trip vs standalone +₹13/trip (8.9× better)
  - IB orchestrator: +₹120/trip vs standalone +₹6/trip (20× better)

Why? The orchestrator forces IC and IB to fire on a SUBSET of their
standalone opportunities. That subset is biased toward winning days
because the score-ranked tournament also captures regime quality. The
orchestrator essentially does opportunistic cherry-picking on
high-VIX days — and the cherry-picked subset wins more per trip.

### 4. SS bleeds in orchestrator (-₹209/trip vs +₹44/trip standalone)

This is the single biggest source of orchestrator's loss. Same params,
same engine, same calibration — but the orchestrator picks SS on a
NEGATIVELY-skewed subset of SS's standalone opportunities.

Standalone SS V1: 18 trips, mostly winners (69.4% WR), avg +₹44/trip.
Orchestrator SS: 7 trips, mostly losers, avg -₹209/trip.

Hypothesis: standalone SS captures all 18 of its eligible opportunities
including the easy wins. Orchestrator cherry-picks 7 of those 18 — but
the cherry-pick is ANTI-correlated with profitability. Days where SS
"clearly wins" are also days where some other child clears the score
threshold; orchestrator routes to that other child. Days where SS is
the ONLY eligible candidate are typically borderline conditions where
SS's WR drops.

The orchestrator's V4 score-based routing has zero forward-looking EV.
It picks "highest score" which correlates with regime FIT, not future
P&L. SS's edge comes from its V1 risk-management (no trail, asymmetric
exits) — not its setup score.

### 5. Activity reduction (10 active days vs ~50+ standalone aggregate)

The single-slot model bounds total activity. Even with multi-trip-
per-day exits and re-entries, the orchestrator only fires on 10 days.
Standalone IC alone fires on ~20 days; aggregate is 50+. The
orchestrator is leaving 80%+ of opportunities on the table.

## Net effect

  Orchestrator total: 76 fills | -₹150 PnL | 55.3% WR | Sharpe -0.13
  vs sum-of-standalones: ~76 trips | +₹1,273 PnL across 50+ days

The orchestrator's V4 single-slot routing is structurally inferior
to running the strategies as separate shadows. The "coordinator
benefit" doesn't exist in V4 because:
  - Score doesn't reflect EV
  - Single-slot is bounded by 1× best-trip-per-day, not ∑ standalone trips
  - Opportunity loss > opportunity quality gain

## Tomorrow's deployment plan

Running an overnight tournament of 7 alternative configs:
  1. ORCH_IC_SS — minimal premium-selling pair (no IB tie-break, no TD)
  2. ORCH_IC_IB_SS — drop only TD
  3. ORCH_ALL4_V5_REGIME — V5 regime-aware scoring on
  4. ORCH_ALL4_V5_CASHFLOOR — adds 0.50 cash-floor confidence
  5. ORCH_ALL4_V5_MARGIN — adds margin-aware ranking
  6. ORCH_IC_SS_V5_REGIME — minimal + V5 regime-aware
  7. STANDALONE_SS_V1_RECHECK — confirm SS V1 reference

If any config beats +₹10/trip with positive Sharpe, deploy as
orchestrator_1 LIVE. Otherwise fall back to **SS V1 solo LIVE +
orchestrator_shadow** for V5 development.

## Long-term fix recommendations

The score-based routing is the binding loss source. Real fixes:

1. **Forward-looking EV per child** — replace `evaluate_score()` with a
   rolling per-trip EV estimate (last 30 days) so SS's higher EV
   directly outranks IC.

2. **Multi-slot orchestration (V6)** — let ic + ss + trend run in
   parallel when in different regimes. Single-slot bounded loss is
   structural; V6 removes it.

3. **Drop legacy 0-100 score entirely** — V5's regime confidence is
   a better signal. With `regime_aware_scoring=true`, the effective
   score = legacy × confidence. If confidence dominates legacy noise,
   routing improves naturally.

4. **Remove trend_daily from orchestrator** — different lifecycle
   (multi-day vs intraday). Run TD as separate top-level deployment.
