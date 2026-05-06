# Long Calendar v2 — Smoke Findings (May 6 2026)

**Status:** Gate fires (17 trades / 173 days), but strategy is unprofitable on train+val. NOT empirically dead — fundamentally different verdict from the "0 fires" provisional finding earlier in this commit's history (see CORRECTION below).

**Hypothesis tested:** Long Calendar with the orthogonal-traditions gate `CI ≥ 61.8 AND VRP < 0` would fire as the natural complement to IC v2 (`CI ≥ 61.8 AND VRP > 0`) and produce a regime-diversified F&O book.

**Verdict:** ⚠️ Gate is alive but strategy underperforms in train+val. Sparse fire rate (~0.1 trades/day vs IC v2's 2.3/day) and negative train+val PnL.

## CORRECTION (May 6, 22:58 IST)

An earlier draft of this doc and the parent commit `1b97901` claimed "0 LC v2 fires" based on counting `(OK, OK_LV)` skip-log entries. **That was a methodology error**: when the gate PASSES, the strategy proceeds to entry — there is no "skip" log, by design. Counting `(OK, OK_LV)` in the skip logs is mechanically guaranteed to be 0, regardless of whether the gate is alive.

The real measurement is the count of `[ENTRY]` events in the backtest log, which is **17** across the 173-day post-SEBI corpus.

## TL;DR (final 173-day numbers)

| Metric | Value |
|---|---|
| Days backtested | 173 (post-SEBI: 2024-11-21 → 2025-07-31) |
| Total entries (round-trips) | **17** |
| Per-trade exits booked | 17 (every entry exited cleanly) |
| Win rate | **47.1%** (8W / 9L) |
| Best winner | +₹652 |
| Worst loser | -₹1,481 |
| Sum of exit P&L (gross) | **-₹2,565** |
| Net P&L after charges | **-₹1,707** |
| Sharpe (per-trade) | **-0.95** |
| Max DD | -₹2,122 |
| Trade size (qty) | 75 (1 lot NIFTY) |
| Average net debit per trade | ₹200-320 (varies by strikes) |

## What this means

LC v2 fires sparsely (0.1 entries/day vs IC v2's 2.3/day). The bottleneck is the joint condition CI ≥ 61.8 AND VRP < 0 — this is the relatively rare "range-bound + IV-cheap" state. It exists but is uncommon on Indian post-SEBI data.

Win rate 47.1% with asymmetric loss profile (worst -₹1,481 vs best +₹652) means the structural bias of LC under this gate is to **lose more on losers than win on winners**. Over 17 trades, this aggregates to net loss.

## Why the gate fires but doesn't profit

Three plausible mechanisms (none of these have been verified individually yet — flagged for follow-up):

1. **Cost basis is high relative to vega edge.** Each entry crosses 4 spreads (front+back × buy+sell) on options that are already at the cost wall. ~₹200-320 net debit on 75-lot = ₹15K-24K of capital. Even modest unfavorable IV moves wipe out the small theta gain.

2. **The "range" condition isn't holding for the calendar's lifetime.** Even when CI ≥ 61.8 at entry, spot can drift away from the strike in the days following — calendar's max profit zone is narrow (typically ±1% from strike on the front-leg expiry). Trending after entry → losses.

3. **VRP < 0 doesn't always mean "vol expansion incoming".** The interpretation "IV is cheap, expansion likely" assumes mean-reversion of VRP. On Indian post-SEBI data, VRP can stay negative for extended periods without a reversion event. So buying vega at VRP < 0 doesn't guarantee a vol-expansion payoff.

## Quadrant counts (provisional from skip-log analysis, ~13K decisions)

The skip-log analysis is still useful as a **regime time-share** measurement (how often each gate condition fails):

| Quadrant | % of skip-log decisions | Meaning |
|---|---|---|
| range + IV-rich  (IC v2 territory)   | ~2.4% | IC v2 fires here |
| trend + IV-cheap (LC v2 partial)     | ~13%  | LC would fire if no CI requirement |
| trend + IV-rich  (neither)            | ~82%  | dominant regime |
| range + IV-cheap (LC v2 territory)   | sparse | implicit (entries happen, no skip log) |

Skip-log analysis confirmed: among 310 skipped-but-CI-OK cases (range-bound moments where the entry was blocked by VRP), VRP was ALWAYS positive — confirming range moments tend to also have rich IV, **but the entries that did fire show this isn't an absolute rule.**

## Path forward

### Option A — Formal validation of LC v2 as committed

Run the full WF + cost-sensitivity + holdout pipeline despite negative train+val. The 17-trade sample is thin; PF and Sharpe estimates have wide error bars. Worth 30-60 minutes of compute to know:
- Does WF coverage hold above 0.55?
- What does cost-sensitivity look like? PF at +0.5 cost shift?
- Is there a cleaner subset of the 17 trades that filters profitably?

### Option B — LC v2b: pure VRP < 0 gate

Drop the CI requirement. Fires on the ~13% of corpus moments with VRP < 0, regardless of regime. Larger sample (probably 50-100 trades on 173 days). The risk: trending markets drag spot off-strike, hits the calendar's max_underlying_move_pct stop.

### Option C — Pivot to NIFTY Futures Trend

The original PIVOT_DESIGN_trend_futures.md path. Different cost structure (no SEBI options-sell hike), simpler signal mechanic, probably 3-4 weeks of work including data ingestion.

### Option D — Long Straddle on VRP < 0

Replace the calendar structure with a long straddle (buy ATM CE + buy ATM PE). Profits from EITHER vol expansion OR absolute spot movement — so trending markets don't kill the trade. Same gate (VRP < 0). Different structure, same cost wall concern.

## Files

- `reports/standalone_post_sebi/lc_v2_research_params.json` — params override
- `scripts/smoke_lc_v2.py` — smoke harness (final form: 173 days)
- `scripts/diagnose_lc_v2_gate.py` — quadrant histogram from skip logs
- (this file) — corrected findings synthesis

## Methodology lesson

**Counting skip-log quadrants is NOT a measurement of gate fire rate.** The skip log is mechanically silent on the (OK, OK_LV) quadrant because that's where entries happen. To measure gate fire rate, count `[ENTRY]` events in the backtest output, not skip-log lines.

This bug led to the earlier draft of this doc claiming the gate was empirically dead. It's a one-line fix in any future v3 / v2-variant analysis: count `[ENTRY]` lines as the truth.

## Final word

LC v2 **gate is alive** but the strategy as configured is **not profitable** on the 173-day train+val window. This is a much more nuanced verdict than the "empirically dead" claim in the parent commit. Whether LC v2 (or LC v2b/v2c) can be made tradeable requires deeper analysis: trade-level audit of the 17 entries, cost-sensitivity, and possibly structural redesign (long straddle, futures trend).

The IC v2 + LC v2 portfolio thesis is partially validated — they DO fire on different days (mutually exclusive on the vol axis as designed) — but the LC v2 leg currently subtracts from the combined book rather than diversifying it positively.
