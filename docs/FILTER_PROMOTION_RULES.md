# Filter & Parameter Promotion Rules

**Owner:** Kundan Bora
**Created:** 2026-04-18
**Sibling doc:** `PROMOTION_CRITERIA.md` (strategy-level: backtest → shadow → live capital). This doc is narrower: when to flip a single feature flag (`portfolio_filters_enabled`, `pcr_filter_enabled`, `advisor_confluence_enabled`, etc.) from default-OFF to default-ON based on A/B evidence.

**Why this exists:** The Apr 18 chain-replay A/B compared portfolio with `portfolio_filters_enabled=False` (24 trades, −₹512) vs. `True` (24 trades, +₹35) over 16 days. The +₹547 delta looked encouraging but is statistically inside the noise band (σ ≈ ₹1,000/trade × √24 ≈ ±₹400 CI on the mean). Without a written rule, the temptation is to flip the flag because "the number went up." This doc is the rule, written *before* you re-run the test, so post-hoc rationalization is harder.

---

## The promotion gate (must pass ALL)

A new filter / parameter / advisor-tuning change moves from default-OFF to default-ON only if **every** condition below holds. Document each in the commit message that flips the default.

### 1. Sample size
- **Backtest A/B:** ≥ 60 trading days OR ≥ 200 evaluation events (whichever the change touches more often). 16 days is exploration, not a decision.
- **Shadow A/B:** ≥ 30 live trading days. Backtest passes ≠ shadow passes ≠ live passes.

### 2. Effect size vs. noise
- Compute the delta's **95% bootstrap confidence interval**, not just point estimate. (See "Bootstrap recipe" below.)
- The CI must **exclude zero** for the primary metric (usually net P&L per trade).
- Effect size ≥ **1.5x the bid-ask spread cost** the change avoids. A filter that "saves" ₹50/trade is worthless if the spread it crosses costs ₹40 — you'd lose the edge to round-trip frictions.

### 3. No tail-risk worsening
- **Max drawdown** in the ON arm ≤ OFF arm × 1.10. (Don't let a Sharpe improvement come from cherry-picking quiet days while the bad days got worse.)
- **Worst single day** in ON arm ≤ OFF arm × 1.10.
- **Number of stop-loss exits** in ON arm ≤ OFF arm × 1.20. (Filters should reduce or hold losing trades, not just shift their timing.)

### 4. Multiple-testing correction
- If you A/B-tested **N filters in the same window**, apply a **Bonferroni correction**: required p < 0.10 / N for any single filter to be promoted.
- Worked example: testing PCR + max-pain + advisor-confluence in one shadow window → each individual filter needs p < 0.033 to be promoted, not p < 0.10.
- The honest move when N > 1: stop, freeze the other tests, A/B one filter at a time in serial windows.

### 5. Behavioural sanity
- The ON arm's filter-block log line count is **non-trivial**: ≥ 5% of evaluation events fired the block. If 0% fired, the filter is dead code; if 100% fired, the filter killed the strategy. Either way, don't promote.
- The change is **explicable in one sentence**: "blocking entries when PCR > 1.5 avoids the high-IV event days." If you can't explain *why* the change helps, you've curve-fit.

### 6. Pre-registered metric
- The metric you're A/B-ing on (net P&L per trade, Sharpe, max DD, win rate) is **declared in the commit that introduces the flag**, not chosen after seeing the result.
- "I A/B'd net P&L, it didn't move, but Sharpe went up so I'm promoting on Sharpe" → fail. Run a fresh window with Sharpe as the pre-declared metric.

---

## Bootstrap recipe

For the +₹547 delta on 16 days, the right confidence statement is:

```python
import numpy as np
trade_pnl_off = np.array([...])  # 24 entries from off-arm
trade_pnl_on  = np.array([...])  # 24 entries from on-arm
n_boot = 10_000
deltas = []
for _ in range(n_boot):
    s_off = np.random.choice(trade_pnl_off, size=len(trade_pnl_off), replace=True)
    s_on  = np.random.choice(trade_pnl_on,  size=len(trade_pnl_on),  replace=True)
    deltas.append(s_on.mean() - s_off.mean())
ci_low, ci_high = np.percentile(deltas, [2.5, 97.5])
print(f"95% CI on per-trade delta: [{ci_low:+.0f}, {ci_high:+.0f}]")
```

If the printed CI is `[-380, +420]`, the +₹23/trade point estimate is noise. Don't promote.
If it's `[+45, +380]`, the lower bound excludes zero — promote candidate (still apply gates 3-6).

---

## Demotion is ALWAYS in scope

A flag that was promoted to default-ON last quarter must be **re-validated quarterly** with the same gate. Markets change. Fixed thresholds (PCR 0.7-1.5) calibrated on Q1 data may not hold in Q3. The annual review checklist:

- Re-run the original A/B on the trailing 60-day window.
- If gates 1-6 no longer hold, demote to default-OFF and open an investigation.
- Demote without debate if: 3 consecutive months of negative live contribution attributable to the filter (per attribution log).

---

## Filter-flag inventory (status as of 2026-04-18)

| Flag | Default | Status | Last A/B | Notes |
|------|---------|--------|----------|-------|
| `pcr_filter_enabled` (BaseStrategyParams) | True | Production for IC/strangle/straddle | Pre-Apr-18 | Was applied unevenly — portfolio strategy ignored it until Apr 18 |
| `max_pain_filter_enabled` (BaseStrategyParams) | True | Production for IC/strangle/straddle | Pre-Apr-18 | Same as above |
| `portfolio_filters_enabled` (PortfolioParams) | False | **Exploration** | 2026-04-18 (16d, n=24, +₹547 delta — inside noise band) | Re-A/B at n≥60 trades before promoting |
| `advisor_confluence_enabled` (Settings) | False | **Inert** — confidence gate (0.7) above live confidences (0.4-0.6) | Never (no adjustments fired) | Lower gate to 0.5 OR backfill day_bias before A/B |
| `shadow_only` (BaseStrategyParams) | False | Infrastructure (per-strategy override, not a global flag) | N/A — mechanism not metric | n/a |

When you flip a flag's default, **update the table**. If the table says "exploration" and the value is "True", you have a process violation.

---

## Worked example — what *would* let us flip `portfolio_filters_enabled` to default-ON

Concrete pre-registration for the next A/B:

- **Window:** 60 trading days starting first weekday after `data/chain_snapshots/` reaches that count (estimated mid-July 2026 if recorder runs daily).
- **Primary metric:** net P&L per portfolio premium-leg trade.
- **Pre-declared promotion threshold:** bootstrap 95% CI lower bound > +₹100/trade.
- **Tail-risk guard:** max drawdown in ON arm ≤ 1.10x OFF arm; worst single-day P&L in ON arm ≤ 1.10x OFF arm.
- **Block rate guard:** PCR or max-pain block fires on 5%-50% of evaluations (already 40% in Apr 18 sample, so likely satisfied).
- **Multiple-testing:** if AI confluence A/B is also running in the same window, require lower bound > +₹150/trade (Bonferroni for N=2).
- **Decision rule:** if all four pass, flip default to True in a one-flag commit citing this doc and the A/B run JSON. If any fail, document why in the commit that *doesn't* flip, then either re-run with longer window or close the experiment.

The point of this exercise: by the time the data arrives, the decision is mechanical. No "I think the trend is encouraging." Either the numbers cleared the bar or they didn't.
