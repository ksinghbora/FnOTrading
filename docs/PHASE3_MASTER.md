# Phase 3 Master Plan — Indian Options Algo Trading System

> **Document version:** v1 (consolidated, supersedes PHASE3_ADVANCEMENT.md
> and PHASE3_PRE_FEE_TRUTHUP.md as standalone docs — both are absorbed
> here)
> **Author / Last edited:** Apr 25 2026
> **Status:** Draft for operator commitment. No work in §V or §VI starts
> until decisions in §VIII are committed.
> **Predecessors:** validation_audit_apr25.md, p15_regime_gate.md
> **Audience:** Operator (primary), future maintainers, independent
> reviewers

---

## Table of contents

```
0.   Executive summary
I.   The audit crisis (what we just learned)
II.  The honest validation (what the bugs were hiding)
III. Architectural reality check
IV.  Independent expert review (push-back received)
V.   Phase 3-Pre: Fee/Slippage Truth-Up (3 days, P0)
VI.  Phase 3: Strategy Advancement (only if 3-Pre PASSes)
VII. Cross-cutting discipline rules
VIII. Decisions required before starting
IX.  Resources, timeline, off-ramps
X.   Files modified this audit cycle
XI.  References & methodology
```

---

# 0. Executive summary

## What this document is

This is the master plan for the next phase of the FnO trading
system. It consolidates the findings of the Apr 25 2026 audit
cycle, the architectural review, the expert third-party reviewer's
push-back, and the resulting roadmap. **It supersedes
`docs/PHASE3_ADVANCEMENT.md` and `docs/PHASE3_PRE_FEE_TRUTHUP.md`
as standalone documents** — both are integrated into Parts V–VI.

## Where we are (Apr 25 2026)

After ~12 months of strategy development, the project hit a wall this
session. While diagnosing why a runtime "regime gate" (the P1.5
experiment) appeared to make things worse, **four critical bugs in
the validation harness itself were discovered**. The harness had
been silently lying about strategy quality for the entire
post-Phase-2 development cycle.

After fixing the bugs, parallelizing the CPCV runner (3.5× speedup
verified deterministic on Apple M4), and re-running the validation
on the full 200-day train+val window:

- **The strategy still fails.** Median CPCV Sharpe +0.47 (down from
  the buggy +0.99); cost-sensitivity FAILs at +0.25 spread shift;
  capacity is negative at the minimum lot size (-₹82/lot at 75
  lots); regime stratification flags `mid_vix` at -0.91 Sharpe.
- **The failure is structural, not random.** Walk-forward windows
  show three consecutive losing test periods from late Mar 2025
  through Jun 2025, while early 2025 windows were strongly
  profitable (+2.59, +5.57, +4.55 Sharpe). The strategy worked in
  late 2024 / early 2025 and stopped working from late Q1 2025.
- **Tail diagnosis identifies vol-expansion as the source.** The
  strategy's *median* trade is still profitable in late 2025
  (median pnl/premium +1.95). What kills it is fat losing tails
  (mean pnl/premium -2.93). Score features do not predict bad days.
  Top losing days had entry scores up to 95.

An independent expert review of the Phase 3 advancement plan flagged
a P0 finding: **before committing 36 worker-days to Phase 3a
optimization, run a 3-day fee/slippage truth-up against the recorded
chain snapshots.** Two market-structure changes in late 2024 (Oct
STT increase on options sell premium, Nov weekly-only rule on NIFTY)
may have destroyed the retail premium-seller edge entirely. If true,
no parameter optimization rescues the strategy.

## The plan

```
┌───────────────────────────────────────────────────────────────┐
│ Phase 3-Pre: Fee/slippage truth-up (3 days, P0)              │
│   Verify costs vs edge at 1-5 lot retail scale on real chain │
│   PASS → proceed to Phase 3                                   │
│   YELLOW → Phase 3 with max_lots=1                            │
│   FAIL → replan (institutional size / different strategies / │
│           retire)                                              │
└────────────────┬──────────────────────────────────────────────┘
                 │ PASS / YELLOW
                 ▼
┌───────────────────────────────────────────────────────────────┐
│ Phase 3a: Per-strategy optimization (2-3 weeks parallel)     │
│   5 strategies independently CPCV-optimized                   │
│   Each gets per-strategy holdout                              │
└────────────────┬──────────────────────────────────────────────┘
                 │
                 ▼
┌───────────────────────────────────────────────────────────────┐
│ Phase 3b: Regime detector design (2-3 weeks, parallel w/ 3a) │
│   HMM on at-decision features, BIC-selected, validity-tested │
└────────────────┬──────────────────────────────────────────────┘
                 │
                 ▼
┌───────────────────────────────────────────────────────────────┐
│ Phase 3c: Strategy-regime mapping (1-2 weeks)                │
│   Per-(strategy, regime) Sharpe matrix → allocation map      │
└────────────────┬──────────────────────────────────────────────┘
                 │
                 ▼
┌───────────────────────────────────────────────────────────────┐
│ Phase 3d: Orchestrator design (2-3 weeks)                    │
│   Volatility-targeting weights, 40% per-strategy cap,        │
│   0.70 confidence floor → cash                                │
└────────────────┬──────────────────────────────────────────────┘
                 │
                 ▼
┌───────────────────────────────────────────────────────────────┐
│ Phase 3e: Final holdout test (1 day, single shot)            │
│   PASS → proceed to Phase 4. FAIL → retire variant.          │
└────────────────┬──────────────────────────────────────────────┘
                 │ PASS
                 ▼
┌───────────────────────────────────────────────────────────────┐
│ Phase 4: Forward live (30d paper + 60d cautious live, 1 lot) │
└───────────────────────────────────────────────────────────────┘
```

Total elapsed: ~3 days for 3-Pre, ~3 months for full Phase 3 if all
gates PASS, ~6-8 weeks if we retire after 3a/3b. Forward live adds 90
days regardless.

## Headline numbers

| Metric | Status |
|---|---|
| Validation harness bugs found this session | **4 (plus 1 latent)** |
| Tests added to lock fixes | **48 unit + 1 integration** |
| Independent auditor confirmed bugs 1-3, hardened bug 4 for parallel | ✓ |
| Parallel CPCV speedup on M4 | **~3.5×** (deterministic) |
| Wide validation result | **FAIL** — but honestly so |
| Failing gates | cpcv_stability, dsr, wf_coverage, mid_vix regime, cost_sensitivity, capacity |
| Median CPCV Sharpe (200 days) | +0.47 |
| Median trade pnl/premium (late 2025) | +1.95 |
| Mean trade pnl/premium (late 2025) | -2.93 |
| Cumulative P&L peak | +₹15,098 on 2025-04-04 |
| Net at end of 200-day window | +₹514 |

The strategy makes money on the median day and gives it back on
~5-10% of vol-shock days, with insufficient cost margin to absorb
realistic Indian options charges.

---

# I. The audit crisis (what we just learned)

This section documents the four bugs in the validation harness that
this session uncovered, and how each was fixed. **Past Apr-22 / Apr-23
/ Apr-25 validation reports are all affected by these bugs and
should not be trusted as evidence of anything.** The relative
gate-on-vs-off comparisons (e.g., the P1.5 net-negative finding) are
robust because both sides used the same buggy harness, but absolute
numbers are wrong.

## 1.1 Bug 1: Stratifier used EXIT-time labels

### Symptom

The Apr 25 P1.5 baseline reported `trending` regime with n=1017
losing trades (Sharpe -7.38). Manual entry-time recomputation showed
n=4 trades. The "1013 missing" trades had entered with `move<1%`
(not trending) and only **became** trending after vol expansion or
intraday move growth — they were never seen by the runtime gate.

### Mechanism

`src/backtest/validation/regime.py::stratify` applied `bucket_row`
to **every row** in the decisions CSV — both ENTER and EXIT rows.
EXIT rows carry exit-time vix/move/dte/is_expiry. So a trade entered
with vix=14, move=0.7 (mid_vix + neither bucket at entry) and exited
with vix=18, move=1.5 (high_vix + trending at exit) was counted in
**high_vix and trending** at EXIT, with its loss attributed there.

### Fix

`stratify` now pairs ENTER and EXIT rows by
`(strategy_id, leg, cumcount-per-decision)` and labels each trade by
ENTRY-row features only, with EXIT pnl. Synthetic test fixtures
without a `decision` column fall through to legacy single-row
labelling — preserves backwards compat for 32 existing tests.

### Tests

3 new in `tests/backtest/validation/test_regime.py`:
- `test_stratify_pairs_enter_exit_uses_entry_time_labels`
- `test_stratify_pairs_per_leg_independently`
- `test_stratify_legacy_fixture_without_decision_column_unchanged`

Plus `tests/unit/test_regime_runtime_parity.py` (14 tests) verifying
runtime classifier matches `bucket_row` thresholds verbatim.

### Verification

`scripts/diagnose_stratifier_mismatch.py` — side-by-side A/B on raw
vs paired labels. Sign-flips on high_vix (-2.32 → +3.64), mid_vix
(+1.34 → -0.64), and trending count (1017 → 4) all confirmed.

## 1.2 Bug 2: CPCV silently ran contiguous slices

### Symptom

CPCV reports showed Sharpe distributions with very tight spreads
(p05=+0.89, median=+0.99, p95=+1.57 across "45 paths"). Path
diversity was illusory.

### Mechanism

`src/backtest/engine.py::BacktestEngine.run` accepted
`start_date + num_days` only — no explicit days list. CPCV
constructed `train_dates = [dates[i] for i in train_idx]` where
`train_idx` is `[0,1,5,6,7,...]` (gaps for held-out test folds).
The runner sent `start_date=train_dates[0]` and
`num_days=len(train_dates)`; the engine then ran the **contiguous
slice** `available[start:start+num_days]` — silently expanding gaps
and **leaking test days into train**.

Concrete reproduction (`scripts/diagnose_*` proof):

```
Path 0 fold_ids=(6,8): engine ran idx 0..43 (contiguous), 6 test days leaked
Path 1 fold_ids=(0,8): engine ran idx 7..51 (contiguous), 4 test days leaked
Path 2 fold_ids=(0,2): engine ran idx 7..51 (IDENTICAL to Path 1)
```

So "5 paths" yielded 4 distinct contiguous slices. The Sharpe
variance under-reported true OOS uncertainty.

### Fix

`engine.run` now accepts an optional `days: list[date] | None`.
When supplied, the engine iterates exactly those days **in order,
gaps preserved**, overriding `start_date + num_days`.
`scripts/validate_strategy.py::build_runner` passes
`days=days_filtered` so CPCV's non-contiguous lists go through
cleanly. Day-selection logic extracted to `_select_trading_days` for
unit-testable isolation.

### Tests

9 new in `tests/unit/test_engine_day_selection.py`:
- `test_explicit_days_non_contiguous_preserves_gaps` (canonical)
- `test_explicit_days_missing_dropped_with_report`
- `test_explicit_days_overrides_start_date_and_num_days`
- 6 legacy-path tests guaranteeing the old API still works

## 1.3 Bug 3: OHLC aggregator persisted candles across days

### Symptom

Until enough today-only candles had closed, callers like
`BaseStrategy._move_from_open_pct`, `BaseStrategy._check_trend_filter`,
and `momentum_breakout` (used by trend-leg breakout detection)
operated on yesterday's late-afternoon candles instead of today's
morning. Affected windows:

- M15 limit=3 (move_from_open + trend filter): wrong from 9:30 →
  10:00 IST every day after day 0
- M5 limit=50 (trend leg morning range): wrong from 10:00 → 13:25
  IST every day after day 0
- M5 limit=10/20 (BankNifty range, regime detector): wrong for the
  first ~50-100 minutes after day 0

### Mechanism

The engine's day-boundary block called
`aggregator._builders.clear()` (in-progress builders only). It never
cleared `aggregator._completed_candles` — the running list of closed
candles across the whole engine run. So at 9:31 IST on day 2,
`get_completed_candles(M15, limit=3)` returned
`[day1 15:00, day1 15:15, day2 9:15]` and `candles[0].open` was day
1's afternoon open, not day 2's session open.

Concrete reproduction (`scripts/diagnose_cross_day_candles.py`):

```
get_completed_candles(M15, limit=3) at 9:31 IST day 2 returns 3 candles:
  [0] date=2024-09-02  start=15:00:00  open=23090.0  close=23018.0
  [1] date=2024-09-02  start=15:15:00  open=23020.0  close=23048.0
  [2] date=2024-09-03  start=09:15:00  open=23200.0  close=23207.0

  → candles[0].open = 23090.0
  → today's actual session open = 23200.0
  *** BUG CONFIRMED: 110 pts off ***
```

### Fix

New public method `OHLCAggregator.clear_day()` resets both
`_builders` AND `_completed_candles`. The engine's day-boundary
block now calls `aggregator.clear_day()`. All `get_candles`
callers already assumed today-only data; this restores their
assumption by construction.

### Tests

4 new in `tests/unit/test_aggregator_day_reset.py`:
- `test_clear_day_wipes_both_builders_and_completed_candles`
- `test_after_clear_day_first_candle_is_todays_open`
- `test_clear_day_idempotent`
- `test_clear_day_followed_by_full_day_returns_only_today`

### Suspected impact on past results

The trend leg's -7.38 Sharpe in "trending" likely partly reflects
*bad breakout signal quality* — `momentum_breakout` was comparing
today's price against **yesterday's afternoon range** for the first
~3 hours of every day. Post-fix data hasn't proven the trend leg
works on a clean window yet; revalidation is part of Phase 3a.

## 1.4 Bug 4: Decisions CSV pollution from accumulated runs

### Symptom

Across the Sep-Dec 2024 train+val window,
`data/decisions/decisions_*.csv` files contained 25,650 rows but
only 637 unique `(timestamp, strategy_id, leg, decision)` tuples —
a **40× duplication factor**. Specific dates like Oct 3 had 159
copies of every decision (with slightly different `rule_score`
values from runs at different code states). The stratifier was
reading every row as a unique trade, inflating regime trade counts
proportionally.

### Mechanism

Each call to `BacktestEngine.run` instantiates a fresh
`PortfolioStrategy`, which constructs a fresh `DecisionLogger` in
**append mode** (because `strategy_id == "portfolio_bt"` doesn't end
with `"_replay"`). Multiple write paths in a single validation
invocation:

1. CPCV evaluates each path → `engine.run(days=train_dates)` per
   path → 50 paths × ~30-60 days each = decisions for every day
   appended ~40 times.
2. Walk-forward → similar appending (when WF windows fit).
3. Full-window run for cost/capacity → one more append per day.

Plus, prior validation invocations across days/sessions left behind
stale rows that were never cleaned up. The CSVs grew unboundedly.

### Effect

The regime stratification's `num_trades` was inflated by ~40×. The
`total_pnl` per regime was also inflated proportionally. Sharpe
(computed from daily-aggregated pnl) was less affected because the
daily groupby flattens duplicates, but `passed = not (sharpe < -0.5
and num_trades > 20)` thresholds based on trade count were misjudged
— buckets that "passed" with n=2516 might have failed with the real
n=63. CPCV / WF / DSR gates were **not** affected: they operate on
`result["metrics"]` returned from `engine.run`, which is computed
from `broker._trades` accumulated during the run — independent of
the decisions CSV.

### Fix

`scripts/validate_strategy.py` now wipes `decisions_YYYY-MM-DD.csv`
files for every day in the validation window **before** the
full-window backtest runs, and the stratifier is moved to run
**after** the full-window backtest writes its (now sole) decisions.
Order is now:

1. CPCV (writes garbage decisions, ignored)
2. WF (writes garbage decisions, ignored)
3. **Wipe decisions for the validation window**
4. Full-window run (clean decisions)
5. Stratifier reads only the full-window's decisions

### Hardening for parallel CPCV

Independent reviewer flagged that with parallel CPCV workers, each
spawning fresh `DecisionLogger`s, multiple processes would race on
`not path.exists()` header checks → duplicate header rows mid-file
→ broken cumcount pairing in the Bug 1 fix.

**Fix:** new env var `FNO_DISABLE_DECISIONS=1` makes
`DecisionLogger.log()` a per-call no-op. Workers set this before any
module imports. The validation harness already wipes decisions
before the full-window run, so workers' decision data was always
going to be discarded; this just stops them from being written in
the first place, eliminating the parallel-write race.

13 new tests in `tests/unit/test_decision_logger_disable.py` lock
the contract.

## 1.5 Additional Bug A (latent, fixed proactively): IV Rank look-ahead

### Symptom (potential)

`load_iv_rank_baseline()` in `portfolio_scoring.py` read the entire
`data/india_vix_minute.csv` and returned the last 252 days' max/min
regardless of the backtest's "now". A Sep 2024 → Apr 2026 backtest
computed every IV-Rank from data through Apr 2026, silently leaking
forward-looking VIX. Currently used only in `iv_rank_shadow_adj`
(shadow log), so doesn't affect P&L today, but the code comment
flagged "Promote to a hard score adjustment once 30+ trading days
correlate" — promotion would silently leak.

### Fix

Added `as_of_date` parameter; the strategy passes
`self.ctx.clock.now().date()` at on_start, slicing the CSV to ≤ that
date. Backwards compatible (default None preserves legacy behaviour).

## 1.6 Independent auditor verification

A separate audit agent verified each fix against the source code
without seeing the original reasoning:

- **Bug 1 (CORRECT):** pairing logic preserves legacy behaviour;
  defensive inner-merge avoids silent data loss; 21 regime tests pass.
- **Bug 2 (CORRECT):** engine honors gaps; build_runner passes
  explicit list; 9 day-selection tests pass.
- **Bug 3 (CORRECT):** clear_day placement is correct; all callers
  benefit. RegimeDetector unaffected.
- **Bug 4 (INCOMPLETE for parallel):** wipe-then-run correct for
  serial, but parallel CPCV workers would race on header checks.
  **Hardened by FNO_DISABLE_DECISIONS env var** + 13 tests.
- **Additional Bug A (medium-severity latent):** IV Rank look-ahead.
  Auditor surfaced it; fixed inline.

Three low-severity quality issues also surfaced and **deferred**:
`pnl_per_lot` metric mislabel, stale `transaction_charges.options_pct`
rate (~43% pessimistic, not a look-ahead), `metrics.py` win-rate
gross-vs-net asymmetry. None block validation correctness.

## 1.7 Parallelization (post-audit)

`scripts/validate_strategy.py --workers N` now runs CPCV paths
across N subprocesses via `concurrent.futures.ProcessPoolExecutor`
(spawn start method). Each worker:

1. Sets `FNO_DISABLE_DECISIONS=1` BEFORE any engine/strategy imports.
2. Reconstructs `BacktestEngine` + `GDFLMarketSource` from a frozen
   `RunnerSpec` dataclass (picklable: strings/ints/floats only — no
   live objects cross the process boundary).
3. Derives a deterministic per-path seed:
   `path_seed = base_seed * 2654435761 + path_id` (Knuth
   multiplicative hash, modulo 2³¹−1).
4. Runs `engine.run(days=train_dates_for_this_path, seed=path_seed,
   ...)` in its own asyncio event loop.
5. Returns `(path_id, metrics_dict)` to the parent for stitching by
   path_id.

**Determinism contract:**
`tests/integration/test_parallel_cpcv_determinism.py` runs CPCV with
`n_workers=1` and `n_workers=2` against the same seed and asserts
bit-identical per-path Sharpe / num_trades / total_pnl. **Verified
passing.** The parallel codepath is a pure speedup, never a
behaviour change.

**Speedup:** Apple M4 has 4 performance + 6 efficiency cores. With
`--workers 4`, all 4 perf cores saturate; ~3.5× wall-clock speedup
verified on the 200-day validation (~3.5h instead of ~12h
extrapolated sequential).

---

# II. The honest validation (what the bugs were hiding)

After all 4 bug fixes + parallelization, we re-ran validation on the
full 200-day train+val window (Sep 2024 → Jun 2025) with the 50-day
holdout (Jun 25 → Sep 5 2025) reserved.

## 2.1 Final verdict: FAIL — but cleanly

| Gate | Status | Detail |
|---|---|---|
| cpcv_stability | FAIL | median +0.47 (was +1.06 buggy on 82d), p05 -0.43 |
| cpcv_pbo | PASS | not computed (single-config CPCV) — WARN |
| dsr | FAIL | 0.0 — high path variance now visible |
| wf_decay | **PASS** | median_decay -0.50 (test > train, healthy) |
| wf_coverage | FAIL | 3/6 windows positive (need ≥0.7) |
| regime: mid_vix | **FAIL** | -0.91 Sharpe, n=103 |
| cost_sensitivity | **FAIL** | breaks even at +0 shift, fails at +0.25 |
| capacity | **FAIL** | -₹82/lot @ 75 lots — unprofitable at any retail size |

## 2.2 The walk-forward windows tell the story

| Window | Train end | Test period | Test Sharpe | Trades |
|---|---|---|---|---|
| 0 | 2025-01-10 | Jan 14 - Feb 24 | **+2.59** | 156 |
| 1 | 2025-01-31 | Feb 4 - Mar 19 | **+5.57** | 132 |
| 2 | 2025-02-21 | Feb 25 - Apr 11 | **+4.55** | 112 |
| 3 | 2025-03-18 | Mar 20 - May 7 | **−2.82** | 136 |
| 4 | 2025-04-09 | Apr 15 - May 28 | **−2.16** | 180 |
| 5 | 2025-05-06 | May 8 - Jun 18 | **−1.23** | 176 |

Three consecutive losing test windows is not noise — it's a regime
shift. Trade count even *increases* (180 trades in window 4 vs 132
in window 1) while Sharpe collapses → strategy is firing more often
into worse conditions.

## 2.3 Tail diagnosis findings

`scripts/diagnose_2025_regime_shift.py` — strictly descriptive, no
overfitting:

**Entry frequency dropped in late 2025:** 28 entries (Jan 2025) →
~20 entries (Mar-Jun avg). Counter to "over-entering" hypothesis.

**VIX environment shifted up:** mean VIX at entry rose from 15.05
(EARLY) → 15.88 (LATE). Strategy entered in higher-VIX conditions
where premium is fatter — bigger to win OR lose.

**Median trade still profitable in late 2025:**

| Bucket | Median pnl/premium | Mean pnl/premium |
|---|---|---|
| EARLY (≤Feb 2025) | +3.04 | −0.68 |
| MAR (Mar 2025) | +7.12 | +4.51 |
| LATE (≥Apr 2025) | +1.95 | **−2.93** |

The median stays positive (typical day still works) but the **mean
diverges sharply negative in LATE** → losing days got materially
bigger.

**Cumulative P&L peaked 2025-04-04 at +₹15,098** then gave back
₹14,584 over the next 11 weeks, ending at +₹514.

**Top 10 losing days share a signature** — small intraday move
(0.2-0.5%) with what must have been vol expansion. **High score did
NOT protect:** 2025-04-16 lost ₹3,945 with score=95, 2025-06-16 lost
₹5,079 with score=76. Score is uncorrelated with vol-shock days.

**Score-vs-threshold drift:** by 2025-05, average final_score (65.4)
was BELOW average threshold (67.7), suggesting the threshold
ratcheted up (probably VIX-adaptive) faster than the strategy's
score-generating signals improved. Borderline entries are getting
through.

## 2.4 Honest interpretation (no fix prescription)

The diagnosis points to **risk management, not entry selection**.
The strategy's typical day still works in 2025; what's killing it
is **a fatter losing tail** — vol-expansion events on chop-bound
days where the strategy expected decay but got premium expansion.
Score features cannot predict these intraday vol shocks.

This is consistent with broader Indian options 2025 reality: post-
Nov-2024 weekly-only expiry rule, IV term-structure compressed, and
0DTE-style premium-selling has tighter margins.

---

# III. Architectural reality check

## 3.1 The current "Portfolio strategy" is an anti-pattern

The existing strategy (`src/strategy/implementations/portfolio_strategy.py`)
runs two legs simultaneously:

- **Premium leg:** IC (VIX≥12) or strangle (VIX<12) at 9:30+ on
  signal score ≥ 65
- **Trend leg:** debit spread at 10:00+ on confirmed breakout

Both share state, share the strategy ID, share the same context.
This conflates "which strategy to run" with "how to run it":

- When the combined strategy fails, you can't tell which leg is the
  problem.
- When one leg works, you can't tell which regime it works in.
- Parameters are global; can't tune per-strategy without affecting
  others.

**Three diagnostic facts** from this audit cycle proved the
architecture is broken:

1. **Trend leg loses in its own designed regime** — when measured
   with the now-fixed stratifier, trend showed -7.38 Sharpe in
   "trending" (its designed regime).
2. **Premium leg has fat-tail vol-expansion risk** — scores don't
   predict vol-shock days. High-score entries (score 95) lose just
   as much as low-score entries.
3. **The legs aren't actually independent** — both fire on similar
   conditions (chop days), both lose on shock days. Correlated, not
   complementary.

The architecture **looks** regime-aware but in practice is **two
correlated bets on "decay will happen today"**, not a regime-
conditioned hedge.

## 3.2 The vega-sign portfolio gap

| Strategy | Delta | **Vega** | Gamma | Theta | Profits from |
|---|---|---|---|---|---|
| Short Strangle | ~0 | **negative** | negative | positive | Vol contraction, time decay |
| Iron Condor | ~0 | **negative** | negative | positive | Vol contraction, time decay |
| Short Straddle | ~0 | **negative** | negative | positive | Vol contraction, time decay |
| Trend Debit Spread | +/− | ~0 | ~0 | slight neg | Directional move |
| Delta-Neutral (existing) | ~0 (hedged) | **negative** | negative | positive | Vol contraction (purer) |

**Every premium strategy is short vega. There is no positive-vega
strategy in the portfolio.** That's the architectural hole — when
2025 brought vol expansion, every strategy in the stack lost money.

## 3.3 The Long-vega strategy gap (and why existing delta_neutral.py won't fix it)

The existing `src/strategy/implementations/delta_neutral.py` is
short straddle/strangle + futures delta hedge. It's a SHORT-vol
strategy with delta hedging. Same fundamental edge as IC/strangle
(short vol-risk-premium); just captures it more purely. **Same
vol-expansion failure mode.** Adding it doesn't add diversification.

The honest fix is a **LONG-vol strategy** that profits when premium-
sellers lose:

| New strategy | Delta | **Vega** | Profits from |
|---|---|---|---|
| Long Straddle | ~0 | **positive** | Vol expansion, large move |
| Long Calendar | ~0 | positive (front-month) | IV term-structure normalization |
| Long Strangle (cheap) | ~0 | **positive** | Vol expansion, lower theta cost |

This is what real vol-arb funds run — a barbell of short-premium and
long-premium strategies, regime-allocated.

---

# IV. Independent expert review (push-back received)

A senior systematic options trader and quant researcher reviewed the
draft Phase 3 plan. Their feedback materially changed the plan.

## 4.1 P0 finding — fee/slippage truth-up before optimization

> "If BS-vs-real gap is >70% on OTM strangles at 1–5 lot retail
> size, you have no edge to optimize and the entire plan is
> redirected. Backtest the *current* portfolio strategy's P&L
> against actual recorded mid-and-spread prices for those 4 weeks.
> This is a 2–3 day analysis and could obviate the entire
> 36-worker-day Phase 3a."

Two market-structure changes in late 2024 may have destroyed the
retail premium-seller edge:

1. **Oct 1 2024:** STT on options sell premium increased
   (rate to be verified — reviewer estimates 0.1%, project memory
   states 0.125%; truth-up will confirm against budget memorandum)
2. **Nov 2024:** SEBI restricted weekly expiries to NIFTY only.
   BANKNIFTY/FINNIFTY moved to monthly-only.

These are absorbed as **Phase 3-Pre** (Part V).

## 4.2 Strategy roster corrections

| Reviewer call | Change |
|---|---|
| **Drop Short Straddle** | Just a delta=0.50 strangle. Three "premium" strategies are three flavors of one bet. |
| **Replace Long Straddle with Long Calendar OR Long 0.10Δ Strangle** | Long Straddle is too expensive (1-1.5% of spot), needs >2% move in 2-3 days, hits ~15% of weeks. Long Calendar is positive vega + near-zero theta + cheap. |
| **ADD Iron Butterfly** (ATM short straddle + protective wings) | Structurally highest theta in the chain post-STT change |
| **ADD NIFTY/BANKNIFTY relative-vol pair** | Nov 2024 weekly removal dislocated BANKNIFTY monthly IV — vega-neutral spread trade |
| Skip 0DTE (HFT-dominated at retail), skip event-driven-as-strategy | Operational reality |
| Trend Debit Spread is the urgent retirement candidate | Bug 3 disclosure — its -7.38 Sharpe was partly artifact, but post-fix data hasn't proven it works on a clean window |

## 4.3 Indian-market-specific corrections

| Issue | Detail |
|---|---|
| STT magnitude | Reviewer 0.1%, memory 0.125%. ~25% magnitude difference. **Verify against budget memo before Phase 3-Pre runs.** |
| OTM bid-ask | Real touch-spread on NIFTY weekly 0.15Δ strikes is 0.5-1.0₹ on ₹40-60 mid (1-3% one-way, 2-6% round trip). +0.25 cost shift = half a touch-spread. Strategy already FAILs at +0.25; **revise cost gate to PASS at +0.50** (full touch-spread) or accept retail unviability. |
| BANKNIFTY opportunity | Monthly IV elevated 2-4 vol points above NIFTY post-rule — dislocation worth scoping at Phase 3+. |
| Margin reality | IC margin ~₹50-60K/lot (defined risk benefit); naked Strangle ~₹1.2-1.5L/lot. **Orchestrator allocator must be margin-ratio-aware, not just weight-aware.** |

## 4.4 Architecture concerns

1. **GMM is wrong default → use HMM.** Plan applies 5-min majority-
   vote hysteresis on top of GMM, recovering HMM-like behavior with
   extra steps. Use HMM directly.
2. **Soft allocation + Markowitz on 5 strategies × 200 days is
   ill-conditioned.** Use volatility-targeting weights
   (1/realized-vol, normalized) clamped to 40% cap. Robust,
   parameter-free, barely differs from optimum at 1-4 lot scale.
3. **Independence is fiction.** Strangle/IC have correlation ~0.7 in
   vol shocks. Compute and report strategy P&L correlation matrix on
   TRAIN; if any pair > 0.6, orchestrator treats them as one.
4. **6 holdouts → 1 common holdout.** Per-strategy holdouts (5) +
   orchestrator holdout (1) = 6 windows with implicit cross-leakage.
   Use one common 90-day holdout (Jul-Sep 2025), or compute
   multiple-comparison-adjusted DSR across all six.
5. **Use full 18 months.** Plan uses Sep 2024 → Jun 2025 (9 months);
   Aug 2025 → Feb 2026 untouched. **The post-Mar-2025 data is exactly
   where the strategy died** — using only the regime where it works
   is selection bias.

## 4.5 Validation discipline corrections

| Setting | Plan v2 | Reviewer's call |
|---|---|---|
| PBO threshold | < 0.3 strategy / < 0.4 grid | < 0.5 binding (López de Prado canonical), < 0.4 yellow flag |
| DSR target | > 0.95 | Unreachable on 9 months. Use PSR > 0.95 or extend window |
| wf_coverage | 0.7 fraction-positive | Wrong-shaped — penalizes legitimate cash periods. Use (test_sharpe > train_sharpe × 0.5) |
| 50-day holdouts | Locked PASS criteria | Insufficient power. Extend to 90 days OR relax bar |
| Capacity gate | "FAIL" | Binding constraint, not a metric. Solve via better fill model FIRST. |

## 4.6 Missing pieces (most important)

1. **Fee/slippage truth-up on chain data BEFORE Phase 3a** — 3 days,
   could save 6 weeks
2. **Portfolio-level Greek budgets** (gross gamma/vega/theta caps) —
   what Optiver/SIG actually do; structural answer to vol-expansion
   risk
3. **Failure-mode circuit breakers** — runtime detectors for
   vol-expansion >30% in 30min, gap >1.5%, NIFTY/BANKNIFTY
   divergence; pause trading on trigger
4. **Live-paper run during 3a** (not after 3e) — catches operational
   issues
5. **Synthetic regime stress tests** before 3e (Mar-2020 gap,
   Aug-2024 VIX spike, etc.)

## 4.7 Off-ramp realism

> "After 36 worker-days of compute and 6 weeks of calendar time, if
> 3 of 5 strategies fail their 3a gate the plan asks the operator
> to declare 'the entire premium-selling architecture is questionable;
> pause and reassess.' That decision is going to feel like throwing
> away 18 months of work — the plan needs a **pre-committed,
> written, single-number rule** the operator signs before Phase 3a
> starts."

The Phase 3e "ambiguous → pause deployment, do not iterate" gate is
the single most predictable failure point. Reviewer recommends a
clause: **if ambiguous, the variant goes to 4 weeks of paper trade,
not deployment, not deletion.**

## 4.8 Compute reality

> "36 days at 4 workers is unrealistic for a solo operator on an M4
> — the machine has other jobs, OS updates, thermal throttling.
> Practical answer: rent 1× c7i.4xlarge spot (~$0.30/hr × 100 hours
> = $30) and run the full 5-strategy grid in 2-3 days."

Recommend cloud compute as default; M4 path as fallback.

---

# V. Phase 3-Pre: Fee/Slippage Truth-Up (P0, 3 days)

## 5.1 Goal

Verify whether the strategy has any positive net edge at retail
scale (1-5 lots) under realistic Indian market costs, BEFORE
committing 36 worker-days to Phase 3a optimization.

If the answer is no, Phase 3 is replanned. If yes, Phase 3 proceeds
with the reviewer's other corrections incorporated.

## 5.2 Data we have

19 days of recorded chain snapshots in `data/chain_snapshots/`
(Mar 25 → Apr 24 2026):

```
chain_2026-03-25.csv  (first)
...
chain_2026-04-24.csv  (last)
```

Schema (one row per (minute, strike, option_type)):

```
time, underlying, expiry, strike, option_type,
ltp, iv, delta, gamma, theta, vega,
oi, volume,
bid_price, ask_price
```

**bid_price and ask_price are recorded from the actual NSE quote
feed**, not modeled. This is the truth source for execution cost
analysis.

Plus:
- `data/gdfl_snapshots/` parquet covering Sep 2024 → Feb 2026
- `data/decisions/decisions_2026-*.csv` from wide validation runs
- `src/portfolio/charges.py` — already implements all Indian charges

## 5.3 Hypotheses tested

**H1 (cost magnitude):** Total round-trip costs (STT + exchange +
SEBI + GST + stamp duty + brokerage) on a typical NIFTY weekly
0.20Δ short strangle exceed 8% of gross premium collected.

**H2 (slippage reality):** GDFL parquet's bid/ask are **materially
tighter** than chain_snapshot bid/ask for the same (minute, strike).
If true, validation has been understating execution cost.

**H3 (size scaling):** Net P&L per lot turns negative between 1 and
5 lots due to spread crossing.

**H4 (post-Oct-2024 edge degradation):** Strategy's net edge per
trade in the 19-day chain window is meaningfully lower than the
GDFL-modeled edge would suggest.

## 5.4 Methodology

### 5.4.1 Verify exact charge rates (Day 1 morning)

| Charge | Project memory | Reviewer | Authoritative source to verify |
|---|---|---|---|
| STT (options sell premium) | 0.125% | 0.1% | FY2024-25 Budget Memorandum |
| STT (options exercise on intrinsic) | 0.125% | 0.125% | NSE STT circular |
| Exchange transaction (F&O options) | 0.05% | 0.0353% | NSE F&O fee schedule |
| SEBI turnover | 0.0001% | 0.0001% | SEBI circular |
| GST | 18% on (brokerage + exchange + SEBI) | 18% | CBIC standard |
| Stamp duty (options buy) | 0.003% | 0.003% | State-specific |
| Brokerage (Zerodha) | Per-order cap in constants.py | Same | Zerodha pricing |

**Action:** if any rate in `src/core/constants.py::CHARGES` differs
from authoritative source, fix the constant. **Only code change
permitted in Phase 3-Pre.**

### 5.4.2 GDFL vs chain_snapshot price reconciliation (Day 1)

Build `scripts/truthup_compare_price_sources.py`:

1. For each chain_snapshot date, load both GDFL parquet day and
   chain_snapshot day.
2. Join on `(timestamp, strike, option_type, expiry)`.
3. For each match, compute `bid_diff`, `ask_diff`,
   `spread_chain`, `spread_gdfl`, `spread_ratio`.
4. Stratify by:
   - Strike distance from spot (ATM, ±100, ±200, ±400, ±600 pts)
   - Days-to-expiry (0, 1-3, 4-7)
   - Time-of-day (open / midday / close)

**Pass criterion for the data source:** median `spread_ratio` < 1.5
across all strata. If chain spreads are systematically 2× wider than
GDFL spreads, validation has been understating slippage.

### 5.4.3 Re-replay strategy entries on chain prices (Day 2)

Build `scripts/truthup_chain_replay.py`:

1. Load decisions CSVs from wide validation for the 19 chain dates.
2. For each ENTER / EXIT, look up actual chain_snapshot bid_price
   (for SELL fills) or ask_price (for BUY fills) at that minute and
   strike.
3. Compute fill price assuming **cross-spread** execution
   (taker-side; SELL @ bid, BUY @ ask).
4. For each round-trip, compute:
   - Gross P&L at GDFL prices (validation's number)
   - Gross P&L at chain prices (truth)
   - Slippage gap = GDFL gross − chain gross
5. Compute charges via `src/portfolio/charges.py` for each leg using
   chain-truth prices. Sum per round-trip.
6. **Net P&L truth** = chain gross − total charges.

### 5.4.4 Per-strategy net edge analysis (Day 2)

For each strategy that fired in the 19-day window:

| Metric | At 1 lot | At 5 lots | At 10 lots |
|---|---|---|---|
| Total gross P&L (GDFL) | | | |
| Total gross P&L (chain truth) | | | |
| Slippage gap (₹) | | | |
| Slippage gap (% of gross) | | | |
| Total charges | | | |
| Charges (% of gross) | | | |
| **Net P&L** | | | |
| Net Sharpe | | | |
| Net win rate | | | |

For lots > 1, walk the order book conservatively (each additional
lot crosses 0.05₹ deeper). Likely an underestimate; if even
conservative model fails, multi-lot scale is unviable.

### 5.4.5 Cost decomposition (Day 2)

Per-trade-leg breakdown: STT / Exchange / SEBI / GST / Stamp /
Brokerage / Slippage as % of gross premium. Identifies where cost
concentrates — useful for replan if FAIL.

## 5.5 Decision criteria (locked before Day 3)

### 5.5.1 PASS — proceed to Phase 3 with reviewer corrections

All of:
- 1-lot net Sharpe over 19 days > **0.5**
- 5-lot net Sharpe > **0**
- Median net P&L per trade > **+₹50**
- No single charge component > 50% of gross edge

### 5.5.2 YELLOW — viable only at minimum size

Mixed: 1-lot net Sharpe > 0.5 BUT 5-lot net Sharpe < 0.

**Decision:** proceed with Phase 3 BUT lock `max_lots = 1` in all
subsequent optimization. Capacity is the binding constraint.

### 5.5.3 FAIL — replan required

Any of:
- 1-lot net Sharpe < 0
- Median net P&L per trade < 0
- Slippage gap > 70% of gross premium
- A single charge component > 50% of gross edge

**Replan options (no commitment, just signal):**
- (a) Larger size where we receive spread (institutional 50+ lots;
  retire current operator scope)
- (b) Different strategies (Iron Butterfly, NIFTY/BANKNIFTY vol-arb,
  Long Calendar) that don't depend on OTM premium-selling edge
- (c) Retire systematic premium-selling at retail scale

## 5.6 Discipline rules

1. **No tuning during truth-up.** Measurement, not optimization.
2. **No cherry-picking dates.** Anomalous data documented and
   excluded transparently — never filter post-hoc.
3. **Holdouts not touched.** The 19-day chain window is post the
   200-day train+val. Natural forward-time sample, not held-out
   window. Phase 3's TRUE holdout (Aug 2025 → Feb 2026 with full
   18-month expansion) is reserved.
4. **Charge constants must match authoritative sources.**
5. **No iterating on §5.5 cutoffs.** Locked. Borderline → accept,
   don't relax.

## 5.7 Deliverables

- `scripts/truthup_compare_price_sources.py` (Day 1)
- `scripts/truthup_chain_replay.py` (Day 2)
- `scripts/truthup_cost_decompose.py` (Day 2)
- `reports/phase3_pre/fee_truthup_v1.md` — verdict report (Day 3)
- `memory/phase3_pre_truthup_apr25.md` — short summary indexed in
  MEMORY.md (Day 3)

## 5.8 Off-ramps

If during truth-up we discover:

- **Charge rates wrong by > 10%:** stop, fix constants, re-run all
  prior validation reports.
- **Chain snapshot data corrupt:** truth-up moot; pause Phase 3.
- **GDFL parquet doesn't have bid/ask (only ltp):** Bug 5 of audit
  cycle. Pause Phase 3 until fill model is rebuilt against
  chain_snapshot data.

---

# VI. Phase 3: Strategy Advancement (only if 3-Pre PASSes)

This section is the architecture-level redesign that replaces the
combined "Portfolio strategy" with **independent strategies +
separate regime detector + soft-allocation orchestrator**.

## 6.1 Strategic framing — separation of concerns

```
                    ┌─────────────────┐
                    │ Regime Detector │  (one model: "which regime")
                    └────────┬────────┘
                             │ regime_id, confidence
                             ▼
                    ┌─────────────────┐
                    │   Orchestrator  │  ("which strategy gets capital")
                    └────────┬────────┘
                             │ allocation per strategy
            ┌────────────────┼─────────────────────┬──────────────┐
            ▼                ▼                     ▼              ▼
      ┌──────────┐   ┌─────────────┐   ┌──────────────────┐   ┌─────────────┐
      │ Strangle │   │ Iron Condor │   │ Iron Butterfly   │   │ Long        │
      └──────────┘   └─────────────┘   └──────────────────┘   │  Calendar   │
                                                              └─────────────┘
        (each strategy: "how to trade well in its conditions")
```

## 6.2 Strategy roster (incorporating reviewer corrections)

| # | Strategy | Delta | **Vega** | Designed regime |
|---|---|---|---|---|
| 1 | **Short Strangle** | ~0 | negative | Low-mid VIX, range-bound |
| 2 | **Iron Condor** | ~0 | negative | Mid VIX, range-bound, capped risk |
| 3 | **Iron Butterfly** *(NEW)* | ~0 | strong negative | Highest theta — protective wings + ATM short straddle |
| 4 | **Long Calendar** *(NEW)* | ~0 | **positive (front-month)** | Low IV, term-structure normalisation |
| 5 | **Trend Debit Spread** (re-validation) | +/− | ~0 | Trending, breakout — retire if 3a fails |

**Explicit removals** from earlier draft:
- Short Straddle (= delta-0.5 strangle, redundant with #1)
- Long Straddle (replaced by Long Calendar — cheaper, lower theta cost)
- Existing `delta_neutral.py` (same vega sign as IC, no diversification)

**Optional Phase 3+ additions (not in initial 3a):**
- NIFTY/BANKNIFTY relative-vol pair (post-Nov-2024 dislocation)
- 0DTE strategies (deferred — HFT-dominated at retail size)
- Event-driven (use as filter, not strategy)

## 6.3 Per-strategy design principles

Each of the 4 (or 5 if Trend revives) strategies must satisfy:

1. **One clear edge.** Strangle = vol-risk-premium. Iron Butterfly =
   max ATM theta. Long Calendar = vega + theta differential.
2. **Explicit failure modes codified** in docstring AND used by
   regime gate.
3. **Stop-loss calibrated by MAE** — set SL above 95th percentile of
   max-adverse-excursion-on-winners.
4. **Profit-target calibrated by MFE** — typically 60th percentile
   of MFE-on-winners.
5. **Per-strategy capacity** — max lot size before slippage erodes
   edge, baked into params.
6. **Per-trade risk capped at fractional Kelly** — half-Kelly ceiling.

## 6.4 Phase 3a — Per-strategy independent optimization

**Duration:** 2-3 weeks (4 parallel tracks)
**Goal:** for each strategy, find best params on TRAIN, validate on
VAL with post-audit harness, characterize favorable regime.

### Per-strategy CPCV grid (locked)

**Short Strangle:**
- entry_score_threshold ∈ {55, 60, 65, 70}
- premium_call_delta ∈ {0.15, 0.20, 0.25, 0.30}
- premium_stop_loss_pct ∈ {20, 25, 30, 35}
- premium_profit_target_pct ∈ {40, 50, 60, 70}

**Iron Condor:** + wing_width_atm_pct ∈ {1.5, 2.0, 2.5, 3.0}

**Iron Butterfly:** ATM strike fixed, wings ∈ {0.75, 1.0, 1.25, 1.5} × daily SD

**Long Calendar:**
- short_expiry_dte ∈ {3, 5, 7}
- long_expiry_dte_offset ∈ {7, 14, 21}
- entry_iv_percentile_max ∈ {15, 20, 30, 40}
- exit_theta_decay_pct ∈ {30, 40, 50}

**Trend Debit Spread (only if Bug-3-clean revalidation shows edge):**
- breakout_confirmation_pct ∈ {0.3, 0.4, 0.5}
- trend_stop_loss_pct ∈ {15, 20, 25, 30}
- min_trend_duration_minutes ∈ {30, 45, 60}

### Discipline gates for 3a

- **No re-running with different grid bounds.** Locked above.
- **No iteration on validation feedback.** Picked params fail VAL →
  strategy retired, not re-tuned.
- **PBO computed across grid.** PBO > **0.5 binding** (López de
  Prado canonical, reviewer correction), > 0.4 yellow flag.
- **MAE/MFE calibration descriptive only in 3a.** Informs next-
  iteration grids if there's appetite, doesn't retroactively change
  Phase 3a winners.

### Per-strategy decision gate

| Outcome | Status |
|---|---|
| VAL Sharpe ≥ 0.5 AND PBO ≤ 0.5 AND ≥ 5 of 8 gates PASS | Include in regime detector design (3b) |
| VAL Sharpe < 0.5 OR PBO > 0.5 OR ≤ 3 gates PASS | Retire from Phase 3 |
| Borderline (Sharpe 0.5-0.0, gates 3-4) | Keep but flag — orchestrator gives small allocation only |

### Compute reality — use cloud

5 strategies × ~50 grid combos × ~3.5h CPCV ≈ 875 worker-hours = 36
days at 4-worker M4 parallelism. **Recommended:** rent 1×
c7i.4xlarge spot (~$0.30/hr × 100hr = $30), run in 2-3 days. M4 path
as fallback only.

## 6.5 Phase 3b — Regime detector design

**Duration:** 2-3 weeks (parallel with 3a)
**Goal:** unsupervised regime discovery on at-decision features.
**No strategy code changes.** Pure research.

### Why HMM not GMM (reviewer correction)

The original plan used GMM + 5-min majority-vote hysteresis +
confidence threshold. Reviewer pointed out this is recovering
HMM-like behavior with extra steps. Use HMM directly:

- Gaussian-emission HMM with K=2-4, BIC-selected
- Transition matrix bakes in state persistence (no hysteresis hack)
- Per-state likelihood gives proper confidence threshold
- Same implementation effort as GMM

### Pipeline

1. **Feature extraction.** Per-minute features (NO outcome features):
   - vix, vix_5m_change, vix_30m_change
   - intraday_range_pct (realized intraday vol proxy)
   - time_of_day_bucket (morning/midday/afternoon)
   - dte, is_expiry_week, is_expiry_day
   - pcr_oi, iv_skew_ratio
   - move_from_open_pct, morning_range_pct
   - banknifty_correlation_30m
2. **Standardise.** z-score per feature, fit on TRAIN only.
3. **Fit HMM.** K = {2, 3, 4, 5}, BIC-selected.
4. **Confidence threshold:** posterior P(top state) > 0.70 → emit
   label; else `regime_uncertain` → cash.
5. **Cluster validity test** (CRITICAL anti-overfit step):
   - Split TRAIN chronologically into halves T1, T2.
   - Fit HMM on T1; use T1 emission params + transitions to label T2.
   - Compare per-state feature means in T1 vs T2; require within 1σ.
   - If states don't generalise, **regime detector rejected**;
     Phase 3c-e proceed without it (or are canceled).
6. **Information content test.** Per-strategy Sharpe by HMM state
   on TRAIN vs by rule-based VIX bucket. Require: HMM spread >
   rule-based spread by ≥ 1σ.

### Discipline gates for 3b

- **No outcome features in HMM inputs.**
- **K chosen by BIC, locked before per-state Sharpe is measured.**
- **Validity test BEFORE Sharpe analysis.** If states don't
  generalise, report says so, 3c canceled.
- **No retro-labeling.** "Days where premium-decay > X = state A"
  forbidden.

## 6.6 Phase 3c — Strategy-regime mapping

**Duration:** 1-2 weeks
**Goal:** for each regime label from 3b, identify which strategy
"owns" that regime via per-(strategy, regime) Sharpe matrix.

### Pipeline

1. For each regime label, slice TRAIN+VAL data to bars in that
   regime.
2. For each strategy from 3a (passed gate), compute Sharpe on that
   regime's bars using 3a-selected params.
3. Build matrix `(regime_id × strategy)` of Sharpe values.
4. Per regime, identify:
   - **Primary strategy:** highest Sharpe > 0.5 AND statistically
     significant (n ≥ 30, Sharpe > 1σ above 0)
   - **Secondary strategies:** Sharpe > 0 AND > 0.5σ above 0
   - **No-fire regimes:** no significant strategy → cash
5. Build the `regime_strategy_map` lookup table.

### Discipline gates for 3c

- **TRAIN+VAL combined ONLY.** Holdout untouched.
- **Statistical significance required** (n ≥ 30, Sharpe > 1σ).
- **No re-fitting strategy params per regime.** Params from 3a
  fixed; 3c only assigns weights.

## 6.7 Phase 3d — Orchestrator design

**Duration:** 2-3 weeks
**Goal:** wire regime classifier + strategy-regime map into runtime
orchestrator that allocates capital each tick.

### Architecture (with reviewer corrections)

- New `RuntimeOrchestrator` in `src/strategy/orchestrator.py`.
- Per-tick:
  1. `RegimeClassifier.classify(market_state)` → `(regime_id, conf)`.
  2. If `conf < 0.70`: 0% allocation everywhere (cash).
  3. Else look up `regime_strategy_map[regime_id]` → list of
     `(strategy_id, weight)`.
  4. **Volatility-targeting weights** (reviewer correction): each
     strategy's weight scaled by `1 / realized_vol_30d`, normalized,
     clamped to 40% per-strategy cap.
  5. Margin-ratio-aware allocator (reviewer correction): translate
     weights into lot counts respecting margin requirements
     (IC ~₹50-60K/lot; Strangle ~₹1.2-1.5L/lot; etc.).
  6. Existing positions follow their strategy's exit logic
     regardless of regime change (don't force-flat).
- Each strategy retains internal state and exit logic. Orchestrator
  only decides capital allocation for new entries.

### Why volatility-targeting weights (reviewer)

> "Markowitz/BL is ill-conditioned with n=5 strategies and 200 days
> of data — you're estimating a 5×5 covariance matrix from ~150
> train days where most strategies don't fire on most days. You'll
> get unstable weights. Use volatility-targeting weights
> (1/realized-vol per strategy, normalized) clamped to the 40% cap.
> It's robust, parameter-free, and in 1-4 lot regime barely differs
> from the optimum."

### Validation

End-to-end on TRAIN+VAL combined with all surviving strategies +
HMM regime detector + strategy-regime map. Compute full validation
report. **Holdout NOT touched.**

### Discipline gates for 3d

- **No re-fitting strategy params, no re-clustering.** All upstream
  decisions frozen; this phase tests integration only.
- **Single-shot end-to-end VAL evaluation.** Don't iterate on
  orchestrator parameters (40% cap, 0.70 threshold) based on what
  VAL shows.

### Decision gate (3d → 3e or simplify)

| Outcome | Next |
|---|---|
| End-to-end VAL: median CPCV Sharpe > 0.5 AND ≥ 6 of 8 gates PASS AND PBO < 0.5 | Proceed to Phase 3e holdout |
| Borderline (4-5 gates, Sharpe 0.0-0.5) | Pause. Investigate descriptively (no tuning). Decide: simplify orchestrator (disable secondary allocations) and re-run, or retire. |
| FAIL (< 4 gates, Sharpe < 0) | Retire orchestrated approach. Best individual strategy from 3a → Phase 3e instead. |

## 6.8 Phase 3e — Final holdout test (single shot)

**Duration:** 1 day
**Goal:** apply chosen variant to one common holdout window
(reviewer correction: ONE holdout, not 6).

### Pre-requisite: lock variant before run

One of:
- **Variant A:** Full orchestrator + 4 (or 5) strategies (if 3d passed)
- **Variant B:** Best single strategy from 3a (if 3d failed)
- **Variant C:** Top 2-3 strategies from 3a in equal-weight static
  portfolio (no regime conditioning)

### Holdout window

Reviewer correction: extend to 90 days minimum for statistical power.

**Recommended holdout:** Aug 2025 → Feb 2026 (~120 days, end of GDFL
corpus). Reserves the post-Q2-2025 regime continuation as the test
set. **Single-shot, no peeking.**

### Decision gate (3e → deploy or retire)

Reviewer correction: ambiguous → 4 weeks paper, not retire.

| Outcome | Next |
|---|---|
| Holdout PASS or near-PASS (≥ 6 gates) AND median Sharpe > 0.5 | Phase 4 (paper-trade 30 days, then 1-lot live) |
| Holdout FAIL (< 4 gates) OR median Sharpe < 0 | **Retire variant.** No further attempts on this strategy family. |
| Ambiguous (4-5 gates, Sharpe 0.0-0.5) | **4 weeks of paper trade, no deployment, no deletion.** Then re-evaluate. |

## 6.9 Phase 4 — Forward live (post-deployment)

**Duration:** 30 days paper + 60 days cautious live at 1 lot
**Goal:** verify holdout result generalises forward.

### Why this matters

No historical validation proves forward stability. Indian options
markets evolved through 2024-2025 (weekly rule, fee changes); they
will evolve further. Forward testing is the only confirmation.

### Daily ritual

- Pre-market: `scripts/verify_system.py`
- Market hours: live trading at 1 lot
- Evening: `scripts/nightly_audit.py` + per-regime P&L breakdown
  using Phase 3b's classifier
- Weekly: per-strategy + per-regime attribution report
- **Pause condition:** 5 consecutive losing days OR weekly drawdown
  > 3× backtest expectation. Investigate before resuming.

---

# VII. Cross-cutting discipline rules

These apply to every phase from 3-Pre through 4. They are the
preventives for the same class of bug the audit cycle exposed.

1. **Never tune parameters by looking at validation set output.**
   Tune on train, select on val, report on val ONCE.
2. **Holdouts are single-access.** Per-strategy holdouts (if used)
   AND the orchestrator holdout each get exactly one read. The
   harness's `SplitLoader` enforces via lock file; don't bypass.
3. **No outcome features in regime labeling.** Outcomes only enter
   AFTER labels are derived from at-decision features.
4. **PBO computed for any multi-config search.** PBO > 0.5
   (canonical, reviewer correction) → reject.
5. **Coarse grids over fine grids.** 4 values per param, not 9.
6. **One change per validation run.** Multiple changes validated
   together; if it fails, ablate one at a time.
7. **No re-running holdout to "see if a tweak helps."** Single read
   per variant. Tweaks require new holdout window or accepting
   verdict.
8. **MAE / MFE calibration is descriptive, not prescriptive.**
   Informs grids in NEXT phase, never retroactively changes current.
9. **Independent auditor pass before each phase declares done.**
   Pattern from `validation_audit_apr25.md` repeats: fresh agent,
   no context, reads proposed phase output and looks for bugs.
10. **Charge constants must match authoritative sources.** Updated
    only in Phase 3-Pre per §5.4.1.

---

# VIII. Decisions required before starting

These are the operator commitments. **No work in §V or §VI starts
until each is committed in writing in `memory/phase3_decisions.md`
(new file, created at commitment time).**

## 8.1 Phase 3-Pre (must commit before scripts are built)

1. **Source for STT verification.** Do you have authoritative source
   links for FY2024-25 STT rate on options sell premium? If yes,
   share; otherwise we verify against the budget memorandum PDF
   directly. (Memory says 0.125%, reviewer says 0.1% — 25%
   magnitude difference matters for cost calculus.)
2. **Primary broker confirmation.** Is the project's primary broker
   still Zerodha? Brokerage rates feed the calculation.
3. **Stamp duty state.** Maharashtra (0.003%) or different?
4. **PASS threshold.** 1-lot net Sharpe > 0.5 (locked)? Or relax to
   > 0.3 (more permissive)? Or tighten to > 1.0 (more
   conservative)?
5. **Replan-options menu.** If FAIL, which of the three options
   would you prefer to investigate first?
   - (a) Institutional size
   - (b) Different strategies (Iron Butterfly, Long Calendar,
     NIFTY/BANKNIFTY pair)
   - (c) Retire systematic premium-selling at retail scale

## 8.2 Phase 3 (must commit before Phase 3a starts; only relevant if 3-Pre PASSes)

1. **Strategy roster.** 4 strategies (Strangle, IC, Iron Butterfly,
   Long Calendar) confirmed? Trend Debit Spread re-included only if
   Bug-3-clean revalidation on a single fresh window shows positive
   Sharpe?
2. **Per-strategy capital cap.** 40% (locked) or different? (30% =
   more diversification, 50% = more concentration.)
3. **Regime confidence threshold for cash.** 0.70 (locked) or
   different? (0.60 = more aggressive trading, 0.80 = more
   conservative.)
4. **Volatility-targeting weights vs Markowitz.** Reviewer-
   recommended vol-targeting (default), or test both in 3d?
5. **Holdout strategy.** ONE common 90-120 day holdout
   (Aug 2025 → Feb 2026, reviewer-corrected) instead of 6
   per-strategy holdouts?
6. **Compute platform.** Cloud (c7i.4xlarge spot, ~$30 budget) as
   default, or M4 only?
7. **PBO threshold.** 0.5 binding (canonical) or 0.4 (current plan)?
8. **3e holdout PASS bar.** ≥ 6 gates + median Sharpe 0.5 (locked)
   or relax to "PASS or near-PASS" judgment call?
9. **3e ambiguous handling.** 4 weeks paper trade (reviewer
   correction) or retire-as-fail or deploy-as-pass?

## 8.3 Phase 4 (must commit before forward live; only relevant if 3e PASSes)

1. **Capital allocation.** 1 lot static for all 90 days, or ramp
   1 → 5 based on weekly performance gate?
2. **Pause-and-investigate triggers.** 5 consecutive losing days
   (locked) and weekly DD > 3× expectation (locked) — additional
   triggers?

## 8.4 Off-ramp commitment (cross-cutting)

The reviewer flagged that off-ramps after multi-week investment are
psychologically hard. **Pre-commit in writing to:**

1. If Phase 3-Pre FAILs, no more parameter optimization on the
   current strategy family for 30 days. Use the time to investigate
   one of the §5.5.3 replan options.
2. If 3 of 5 strategies fail their 3a gate, the entire premium-
   selling architecture is paused and reassessed for 30 days.
3. If Phase 3e holdout fails, the variant is retired. No "fix one
   parameter and retry."
4. If Phase 4 forward live fails (5 consecutive losing days OR
   weekly DD > 3× expectation), live is halted; root-cause
   investigation before any new variant.

These commitments go in `memory/phase3_decisions.md` with a
timestamp. They are not negotiable mid-phase.

---

# IX. Resources, timeline, off-ramps

## 9.1 Timeline summary

| Phase | Duration | Compute | Holdouts touched |
|---|---|---|---|
| **3-Pre fee truth-up** | 3 days | None (analysis on existing data) | None |
| 3a per-strategy optimization | 2-3 weeks (cloud) or 4-6 weeks (M4) | ~700 worker-hours sequential | None |
| 3b regime discovery | 2-3 weeks (parallel w/ 3a) | None (research) | None |
| 3c strategy-regime mapping | 1-2 weeks | None (uses 3a results) | None |
| 3d orchestrator | 2-3 weeks | ~5 CPCV runs (~25 worker-hours) | None |
| 3e final holdout | 1 day | 1× CPCV on 90-120d holdout (~5h) | **Common holdout (single shot)** |
| 4 forward live | 90 days | None (live trading) | n/a |

**Total elapsed:**
- Best case (cloud, all gates PASS): 3 days + ~10 weeks + 90 days =
  ~15 weeks
- Worst case (M4, retire after 3a): 3 days + 6 weeks = ~6.5 weeks
- Most likely (cloud, retire after 3-Pre or 3a): 3 days OR 3 days +
  ~6 weeks

## 9.2 Compute budget

- **Phase 3-Pre:** $0 (analysis only)
- **Phase 3a (cloud):** ~$30 (c7i.4xlarge spot, 100 hours)
- **Phase 3a (M4 fallback):** $0 but ~5 weeks of laptop time
- **Phase 3b:** $0 (analysis only)
- **Phase 3c:** $0 (uses 3a results)
- **Phase 3d:** ~$5-10 cloud or 6-12 hours M4
- **Phase 3e:** ~$5 cloud or 5-6 hours M4
- **Phase 4:** Capital at risk = 1 lot NIFTY ≈ ~₹15K margin per
  strategy active, ~₹50K-₹1L total margin depending on strategy mix

## 9.3 Off-ramps consolidated

Each phase has explicit retire/pause triggers. Aggregated:

- **3-Pre FAIL** → 30-day moratorium on parameter work; investigate
  replan option (a/b/c)
- **3a:** any single strategy fails its gate → that strategy out
- **3a:** ≥ 3 of 5 strategies fail → entire premium-selling
  architecture paused
- **3b:** clusters don't generalise → no regime conditioning
- **3c:** no significant per-(strategy, regime) edge → no regime
  conditioning
- **3d:** end-to-end VAL fails → fall back to best single strategy
  from 3a
- **3e:** holdout fails → retire variant; no further attempts
- **3e:** ambiguous → 4 weeks paper trade
- **4:** forward live fails → halt live; root cause before resuming

---

# X. Files modified this audit cycle

For traceability and rollback if needed.

## 10.1 Source code changes (validation harness fixes)

- `src/backtest/validation/regime.py` — Bug 1 fix (stratifier ENTER+EXIT pairing)
- `src/backtest/engine.py` — Bug 2 fix (explicit `days` parameter); Bug 3 invocation (calls `aggregator.clear_day()` at boundary)
- `src/market_data/aggregator.py` — Bug 3 fix (`clear_day()` method)
- `src/strategy/decision_logger.py` — Bug 4 hardening (`FNO_DISABLE_DECISIONS` env var)
- `src/strategy/implementations/portfolio_scoring.py` — Latent Bug A fix (IV Rank `as_of_date`)
- `src/strategy/implementations/portfolio_strategy.py` — calls IV Rank with `as_of_date`
- `src/strategy/params.py` — `premium_blocked_regimes` default = [] after P1.5 outcome
- `src/strategy/base.py` — P1.5 regime-gate infrastructure retained but disabled
- `src/backtest/validation/cpcv.py` — Accepts `runner_spec` + `n_workers`
- `src/backtest/validation/parallel_runner.py` — NEW (parallel CPCV runner)
- `scripts/validate_strategy.py` — `--workers` CLI; wipes decisions before stratifier; explicit `days` to engine

## 10.2 Tests added

- `tests/unit/test_engine_day_selection.py` — 9 tests (Bug 2)
- `tests/unit/test_aggregator_day_reset.py` — 4 tests (Bug 3)
- `tests/unit/test_decision_logger_disable.py` — 13 tests (Bug 4 parallel hardening)
- `tests/unit/test_regime_runtime_parity.py` — 14 tests (Bug 1 / P1.5 contract)
- `tests/backtest/validation/test_regime.py` — 3 new tests (Bug 1 stratifier pairing)
- `tests/integration/test_parallel_cpcv_determinism.py` — 1 integration test (parallelization contract)

**Total: 44 unit + 1 integration = 45 new tests.** All passing.

## 10.3 Diagnostic scripts retained

- `scripts/diagnose_stratifier_mismatch.py` — Bug 1 evidence
- `scripts/diagnose_trend_leg.py` — per-leg attribution by regime
- `scripts/diagnose_cross_day_candles.py` — Bug 3 evidence
- `scripts/diagnose_cpcv_tail.py` — 82-day tail diagnosis
- `scripts/diagnose_2025_regime_shift.py` — 200-day regime-shift descriptive analysis
- `scripts/time_p15_gate.py` — runtime gate overhead measurement

## 10.4 Validation reports preserved

- `reports/validation/short_baseline_portfolio_pre_p15.md` — pre-fix
  baseline (UNTRUSTED, 6-path CPCV)
- `reports/validation/short_baseline_portfolio_post_p15.md` — pre-fix
  with P1.5 gate (UNTRUSTED)
- `reports/validation/short_baseline_portfolio_fair_gate_off.md` —
  pre-fix gate-off (UNTRUSTED — Bugs 1+2+4)
- `reports/validation/wide_baseline_portfolio.md` — **post-all-fixes,
  parallel — first trustworthy result.** This is the baseline.

## 10.5 Memory entries

- `memory/MEMORY.md` — index updated
- `memory/validation_audit_apr25.md` — full audit (4 bugs + parallel)
- `memory/p15_regime_gate.md` — earlier P1.5 net-negative outcome,
  retained infra
- `memory/phase3_decisions.md` — NEW, will be created at operator
  commitment time per §VIII

---

# XI. References & methodology

## 11.1 Project-internal

- `memory/validation_audit_apr25.md` — 4 bugs + parallelization
- `memory/p15_regime_gate.md` — earlier net-negative regime-gate
  result
- `reports/validation/wide_baseline_portfolio.md` — current baseline
- `docs/ROADMAP_TOP1PCT.md` — broader project roadmap
- `docs/DATA_RELIABILITY_PLAN.md` — data infra
- `docs/SYSTEM_ANALYSIS_2026-04-17.md` — system-wide audit

## 11.2 Methodological references

- López de Prado, *Advances in Financial Machine Learning* — CPCV
  (Combinatorial Purged Cross-Validation), PBO (Probability of
  Backtest Overfitting), DSR (Deflated Sharpe Ratio) formulas
- Lo, *Adaptive Markets Hypothesis* — regime-conditioned strategy
  rationale
- Israelov & Nielsen, "Covered Calls Uncovered" — vol-risk-premium
  decomposition
- Goyal & Saretto, "Cross-Section of Option Returns and Volatility"
  — regime-dependence of strangle/straddle returns
- Markowitz / Black-Litterman — original mean-variance allocation
  framework (NOT used in final design; vol-targeting preferred per
  reviewer)
- Asness et al., "Volatility Targeting" — robust allocation under
  estimation uncertainty (used in orchestrator)

## 11.3 Architecture references

- Multi-strategy CTA literature on regime-conditioned allocation
  (AHL, Aspect, Winton public materials)
- Mixture-of-experts ML literature (Shazeer et al. 2017; gating +
  expert pattern)
- HMM regime-switching: Hamilton (1989), Ang & Bekaert (2002)
- Indian-market-specific: SEBI circulars on Nov 2024 weekly expiry
  rule, Oct 2024 STT amendment in FY2024-25 budget memorandum

---

# XII. Status & next action

> **Apr 26 2026 update:** Phase 3-Pre executed overnight (Apr 25-26).
> See `reports/phase3_pre/MORNING_BRIEFING.md` for the briefing.
> See `memory/phase3_pre_truthup_apr25.md` for the verdict + history.

## Phase 3-Pre verdict (Apr 26 2026)

**Aggregate: FAIL.** Wide-baseline re-cost (211 trades, 200 days, post
Apr 25 charge constants fix) shows net Sharpe -0.18, charges 794% of
gross aggregate, net -₹3,568 over 200 days at 1 lot.

**Per-mode:**
- **Iron Condor: PASS** (+1.06 Sharpe, 27 trades, +₹7,207 net)
- Strangle: FAIL (-0.16 Sharpe, charges absorb thin gross)
- Trend Debit Spread: FAIL (-1.16 Sharpe, structurally losing)

**Phase 3a as written (5-strategy parallel optimization) does NOT
start.** The 30-day moratorium on parameter optimization begins per
§V.5.3 / §11 off-ramp commitments.

## Three options for the operator (decision needed)

**Option A — Reduced-roster Phase 3a:** drop Strangle, Short Straddle,
Iron Butterfly, Trend Debit Spread. Run Phase 3a with **Iron Condor +
Long Calendar + NIFTY/BANKNIFTY relative-vol pair** (3 tracks). Most
of the §VI architecture carries forward; the strategy roster in §VI.2
is the only thing that changes.

**Option B — Pivot to event-window concentration:** abandon the
daily-grind model. Use existing infrastructure for event-only premium
selling (~30-40 days/year: RBI MPC, US Fed, Budget, post-gap reversion)
on top of a NIFTYBEES + monthly-covered-call base. Reviewer's Tier-1
recommendation. ~14-20% annualized expected, conservative ops.

**Option C — Retire systematic premium-selling at retail scale.** The
30-day moratorium is the natural pause to investigate a different
concept entirely. Infrastructure is preserved for the next strategy.

## Charge constants updated (Apr 25 2026)

`src/core/constants.py::CHARGES` was found to have stale rates from
pre-Oct-2024 era. Now reflects current rates (verified against NSE
circular FA64232, Union Budget 2024 + 2026, Zerodha varsity):

- STT options sell: 0.0625% → **0.10%** (Oct 2024) + **0.15%**
  (`*_apr2026` field, Apr 2026)
- Exchange (options): 0.05% → **0.0353%** (NSE circular 100/2024)
- Exchange (futures): 0.002% → 0.00173%
- Date-aware lookup logic in truth-up scripts

This means **every validation report run before Apr 25 2026
silently understated trading costs.**

## What was NOT done overnight (operator decision required first)

- Did not start Phase 3a (depends on Option A/B/C choice)
- Did not modify PHASE3_MASTER strategy roster (§VI.2 still shows
  the 5-strategy plan; needs revision once option is picked)
- Did not touch the holdout window (Aug 2025 - Feb 2026 still
  reserved)
- Did not address the system-hygiene Bug 5 candidate (decisions/
  dir mixing live + backtest writes — fix is straightforward but
  not blocking)

---

*End of master plan v1. Phase 3-Pre executed Apr 25-26 2026; awaiting
operator decision on Option A / B / C.*
