# Orchestrator V4 Backtest — first 173-day result (FAILED validation)

May 7 2026 · branch `FnO-v5-orchestration-impl`
Run: `scripts/smoke_orchestrator_live.py` against 173-day GDFL post-SEBI corpus
PnL fix applied: `e18451e` (orchestrator-child rollup in PositionTracker / PnLCalculator)

## Result

| Deployment | Trips | PnL | WR | Sharpe | EV/trip |
|---|---|---|---|---|---|
| **Orchestrator V4** (4 children, single-slot) | ~19 | **-₹150** | 55.3% | -0.13 | **-₹7.9** ❌ |
| IC v2 + cal (standalone shadow) | 20 | +₹263 | 47.5% | +0.26 | +₹13.1 |
| IB B2 + cal (standalone shadow) | 38 | +₹222 | 48.7% | +0.06 | +₹5.8 |
| SS Phase 1.5 V1 (standalone shadow) | 18 | +₹788 | 69.4% | +0.57 | +₹43.8 |

**Sum of standalone PnL: +₹1,273 across 76 standalone trips.**
**Orchestrator picked a subset (19 trips) that netted -₹150.**

## Diagnosis

Single-slot routing is selecting the wrong child. With the V4 legacy
0-100 score, IC, IB and SS often score similarly because they share
the v2 regime gate (CI ≥ 61.8 AND VRP > 0). The orchestrator's
tie-breaker is list position: IC > IB > SS > trend_daily. So when
multiple children are eligible, IC wins by default.

But the standalone numbers show:
  - IC's per-trip EV: +₹13.1 (modest)
  - SS V1's per-trip EV: +₹43.8 (3.3× IC)

The orchestrator routes most ticks to IC and gets IC-like or worse
performance. SS, the highest-EV strategy, gets a small share of the
slots.

The deeper issue: legacy 0-100 score correlates with "is the regime
suitable" but NOT with "expected P&L per trip." SS V1 wins because of
risk-management calibration (no trail stop, asymmetric exits) — not
because its setup scores higher than IC's. The V4 orchestrator can't
see that distinction.

## Decision: do NOT deploy V4 orchestrator LIVE

Three options for tomorrow:

  A. Revert orchestrator to shadow + deploy SS V1 solo LIVE
     (SS V1 is the validated +₹44/trade Sharpe +0.57 winner)
  B. Revert orchestrator to shadow, all-shadow paper trading
     (safest; zero capital at risk)
  C. Keep orchestrator LIVE with V5 flags ON (regime_aware_scoring=true)
     — V5 isn't validated on this corpus yet, risky

Recommended: **Option A.**

## Follow-up work

The orchestrator's selection logic needs work before another LIVE attempt:

1. **Validate V5 regime-aware scoring on this corpus** — does multiplying
   legacy × regime confidence change which child wins each tick? Run
   the same backtest with `regime_aware_scoring=true` and compare.

2. **Validate margin-aware ranking** — `margin_aware_selection=true`
   would prefer IB (₹1.5L margin) over IC (₹2.5L margin) at equal score.
   Whether this helps depends on whether IB's actual margin yield exceeds
   IC's after the orchestrator's routing pattern.

3. **Per-strategy `evaluate_score` tuning** — SS's score function should
   penalise the "no-trail vs trail" risk-mgmt distinction, but it doesn't.
   The score is regime-shape only. Either:
     a. Promote SS's score baseline (it deserves higher weight given the
        empirical PnL/trip)
     b. Replace legacy score with a forward-looking EV estimator per
        strategy, fed back from historical performance

4. **Multi-slot orchestration (V6)** — the single-slot model wastes
   trips on the orchestrator's pick when other children also have
   eligible setups. Multi-slot would let IC + SS + trend_daily run
   in parallel on the same day if they're in different regimes.

## Backtest infrastructure status

- The PnL aggregation bug (commit `e18451e`) was caught BY this backtest
  attempt. The first run reported ₹0 across 76 fills — clearly a bug,
  not break-even. The fix is now in place and tested.
- The backtest engine handles orchestrator + 4 children correctly.
  Per-child positions, charges, and PnL all roll up to the parent.
- 110 / 110 orchestrator-related tests pass.
- 957 / 960 broader unit tests pass (3 pre-existing replay failures).
