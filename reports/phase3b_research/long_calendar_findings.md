# Long Calendar — Phase 3b Findings (Apr 27 2026)

**Branch:** `FnO-v4-strategy-research`
**Status:** Retired pending data corpus expansion
**Discipline:** §VII — three versions tested under one hypothesis each, no iterative tuning

## TL;DR

Long calendar with weekly back-leg empirically fails (mean Sharpe -6.8, costs eat 5× the gross edge). Long calendar with monthly back-leg cannot be validated on the current GDFL parquet corpus — the data only stores 2 weekly expiries per day, no monthly chains. Strategy concept is **archived** until monthly chain data is acquired.

## Three versions, three failure modes

### v1 — first cut (commit `23b8b9c`, validated Apr 27 12:36)

**Config:** weekly back (next available expiry), default profit/stop/move thresholds.
**Result:** Mean CPCV Sharpe **-14.5**, 0% win rate across all 4 active regimes, total losses ~₹2.5 M across the corpus.

**Diagnosis:** implementation bug. `reset_day_state` cleared `_entered = False` every morning, which made the strategy "forget" yesterday's open position. By Friday of a typical week, the strategy held 5 stacked calendar positions, all bleeding theta together. 92 trades / 30-day window confirmed compounding entries — calendar should have ~1 per week.

### v2 — bug fix (commit `eedd206`, validated Apr 27 13:36)

**Fix:** `reset_day_state` now only clears the per-day skip-log dedup. `_entered` and `_stopped_for_day` persist until `_check_expiry_rollover` fires (front expired → fresh week) or an explicit exit signal closes the position.

**Config:** same as v1 (weekly back).
**Result:** Mean CPCV Sharpe **-6.8**, 0% win rate across all active regimes, ~₹3.5 K total loss per backtest pass.

**Diagnosis (clean signal now that the stacking bug is gone):** the strategy *concept* with weekly back-leg is structurally bad in Indian options.
- Cost-shift at -1.0× = +0.40 Sharpe, +₹732 gross over the corpus → **edge before costs is essentially zero/noise**
- Cost-shift at +0.0× = -1.94 Sharpe, -₹3,528 → cost burden ~₹4,260/round-trip
- 4-leg traversals = 2 full bid-ask spreads per round trip
- Weekly-vs-weekly = ~7 day theta differential, far too small to overcome cost drag
- Front-week IV often ≤ back-week IV (contango), so vega works against the trade initially

### v3 — monthly back-leg hypothesis (commit `46e9da6`, validated Apr 27 17:13)

**Hypothesis:** longer time differential should let theta-decay overcome cost drag. Add `min_back_days: int = 21` param so `_find_back_expiry` picks the smallest expiry at least 21 calendar days after front (typically the next monthly).

**Result:** 0 trades across all 8 walk-forward windows. Strategy correctly returned `None` on every entry attempt because no back-expiry met the gap criterion.

**Diagnosis:** **GDFL parquet only stores 2 expiries per day** — current weekly + next weekly. Inspected `gdfl_nifty_2024-09-02.parquet`:
```
Available expiries: 2
  2024-09-05  (current weekly, +3 days)
  2024-09-12  (next weekly, +10 days)
```

No expiry meets `min_back_days >= 21`. **Monthly long_calendar cannot be validated on this corpus.**

## What the data corpus contains (and doesn't)

| Asset | Coverage | Granularity |
|---|---|---|
| NIFTY weekly chains | Sep 2024 – Feb 2026 | 1-min ticks, 2 weekly expiries per day |
| NIFTY monthly chains | **None** | — |
| BANKNIFTY chains | **None** | (only spot OHLCV, 12 days Apr 2026) |
| India VIX | Sep 2024 – Feb 2026 | 1-min |
| Other underlyings | None | — |

This data structure forecloses on:
- Long calendar with monthly back-leg
- NIFTY/BANKNIFTY relative-vol pair trade
- Inter-month calendar / diagonal spreads
- Term-structure arbitrage strategies

## Discipline note (§VII compliance)

v1 → v2 was a **bug fix**, not a tune (same hypothesis, fixed implementation).
v2 → v3 was a **new hypothesis** (longer time differential), not a tune of v2 thresholds.
v3 → no further test attempted on the same data corpus per §VII.7 (cannot iteratively tune `min_back_days` until it passes).

## Recommendations

| Priority | Action |
|---|---|
| **High** | Archive `long_calendar` from active roster. Code + tests stay in repo for future revisit. |
| **High** | Acquire monthly NIFTY chain data + BANKNIFTY chain data before any further calendar / pair-trade research. Without this, the testable strategy space is exhausted. |
| Medium | If single-index strategies are still in scope (no inter-strategy diversification needed yet), focus on enhancing regime detection for the existing iron_condor — that's the only strategy with *any* real edge per Phase 3a-revised. |
| Low | If validation infra needs to be more permissive about partial data, the validate_strategy harness could log "0 trades, skipping verdict" instead of FAIL when num_trades == 0 — but this is cosmetic. |

## What we learned methodologically

1. **Holdout slices are wasted on strategies that can't even enter.** v3's "0 trades" exposed that we'd burn a slice on a non-test. Future protocol: dry-run a strategy on a single training day BEFORE allocating a holdout slice, to confirm it can actually generate trades.

2. **Bug fixes vs hypothesis revisions need clear labeling.** v1 → v2 was a code correctness fix; v2 → v3 was a strategy-design hypothesis. Both feel like "iterations" but only the second is a §VII-bounded hypothesis. The pre-registration log should distinguish them.

3. **Data audit must precede strategy enumeration.** Spent ~6 hours implementing + validating two long_calendar variants before discovering the corpus only has weeklies. A 5-minute "what's actually in the data" check at the start of Phase 3b would have surfaced this immediately.

---

*Findings locked Apr 27 2026 17:30 IST. Future revisit only after monthly + BANKNIFTY chain data is acquired.*
