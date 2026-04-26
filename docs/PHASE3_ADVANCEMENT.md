# Phase 3 Advancement Plan (rewritten)

> **Author:** Apr 25 2026 (rewrite v2)
> **Status:** Draft for review — discipline gates not yet committed
> **Predecessor:** validation_audit_apr25.md (4 harness bugs found and fixed)
> **Architecture revision:** independent per-strategy optimization +
> separate regime detector + orchestrator. Replaces the earlier
> "single Portfolio strategy with regime-conditioned params" framing.

## 0. Where we are (Apr 25 2026)

After this session's audit work, the validation harness is finally
trustworthy. Four bugs were fixed and verified by an independent
auditor; one medium-severity latent bug was fixed proactively.
Parallel CPCV (`--workers N`) gives a deterministic ~3.5× speedup on
the M4. Full details in `memory/validation_audit_apr25.md`.

The first trustworthy validation (200-day train+val, Sep 2024 → Jun
2025) returned **FAIL** with a coherent failure pattern:

- WF windows 0-2 (test ends ≤ Apr 11 2025): test Sharpe +2.59, +5.57,
  +4.55 — strategy works in early/mid 2025.
- WF windows 3-5 (test starts ≥ Mar 20 2025): test Sharpe -2.82, -2.16,
  -1.23 — strategy stops working from late Q1 2025.
- Cumulative P&L peaked at +₹15,098 on **2025-04-04**, gave back
  ₹14,584 over 11 weeks, ended at +₹514.
- `mid_vix` is the binding losing regime (Sharpe -0.91, n=103).
- Cost sensitivity FAILs at +0.25 spread shift (edge < ¼ spread).
- Capacity is negative at base lot size: -₹82/lot at 75 lots.

Tail diagnosis (`scripts/diagnose_2025_regime_shift.py`) shows the
median trade is still profitable in late 2025 (median pnl/premium
+1.95) but the **losing tail is materially fatter** (mean pnl/premium
-2.93). Score features do **not** predict bad days — top losing days
had entry scores up to 95.

This means: **the strategy's typical day still works; the failure mode
is concentrated vol-expansion shock days that no entry score captures**.

## 1. Strategic framing — separation of concerns

The existing "Portfolio" strategy is an architectural anti-pattern:
it conflates "which strategy to run" with "how to run it". When the
combined strategy fails, you can't tell which leg is the problem;
when one leg works, you can't tell which regime it works in. The
audit cycle just exposed this. The architecture has to change.

**Phase 3 adopts the standard multi-strategy architecture:**

```
                    ┌─────────────────┐
                    │ Regime Detector │  (one model, owns "which regime")
                    └────────┬────────┘
                             │ regime_id, confidence
                             ▼
                    ┌─────────────────┐
                    │   Orchestrator  │  (owns "which strategy gets capital")
                    └────────┬────────┘
                             │ allocation per strategy
            ┌────────────────┼─────────────────────┬──────────────┐
            ▼                ▼                     ▼              ▼
      ┌──────────┐   ┌─────────────┐   ┌──────────────────┐   ┌──────┐
      │ Strangle │   │ Iron Condor │   │ Trend Spread     │   │ ...  │
      └──────────┘   └─────────────┘   └──────────────────┘   └──────┘
        (each strategy owns "how to trade well in its conditions")
```

This is what serious systematic shops do (multi-strategy CTAs,
vol-arb funds, mixture-of-experts in ML). Each component has one
responsibility, gets validated to its own objective, and can be
retired independently if it breaks.

## 2. Strategy roster — 5 strategies, 4 exposure types

The current portfolio is 100% short-vega: every premium-selling
strategy loses on vol-expansion days. That's a structural gap.
Phase 3 adds a long-vega strategy to plug it.

| # | Strategy | Delta | Vega | Gamma | Theta | Designed regime |
|---|---|---|---|---|---|---|
| 1 | **Short Strangle** | ~0 | negative | negative | positive | Low-mid VIX, range-bound |
| 2 | **Iron Condor** | ~0 | negative | negative | positive | Higher VIX, range-bound, capped risk |
| 3 | **Short Straddle** | ~0 | negative | negative | positive | Mid VIX, expiry-week, max theta |
| 4 | **Trend Debit Spread** | +/− | ~0 | ~0 | slight neg | Trending, confirmed breakout |
| 5 | **Long Straddle** *(NEW)* | ~0 | **positive** | **positive** | negative | Low IV percentile, pre-event, vol-expansion expected |

**Why exactly these five:**

1-3 are existing premium-sellers (registered as `short_strangle`,
`iron_condor`, `short_straddle` in `src/strategy/implementations/`);
they share the short-vega exposure but optimize for different
sub-regimes within "premium-favorable". Validating them
independently lets us see which regimes each one actually owns.

4 (`trend_debit_spread`) is the only directional strategy. Audit
showed it currently loses in its own designed regime, so it gets
serious revalidation in 3a — possibly retired.

5 is **new**. Buys ATM straddle when regime detector signals
high vol-expansion probability. Exposure profile is opposite to
1-3, so it acts as a portfolio-level hedge during exactly the days
that killed us in Q2 2025. This is the structural fix that risk-
management tightening alone won't deliver.

**Explicitly NOT included:**

- `delta_neutral.py` (short straddle + futures hedge): same vega
  sign as existing premium-sellers, just purer. Doesn't add
  diversification. Retired for Phase 3; can be resurrected later
  if a specific niche emerges.
- Calendar spreads / diagonal spreads: require multi-expiry data
  and vega-curvature analysis we don't have a clean handle on yet.
- Short single options (naked CE / naked PE): unbounded risk; out
  of scope at retail size.

## 3. Per-strategy design principles (research summary)

Each of the 5 strategies must satisfy:

1. **One clear edge.** Strangle = vol-risk-premium harvesting.
   Trend = momentum/breakout. Long-straddle = vol-expansion. If a
   strategy claims multiple edges, it's actually multiple strategies
   in disguise and should be split.
2. **Explicit failure modes codified.** "Strangle fails when realized
   > implied" — written in the strategy's docstring AND used by the
   regime gate. The strategy itself doesn't try to handle conditions
   outside its edge; the orchestrator decides when not to run it.
3. **Stop-loss calibrated by MAE.** Look at the distribution of
   maximum adverse excursion across all winning trades; set the SL
   above the 95th percentile of MAE-on-winners. This avoids cutting
   trades early that would have come back.
4. **Profit-target calibrated by MFE.** Look at the distribution of
   maximum favorable excursion across all winning trades; PT is
   typically the median or 60th percentile of MFE. Above that you're
   overstaying the average winner.
5. **Per-strategy capacity.** Each strategy specifies the lot size
   above which slippage starts eroding edge. This is a strategy
   property, baked into the strategy's params, not a separate
   validation concern.
6. **Per-trade risk capped at fractional Kelly.** Position size as
   fraction of capital is bounded by Kelly fraction × 0.5
   (half-Kelly), so a string of losses doesn't compound to ruin.

## 4. Phase 3a — Per-strategy independent optimization

**Duration:** 2-3 weeks (4 parallel tracks)
**Goal:** for each strategy, find its best params on TRAIN, validate
on VAL (post-audit harness), and characterize its favorable regime
by attribution. Each strategy is validated **standalone** — the
combined "Portfolio" strategy is not used.

### 3a.1, 3a.2, 3a.3, 3a.4, 3a.5 — five parallel tracks

Each track is identical structure, different strategy:

```
TRAIN  (Sep 2024 → Apr 2025, 150 days)
  ↓ CPCV with 50 paths
  ↓ Coarse 4-value grid on (entry_threshold, sl_pct, pt_pct, sizing)
  ↓ Pick params with median CPCV Sharpe (TRAIN sub-set)
VAL  (Apr 2025 → Jun 2025, 50 days)
  ↓ Single-shot with selected params
  ↓ Report Sharpe, regime stratification, cost sensitivity, capacity
  ↓ Compute MAE / MFE distributions → calibrate next-iteration SL/PT
HOLDOUT  (per-strategy holdout, NOT the orchestrator holdout)
  ↓ Reserved for Phase 3e final
```

Each strategy gets a **separate per-strategy holdout window** (different
days reserved for each, no cross-pollution). The orchestrator gets a
SEPARATE final holdout in Phase 3e.

### Concrete grids per strategy (locked once, no iteration)

**Short Strangle:**
- entry_score_threshold ∈ {55, 60, 65, 70}
- premium_call_delta ∈ {0.15, 0.20, 0.25, 0.30}
- premium_stop_loss_pct ∈ {20, 25, 30, 35}
- premium_profit_target_pct ∈ {40, 50, 60, 70}

**Iron Condor:** similar grid + wing_width_atm_pct ∈ {1.5, 2.0, 2.5, 3.0}

**Short Straddle:** similar to strangle but with strike_distance_pct = 0
fixed (it's ATM by definition)

**Trend Debit Spread:**
- breakout_confirmation_pct ∈ {0.3, 0.4, 0.5}
- trend_stop_loss_pct ∈ {15, 20, 25, 30}
- trend_profit_target_pct ∈ {30, 50, 70, 100}
- min_trend_duration_minutes ∈ {30, 45, 60}

**Long Straddle (new):**
- entry_iv_percentile_max ∈ {15, 20, 30, 40} (only enter when IV is in lowest N% of 252d range)
- max_theta_decay_pct ∈ {30, 40, 50} (exit if premium decays >X% with no breakout)
- breakeven_multiplier ∈ {1.0, 1.2, 1.5} (exit if spot crosses entry ± mult × straddle premium)
- holding_period_days_max ∈ {2, 3, 5}

Each grid has 4-5 values per param, 3-4 params → 64-256 combos. Run
all combos via parallel CPCV. Pick by **median CPCV Sharpe on a
chronologically held-out TRAIN sub-window**, not on VAL.

### Discipline gates for 3a

- **No re-running with different grid bounds.** Grids locked above.
- **No iteration based on validation feedback.** If picked params
  fail VAL, the strategy is retired — not re-tuned.
- **PBO computed across the param grid.** PBO > 0.4 → strategy
  retired regardless of best Sharpe (overfitting prevention).
- **Per-strategy holdouts NOT touched.** Each strategy's holdout is
  reserved for Phase 3e.
- **MAE/MFE calibration is descriptive only in 3a.** It informs
  Phase 3a.next-iteration grids if there's appetite, but doesn't
  retroactively change Phase 3a winners.

### Decision gate (each strategy: 3a → 3b inclusion or retire)

For each of the 5 strategies independently:

| Outcome | Status |
|---|---|
| VAL Sharpe ≥ 0.5 AND PBO ≤ 0.4 AND ≥ 5 of 8 gates PASS | Include in regime detector design (3b) |
| VAL Sharpe < 0.5 OR PBO > 0.4 OR ≤ 3 gates PASS | Retire this strategy from Phase 3 |
| Borderline (Sharpe 0.5-0.0, gates 3-4 PASS) | Keep but flag — orchestrator may give it small allocation only |

Strategies that pass 3a become **building blocks**. Strategies that
fail 3a are **out** — there's no point regime-conditioning a
strategy that fundamentally doesn't have an edge anywhere.

### Files touched in 3a

- `src/strategy/params.py` — review existing param classes (`StrangleParams`, `IronCondorParams`, `StraddleParams`, `TrendDebitSpreadParams`); add new `LongStraddleParams`.
- `src/strategy/implementations/short_strangle.py`, `iron_condor.py`, `short_straddle.py`, `trend_debit_spread.py` — independently revalidate; refactor to remove dependency on `PortfolioStrategy` if any.
- `src/strategy/implementations/long_straddle.py` *(new)* — implement the long-vol strategy.
- `scripts/optimize_strategy.py` *(new)* — generic per-strategy CPCV grid search.
- `tests/unit/test_long_straddle.py`, plus per-strategy tests.

## 5. Phase 3b — Regime detector design (independent research)

**Duration:** 2-3 weeks (can run in parallel with 3a)
**Goal:** unsupervised regime discovery on at-decision features.
**No strategy code changes.** Pure research / labeling.

### Why this is needed

The current rule-based regime labels (low_vix < 13, mid_vix 13-15,
high_vix > 15) were tuned pre-Nov-2024 weekly-expiry rule. There is
no evidence they're still meaningful. Per-regime allocation decisions
require labels we trust.

### Approach

Unsupervised clustering on at-decision features only. The decision
logger captures features per minute; GDFL parquet provides the
substrate.

### Pipeline

1. **Feature extraction.** Per-minute features:
   - `vix`, `vix_5m_change`, `vix_30m_change` (vol level + trend)
   - `intraday_range_pct` (realized intraday vol proxy)
   - `time_of_day_bucket` (9:15-10:30 / 10:30-13:00 / 13:00-15:30)
   - `dte`, `is_expiry_week`, `is_expiry_day`
   - `pcr_oi`, `iv_skew_ratio`
   - `move_from_open_pct`, `morning_range_pct`
   - `banknifty_correlation_30m`
   - **NO** outcome features (no pnl, no exit_reason, no held_minutes)

2. **Standardise.** z-score per feature, fit on TRAIN window only.

3. **Cluster.** Gaussian Mixture Model with K = {2, 3, 4, 5, 6}.
   - Pick K by **BIC**, not silhouette and not Sharpe spread.
   - BIC penalizes overfit; Sharpe-based K picking is data dredging.

4. **Hysteresis layer.** Raw GMM predictions can flicker. Apply:
   - Rolling 5-minute majority vote on raw predictions
   - Confidence threshold: top class probability > 0.70 → emit label;
     else emit `regime_uncertain`
   - This produces stable labels suitable for orchestrator dispatch.

5. **Cluster validity test.** Critical anti-overfit step:
   - Split TRAIN chronologically into halves (T1, T2).
   - Fit GMM on T1.
   - Use T1 centroids to label T2.
   - Compare per-cluster feature means in T1 vs T2; require within
     1σ for each feature.
   - If clusters don't generalise, **the regime detector is rejected**
     and Phase 3c-e proceed without it (or are canceled).

6. **Information content test.** For each strategy from 3a, compute
   per-cluster Sharpe on the TRAIN window (using the strategy's now-
   selected params from 3a). Compare:
   - Per-rule-based-VIX-bucket Sharpe spread (the legacy approach)
   - Per-data-driven-cluster Sharpe spread (the new approach)
   - Require: data-driven spread > rule-based spread by at least 1σ.

### Deliverables

- `src/strategy/regime_classifier.py` — GMM wrapper + hysteresis.
- `scripts/discover_regimes.py` — pipeline above.
- `data/regime_labels/clusters_v1.csv` — minute-level labels for the
  full 18 months.
- `reports/regime_discovery/v1.md` — feature characterisation per
  cluster, validity test, information content test.

### Discipline gates for 3b

- **No outcome features in clustering inputs.**
- **K chosen by BIC.** Locked before per-cluster Sharpe is measured.
- **Cluster validity test BEFORE Sharpe analysis.** If clusters
  don't generalise, the report says so and 3c is canceled.
- **No retro-labeling.** "Days where premium-decay > X = regime A"
  is forbidden.
- **No iteration on K after seeing Sharpe.** K is set; live with it.

### Decision gate (3b → 3c or 3b → orchestrator-skip)

| Outcome | Next step |
|---|---|
| Clusters stable across T1/T2 AND data-driven Sharpe spread > rule-based spread | Proceed to Phase 3c — strategy-regime mapping. |
| Clusters unstable | Reject regime detector. Run strategies independently with no regime conditioning. |
| Clusters stable but no Sharpe spread | Reject regime detector. Strategies' edges are not regime-dependent in our data. |

## 6. Phase 3c — Strategy-regime mapping

**Duration:** 1-2 weeks
**Goal:** for each regime label from 3b, identify which strategy
(or strategies) "own" that regime via per-(strategy, regime) Sharpe.
This is the lookup table the orchestrator consumes.

### Pipeline

1. For each regime label, slice the TRAIN+VAL data to bars in that regime.
2. For each strategy from 3a (that passed its decision gate), compute
   Sharpe on that regime's bars using the 3a-selected params.
3. Build a matrix `(regime_id × strategy)` of Sharpe values.
4. For each regime, identify:
   - **Primary strategy:** highest Sharpe with > 0.5 AND statistical
     significance (n ≥ 30, Sharpe > 1σ above 0).
   - **Secondary strategies:** Sharpe > 0 AND > 0.5σ above 0 (small
     allocation).
   - **No-fire regimes:** no strategy has Sharpe > 0 with significance
     → orchestrator goes to cash.
5. Build the `regime_strategy_map`:
   ```python
   regime_strategy_map = {
       "regime_0": [("short_strangle", 0.6), ("iron_condor", 0.4)],
       "regime_1": [("trend_debit_spread", 1.0)],
       "regime_2": [("long_straddle", 0.7), ("short_straddle", 0.3)],
       "regime_3": [],  # cash
       ...
   }
   ```

### Discipline gates for 3c

- **Done on TRAIN+VAL combined ONLY.** Holdout untouched.
- **Statistical significance required** (n ≥ 30 per (strategy, regime)
  cell, Sharpe > 1σ above 0). Don't allocate to insignificant
  combinations.
- **No re-fitting strategy params per regime.** The strategy params
  from 3a are fixed; this phase only assigns weights, not new params.

## 7. Phase 3d — Orchestrator design

**Duration:** 2-3 weeks
**Goal:** wire the regime classifier and the strategy-regime map
into a runtime orchestrator that allocates capital each tick.

### Architecture

- New `RuntimeOrchestrator` (in `src/strategy/orchestrator.py`).
- At each tick:
  1. Call `RegimeClassifier.classify(market_state)` → `(regime_id, confidence)`.
  2. If `confidence < 0.70`: 0% allocation everywhere (cash).
  3. Else look up `regime_strategy_map[regime_id]` → list of
     `(strategy_id, weight)`.
  4. For each entry, allocate `weight × total_capital × per_strategy_cap`.
  5. Per-strategy capital cap: 40% max (forces at least 60%
     diversification when multiple strategies fire).
  6. Existing positions follow their strategy's exit logic regardless
     of regime change (don't force-flat on regime transition).
- Each strategy retains its own internal state and exit logic. The
  orchestrator only decides capital allocation for new entries.

### Why soft allocation, not hard switching

Both have research support. Comparison:

**Hard switching** (one strategy active at a time, full capital):
- Operationally simpler.
- Loses the diversification benefit when regime is genuinely mixed.
- Whipsaws on regime transitions (close A, open B).
- Cleaner attribution.

**Soft allocation** (multiple strategies, weighted by regime fit):
- Theoretically Sharpe-optimal under mean-variance framework
  (Markowitz / Black-Litterman).
- Smooths regime transitions (gradual reweighting).
- Reduces transaction costs at boundaries.
- Harder to attribute (P&L mixed across strategies).

For 5 strategies and a 70%-confidence cash floor, soft allocation
wins on the math. But the per-strategy 40% cap means the orchestrator
behaves close to hard switching when regime confidence is high
(top-weighted strategy gets 40% × ~1.0 weight = 40%; others get
40% × ~0.0 = 0%). It's a compromise: smooth at boundaries, decisive
at confident regimes.

### Validation in 3d

End-to-end on TRAIN+VAL combined:
- Run the orchestrator with all 5 strategies, regime classifier from
  3b, strategy-regime map from 3c.
- Compute the full validation report (cpcv_stability, dsr, regime,
  cost, capacity, walk-forward).
- **Holdout NOT touched.**

### Discipline gates for 3d

- **No re-fitting strategy params, no re-clustering.** All upstream
  decisions are frozen; this phase tests the integration only.
- **Single-shot end-to-end VAL evaluation.** Don't iterate on
  orchestrator parameters (40% cap, 0.70 confidence) based on what
  VAL shows. Those are locked here.

### Decision gate (3d → 3e or 3d → simplify)

| Outcome | Next step |
|---|---|
| End-to-end VAL: median CPCV Sharpe > 0.5 AND ≥ 6 of 8 gates PASS AND PBO < 0.3 | Proceed to Phase 3e holdout test. |
| Borderline (4-5 gates PASS, Sharpe 0.0-0.5) | Pause. Investigate via descriptive diagnosis (no tuning). Decide: simplify orchestrator (e.g., disable secondary allocations) and re-run, or retire. |
| FAIL (< 4 gates, Sharpe < 0) | Retire orchestrated approach. Best individual strategy from 3a alone goes to Phase 3e instead. |

## 8. Phase 3e — Final holdout test (single shot)

**Duration:** 1 day
**Goal:** apply the chosen variant (orchestrator + 5 strategies, OR
single best strategy from 3a) to the orchestrator holdout window.
**Single shot, no retry.**

### Pre-requisite

Before 3e, lock the variant being tested. One of:

- **Variant A:** Full orchestrator + 5 strategies (if 3d passed)
- **Variant B:** Best single strategy from 3a (if 3d failed but 3a had a winner)
- **Variant C:** Top 2-3 strategies from 3a in equal-weight static portfolio (no regime conditioning)

Lock the variant, run on holdout, write the report.

### Single-access discipline

The harness's `SplitLoader` already enforces holdout single-access via
`holdout_access.json` lock-file. Don't bypass it. Don't peek at the
holdout to "just check one thing." Don't run multiple variants and
pick the best.

### Decision gate (3e → deploy or retire)

| Outcome | Next step |
|---|---|
| Holdout PASS or near-PASS (≥ 6 gates) AND median Sharpe > 0.5 | Proceed to Phase 4 (paper-trade for 30 days, then 1-lot live). |
| Holdout FAIL (< 4 gates) OR median Sharpe < 0 | **Retire this variant.** Move to next strategy idea or pause systematic premium-selling. |
| Ambiguous (4-5 gates PASS, Sharpe 0.0-0.5) | Pause deployment. **Do not iterate.** Either accept ambiguous result and skip live, or commit to a 6-month research cycle on the next architecture. |

## 9. Phase 4 — Forward live (post-deployment)

**Duration:** 30 days paper + 60 days cautious live at 1 lot
**Goal:** verify holdout result generalises forward.

### Why this matters

No historical validation proves forward stability. Indian options
markets evolved through 2024-2025 (weekly expiry rule, fee changes);
they will evolve further. Forward testing is the only confirmation.

### Daily ritual

- Pre-market: `scripts/verify_system.py`
- Market hours: live trading at 1 lot
- Evening: `scripts/nightly_audit.py` → confluence shadow scorecard
  + per-regime P&L breakdown using Phase 3b's classifier
- Weekly: per-strategy + per-regime attribution report
- **Pause condition:** 5 consecutive losing days OR weekly drawdown >
  3× backtest expectation. Investigate before resuming.

## 10. Cross-phase discipline rules

The four bugs caught in this audit cycle were caused by treating
plausible-looking validation output as ground truth. To prevent the
same pattern in 3a-e:

1. **Never tune parameters by looking at validation output.** Tune on
   train, select on val, report on val ONCE.
2. **Holdouts are single-access.** No peeking. Per-strategy holdouts
   AND the orchestrator holdout each get exactly one read.
3. **No outcome features in regime labeling.** Outcomes only enter
   AFTER labels are derived from at-decision features.
4. **PBO computed for any multi-config search.** PBO > 0.3 (strategy
   level) or > 0.4 (per-strategy grid) → reject.
5. **Coarse grids over fine grids.** 4 values per param, not 9.
6. **One change per validation run.** If 3a tightens stops AND adds
   sizing, validate them together; if it fails, ablate.
7. **No re-running holdout to "see if a tweak helps."** The holdout
   is read once per variant. Tweaks require a new holdout window or
   accepting the current verdict.
8. **MAE / MFE calibration is descriptive, not prescriptive.** It
   informs grids in the NEXT phase, never retroactively changes the
   current phase's selection.
9. **Independent auditor pass before each phase declares done.** The
   pattern from `validation_audit_apr25.md` repeats: a fresh agent,
   no context, reads the proposed phase output and looks for bugs
   the implementer missed.

## 11. Off-ramps — when to retire

Several intermediate decisions can lead to retiring the architecture:

- **3a:** any individual strategy fails its gate → that strategy is out
  but others continue.
- **3a:** ≥ 3 of 5 strategies fail → entire premium-selling architecture
  is questionable; pause and reassess.
- **3b:** clusters don't generalise → no regime conditioning;
  orchestrator simplified to static allocation.
- **3c:** no significant per-(strategy, regime) edge → no regime
  conditioning; same simplification.
- **3d:** end-to-end VAL fails → fall back to best single strategy
  from 3a.
- **3e:** holdout fails → retire variant; no further attempts on this
  strategy family.
- **4:** forward live fails → retire live; investigate root cause
  before any new variant.

## 12. Resource & timeline summary

| Phase | Duration | Compute | Holdouts touched |
|---|---|---|---|
| 3a (5 parallel tracks) | 2-3 weeks | ~200 CPCV runs (~700 worker-hours, ~7 days at 4 workers / 24h) | None |
| 3b (research) | 2-3 weeks | None (analysis only) | None |
| 3c (mapping) | 1-2 weeks | None (uses 3a results) | None |
| 3d (orchestrator) | 2-3 weeks | ~5 CPCV runs | None |
| 3e (final holdout) | 1 day | 1× CPCV on 50-day holdout | **Orchestrator holdout (single shot)** |
| 4 (forward live) | 90 days | n/a | n/a |

**Total elapsed:** ~3 months end-to-end if all phases proceed; ~4
weeks if retire-after-3a; ~6 weeks if retire-after-3b.

**Compute note:** Phase 3a is the heavy hitter. With 5 strategies × ~50
parameter combos each × ~3.5h per CPCV ≈ 875 worker-hours = 36 days
at 4-worker parallelism. Practical compromise: reduce grid resolution
from 4 to 3 values per param (3^4 = 81 combos vs 4^4 = 256), giving
~285 worker-hours ≈ 12 days. Or run grids in parallel across multiple
machines if available.

## 13. What to commit to BEFORE starting

These decisions are locked in this document. Do not adjust mid-flight.

1. **The 5-strategy roster.** Strangle, IC, Straddle, Trend, Long Straddle.
2. **The retire-existing-delta-neutral decision.** Out for Phase 3.
3. **The grid resolutions in §4.** 4 values per param, 3-4 params each.
4. **The K-selection criterion: BIC.** Not silhouette, not AIC, not
   Sharpe spread.
5. **The 0.70 regime confidence threshold.** Below → cash.
6. **The 40% per-strategy capital cap.** No exceptions.
7. **The 3e holdout PASS criteria: ≥ 6 gates, median Sharpe > 0.5.**
8. **All off-ramp commitments in §11.** Retiring a phase requires
   accepting the verdict, not "trying one more thing."

## 14. Open decisions for the operator

These need answers before Phase 3a starts:

- [ ] Phase 3a duration cap: 2-3 weeks acceptable, or compress further
      (reduce grid resolution to 3 values/param)?
- [ ] Per-strategy capital cap: 40% as locked above, or different?
      (30% = more diversification, 50% = more concentration.)
- [ ] Regime confidence threshold for cash: 0.70 as locked, or
      different? (0.60 = more aggressive trading, 0.80 = more
      conservative.)
- [ ] Soft allocation (as planned) or hard switching (operationally
      simpler)?
- [ ] Phase 3e holdout PASS bar: 6 gates + median Sharpe 0.5 (locked)
      or relax to "PASS or near-PASS" judgment call?
- [ ] Phase 4 capital: 1 lot static for all 90 days, or ramp 1 → 5
      based on weekly performance gate?

## 15. Tracking and accountability

- Each phase produces a markdown report in
  `reports/<phase>/<phase>_<version>.md`.
- Each phase updates `memory/MEMORY.md` index.
- Decisions and off-ramp triggers logged in `memory/phase3_decisions.md`
  (new file, created Phase 3a.start).
- Independent auditor pass (per `memory/validation_audit_apr25.md`
  pattern) before each phase is declared done.

## 16. References

### Project-internal

- `memory/validation_audit_apr25.md` — audit cycle that made this plan possible
- `memory/p15_regime_gate.md` — earlier net-negative result, retained infra
- `reports/validation/wide_baseline_portfolio.md` — current baseline
- `docs/ROADMAP_TOP1PCT.md` — broader project roadmap
- `docs/DATA_RELIABILITY_PLAN.md` — data infra
- `docs/SYSTEM_ANALYSIS_2026-04-17.md` — system-wide audit

### Methodological

- López de Prado, *Advances in Financial Machine Learning* (CPCV,
  PBO, DSR formulas the harness uses)
- Lo, *Adaptive Markets Hypothesis* (regime-conditioned strategy
  rationale)
- Israelov & Nielsen, "Covered Calls Uncovered" (vol-risk-premium
  decomposition that motivates per-vega-sign separation)
- Goyal & Saretto, "Cross-Section of Option Returns and Volatility"
  (regime-dependence of strangle/straddle returns)
- Markowitz / Black-Litterman (allocation framework underlying the
  soft-allocation orchestrator)

### Architecture references

- Multi-strategy CTA literature on regime-conditioned allocation
  (AHL, Aspect, Winton public materials)
- Mixture-of-experts ML literature (gating + expert pattern,
  Shazeer et al. 2017)
- `examples/` directory of common Python systematic-trading repos
  (zipline, vectorbt) for orchestrator patterns

---

## Status & next action

This plan supersedes the v1 of `PHASE3_ADVANCEMENT.md`.

The immediate next action is **operator review of §13 (locked
commitments) and §14 (open decisions)**. Until those are committed,
Phase 3a does not start.
