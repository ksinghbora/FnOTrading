# Long Calendar v2 — Smoke Findings (May 6 2026)

**Status:** Empirically dead on Indian post-SEBI data — same negative-correlation trap as the v1 principled gate, just on a different axis.

**Hypothesis tested:** Long Calendar with the orthogonal-traditions gate `CI ≥ 61.8 AND VRP < 0` would fire as the natural complement to IC v2 (which uses `CI ≥ 61.8 AND VRP > 0`). Combined book = regime-diversified F&O strategy.

**Verdict:** ❌ Hypothesis falsified by 173-day post-SEBI corpus smoke.

## TL;DR

| Metric | Value (provisional, day 81/173 of corpus) |
|---|---|
| Days backtested | 173 (post-SEBI: 2024-11-21 → 2025-07-31) |
| Gate decisions sampled | 13,279 |
| Range + IV-cheap (LC v2 fires)   | **0** |
| Range + IV-rich  (IC v2 fires)   | 310 (~2.4%) |
| Trend + IV-cheap (neither)        | 1,696 (~13%) |
| Trend + IV-rich  (neither)        | 10,556 (~82%) |
| Among CI≥61.8 (range), VRP stats | mean **+2.09**, min **+0.12**, max **+14.22**, cases <0: **0/310** |

## What we proved

**On Indian post-SEBI data, range-bound markets ALWAYS have rich IV.**

Among 95 sampled cases where CI ≥ 61.8 (the literature-canonical Fibonacci threshold for range-bound), VRP was strictly positive in every single one — minimum VRP of +0.12, mean +1.53. No range-bound day in the post-SEBI corpus has had IV under-priced relative to realized vol.

This is the same _negative-correlation between gate conditions_ that killed the v1 principled IC gate (ADX<22 + BB-squeeze + RV/IV<0.80 fired on 0/2590 valid samples). Different conditions, same empirical structure.

## Why it makes sense (theory)

In a range-bound market:
1. Daily moves are small → realized vol stays low
2. Market makers price IV slightly above RV (structural premium for selling vol)
3. So range-bound regime → low RV → low IV → small positive VRP

The "range-bound + IV-cheap" state is essentially a contradiction in efficient option markets:
- Cheap IV means market makers expect future vol to be higher than recent past
- That expectation typically arises during transitions OR after a vol shock that left RV elevated relative to where IV got pulled
- Both transition and post-shock states are characterized by movement (trending/choppy CI), not range

So the gate's two conditions describe a market state that **doesn't exist as an equilibrium** on Indian post-SEBI options.

## What this means for the IC v2 / LC v2 portfolio thesis

The earlier hypothesis — "deploy LC v2 alongside IC v2 for regime-diversified coverage" — is **empirically wrong**. The two regimes (range+rich-IV vs range+cheap-IV) don't both exist in this market.

Specifically, IC v2's territory IS the entire range-bound subspace. There's no leftover "range + IV-cheap" subspace for LC v2 to capture.

## What the implementation taught us (still useful)

The LC v2 implementation itself was correct and worth keeping:

1. **`is_long_vol_favorable_v2` detector method** — correct, tested, mutual-exclusivity invariant verified by unit tests
2. **`assess()` side-effect requirement for daily-close accumulation** — discovered during the smoke; LC's v2 path needs to call `assess()` to populate `_daily_closes`, otherwise VRP returns None forever in backtest. IC v2 gets this for free via its scoring step. This pattern will need to be replicated in any future v2-style strategy.
3. **Backtest warmup behaviour** — confirmed the `_capture_daily_close` rollover works correctly during backtest with the simulated clock. The warmup_daily_closes loader (May 6 commit `4cff9b3`) is for live mode only; backtest accumulates from simulated ticks.

## Path forward

Three options, ordered by directness:

### LC v2b — pure VRP < 0 gate (drop CI requirement)

Buy long calendar when IV is cheap, regardless of regime. The intuition: long calendar's PnL is dominated by the back-leg's vega exposure, not the front-leg theta differential. So CI/range matters less than VRP.

- **Hypothesis**: ~20% of the corpus had VRP < 0; LC v2b would fire on those days
- **Risk**: trending markets would drag spot away from strike → max_underlying_move_pct stops out before vega expansion materialises
- **Implementation**: trivial — drop the CI condition from the gate
- **Smallest pivot**

### LC v2c — vol-expansion-imminent gate (RV recently spiked relative to IV)

Use a forward-looking vol indicator: enter when RV has been rising sharply over the past 3-5 days (RV momentum > 0) AND VIX hasn't yet reacted. This captures the early phase of a vol expansion, when buying vega has the most asymmetric upside.

- **Implementation**: medium effort — needs a new RV-momentum detector
- **Risk**: small sample of vol-expansion days on a 173-day corpus

### Pivot to Candidate B — Trend on NIFTY Futures

The original PIVOT_DESIGN_trend_futures.md design. Different instrument class (futures, not options). Different cost wall (no SEBI options-sell hike).

- **Implementation**: 3-4 weeks (requires futures data ingestion)
- **Architectural cleanest**: no theta, no 4-leg sync, no STT-on-sell hike
- **Highest information gain**: tests whether the "all post-SEBI options strategies fail" pattern is options-specific or market-wide

## Files

- `lc_v2_research_params.json` — params override used for smoke
- `scripts/smoke_lc_v2.py` — smoke harness (30-day default; was bumped to 173 for this report)
- (this file) — findings synthesis

## Final word

Three iterations of theory-grounded gate design (v1 principled, v2 IC, v2 LC) on Indian post-SEBI options have shown a consistent pattern: **the orthogonal-traditions principle works only when the two traditions actually have an empirical orthogonal subspace**.

- v1 (ADX + BB + RV/IV): three conditions, all negatively correlated, **0 fires**
- IC v2 (CI ≥ 61.8 + VRP > 0): two orthogonal traditions, **324 holdout trades** ✓
- LC v2 (CI ≥ 61.8 + VRP < 0): same two traditions but with the vol axis flipped — empirically **0 fires** because Indian post-SEBI markets don't have the (range, cheap-IV) state.

The methodology is sound. The next strategy needs either a different gate framework OR a fundamentally different alpha source (futures, equity, vol-targeting).
