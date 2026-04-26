# Phase 3-Pre: Fee/Slippage Truth-Up

> **Author:** Apr 25 2026
> **Status:** Draft for operator commitment
> **Predecessor:** PHASE3_ADVANCEMENT.md (v2), independent reviewer feedback
> **Successor:** Phase 3a (proceeds only if this truth-up PASSes)

## 0. Why this exists

The independent reviewer of `PHASE3_ADVANCEMENT.md` flagged a P0 risk:

> "If BS-vs-real gap is >70% on OTM strangles at 1–5 lot retail size, you
> have no edge to optimize and the entire plan is redirected. Backtest
> the *current* portfolio strategy's P&L against actual recorded
> mid-and-spread prices for those 4 weeks. This is a 2–3 day analysis
> and could obviate the entire 36-worker-day Phase 3a."

Two market-structure changes in late 2024 may have destroyed the
retail premium-seller edge that the strategy was tuned for:

1. **Oct 1 2024:** STT on options sell-premium increased (rate to be
   verified against the FY2024-25 budget memorandum — reviewer
   estimates 0.1%, project memory states 0.125%; the truth-up will
   confirm).
2. **Nov 2024:** SEBI restricted weekly expiries to NIFTY only.
   BANKNIFTY/FINNIFTY moved to monthly-only, which changed the
   options term structure and concentrated retail flow on NIFTY
   weekly. Bid-ask quality on far-OTM NIFTY weekly strikes may have
   tightened OR widened depending on MM behavior.

The wide-window validation (`reports/validation/wide_baseline_portfolio.md`)
already shows the strategy fails on `cost_sensitivity` at +0.25 spread
shift — meaning we have no cushion for real STT, exchange charges,
GST, brokerage, and slippage. **Before committing 36 worker-days to
Phase 3a optimization, we verify whether the strategy has any
positive edge at retail scale (1-5 lots) under realistic costs.**

If the answer is no, Phase 3 is replanned. If the answer is yes,
Phase 3 proceeds with the reviewer's other corrections incorporated.

## 1. Data we have

19 days of recorded chain snapshots in `data/chain_snapshots/`:

```
chain_2026-03-25.csv  (first)
...
chain_2026-04-24.csv  (last)
```

Schema (per row, ~one row per (minute, strike, option_type)):

```
time, underlying, expiry, strike, option_type,
ltp, iv, delta, gamma, theta, vega,
oi, volume,
bid_price, ask_price
```

Critically: **bid_price and ask_price are recorded from the actual
NSE quote feed**, not modeled. This is the truth source for execution
cost analysis.

Plus we have:
- `data/gdfl_snapshots/` parquet covering Sep 2024 → Feb 2026 (the
  validation's data source, also includes bid/ask but possibly
  modeled).
- `data/decisions/decisions_2026-*.csv` decision logs from the wide
  validation runs.
- `src/portfolio/charges.py` — already implements all Indian charges.

## 2. Hypotheses tested

**H1 (cost magnitude):** Total round-trip costs (STT + exchange +
SEBI + GST + stamp duty + brokerage) on a typical NIFTY weekly
0.20Δ short strangle exceed 8% of gross premium collected. If true,
the strategy has effectively zero margin at retail size.

**H2 (slippage reality):** The GDFL parquet's bid/ask prices are
**materially tighter** than the chain_snapshot bid/ask prices for the
same (minute, strike). If true, the wide-baseline validation
*understates* execution cost.

**H3 (size scaling):** Net P&L per lot turns negative between 1 and
5 lots due to spread crossing (we are not the MM at retail size).
The capacity curve in `wide_baseline_portfolio.md` already shows
this directionally; truth-up confirms it on real chain data.

**H4 (post-Oct-2024 edge degradation):** The strategy's net edge per
trade in the 19-day chain window (Mar 25 → Apr 24 2026, **post-STT
change, post-weekly-rule**) is meaningfully lower than the BS or
GDFL-modeled edge would suggest.

## 3. Methodology

### 3.1 Verify exact charge rates (Day 1 morning)

Cross-reference these against authoritative sources:

| Charge | Project memory | Reviewer | Authoritative source |
|---|---|---|---|
| STT (options sell premium) | 0.125% | 0.1% | FY2024-25 Budget Memorandum, Sec [TBD] |
| STT (options buy premium) | 0% | 0% | Same |
| STT (options exercise on intrinsic) | 0.125% | 0.125% | NSE STT circular |
| Exchange transaction (F&O options) | 0.05% (constants.py) | 0.0353% | NSE F&O fee schedule current as of Apr 2026 |
| SEBI turnover | 0.0001% | 0.0001% | SEBI circular |
| GST | 18% on (brokerage + exchange + SEBI) | 18% | CBIC standard |
| Stamp duty (options buy) | 0.003% (constants.py) | 0.003% | State-specific; Maharashtra default |
| Brokerage (Zerodha) | Per-order cap in constants.py | Same | Zerodha pricing page |

If any rate in `src/core/constants.py::CHARGES` differs from the
authoritative source, **fix the constant** before running the
truth-up. This is the only code change permitted in Phase 3-pre.

### 3.2 GDFL vs chain_snapshot price reconciliation (Day 1)

Build `scripts/truthup_compare_price_sources.py`:

1. For each of the 19 chain_snapshot dates, load both the
   GDFL parquet day and the chain_snapshot day.
2. Join on `(timestamp, strike, option_type, expiry)`.
3. For each match, compute:
   - `bid_diff = chain_snap_bid - gdfl_bid`
   - `ask_diff = chain_snap_ask - gdfl_ask`
   - `spread_chain = chain_snap_ask - chain_snap_bid`
   - `spread_gdfl = gdfl_ask - gdfl_bid`
   - `spread_ratio = spread_chain / max(spread_gdfl, 0.05)`
4. Stratify by:
   - Strike distance from spot (ATM, ±100, ±200, ±400, ±600 pts)
   - Days-to-expiry (0, 1-3, 4-7)
   - Time-of-day (open / midday / close)

**Pass criterion for the data source:** median `spread_ratio` < 1.5
across all strata. If chain spreads are systematically 2× wider
than GDFL spreads, the validation has been understating slippage
and the entire history of validation reports needs reinterpretation.

### 3.3 Re-replay strategy entries on chain prices (Day 2)

Build `scripts/truthup_chain_replay.py`:

1. Load decisions CSVs from the wide validation run for the 19
   chain dates (Mar 25 → Apr 24 2026).
2. For each ENTER / EXIT decision, look up the actual chain_snapshot
   bid_price (for SELL fills) or ask_price (for BUY fills) at that
   minute and strike.
3. Compute fill price assuming **cross-spread** execution (we are
   the taker, not the MM). Conservative: SELL fills at bid, BUY at
   ask.
4. For each round-trip (ENTER + matching EXIT), compute:
   - Gross P&L at GDFL fill prices (what the wide validation reported)
   - Gross P&L at chain_snapshot fill prices (truth)
   - **Slippage gap** = GDFL gross P&L − chain gross P&L
5. Compute charges via `src/portfolio/charges.py` for each leg using
   actual fill prices (chain-truth). Sum per round-trip.
6. **Net P&L truth** = chain gross P&L − total charges.

### 3.4 Per-strategy net edge analysis (Day 2)

For each strategy that fired in the 19-day window (premium leg
strangle, premium leg IC, trend leg), compute:

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

For lots > 1, the chain bid/ask walks are simulated by walking the
order book — the chain snapshot only records top-of-book, but real
fills at 5 lots cross multiple levels. **Conservative assumption:**
each additional lot crosses 0.05 rupees deeper into the book. This is
likely an underestimate of real depth-walking cost; if even this
conservative model fails, the strategy is unviable at multi-lot scale.

### 3.5 Cost decomposition (Day 2)

Per-trade-leg breakdown stacked-bar chart:

```
gross_premium = 100% (reference)
                ┌────────────┐
                │   STT      │ ~?% (sell side)
                │ Exchange   │ ~0.035%
                │ SEBI       │ 0.0001%
                │ GST        │ ~?%
                │ Stamp      │ 0.003% (buy side only)
                │ Brokerage  │ ~?%
                │ Slippage   │ ~?% (chain truth - midpoint)
                └────────────┘
                Net edge = remaining
```

This decomposition tells us where the cost concentrates — useful for
the replan if we FAIL.

## 4. Decision criteria (locked before Day 3)

### 4.1 PASS — proceed to Phase 3 with reviewer corrections

All of:
- 1-lot net Sharpe over 19 days > **0.5**
- 5-lot net Sharpe > **0**
- Median net P&L per trade > **+₹50** (positive after all costs)
- No single charge component > 50% of gross edge (no single-cost
  death)

### 4.2 YELLOW — viable only at minimum size

Mixed: 1-lot net Sharpe > 0.5 BUT 5-lot net Sharpe < 0. **Decision:**
proceed with Phase 3 BUT lock max_lots = 1 in all subsequent
optimization. Capacity is the binding constraint; orchestrator must
respect it.

### 4.3 FAIL — replan required

Any of:
- 1-lot net Sharpe < 0
- Median net P&L per trade < 0
- Slippage gap > 70% of gross premium
- A single charge component > 50% of gross edge

**Replan options (no commitment, just signal):**
- (a) Larger size where we receive spread instead of paying it
  (institutional 50+ lots; retire current operator scope).
- (b) Different strategies (Iron Butterfly ATM theta, NIFTY/BANKNIFTY
  vol-arb, Long Calendar) that don't depend on OTM premium-selling
  edge.
- (c) Retire systematic premium-selling at retail scale; pivot to a
  different research thread.

## 5. Discipline rules (same as Phase 3, applied to truth-up)

1. **No tuning during truth-up.** This is a measurement, not an
   optimization. Don't change strategy params based on truth-up
   output.
2. **No cherry-picking dates.** If a particular day has anomalous
   data (e.g., chain_snapshot recording broken), document it and
   exclude transparently — don't filter post-hoc to make numbers
   look better.
3. **Holdouts not touched.** The 19-day chain window is post the
   200-day train+val (Sep 2024 → Jun 2025). It's a *natural*
   forward-time sample, not a held-out window the harness controls.
   It's appropriate to use here because we're measuring cost
   structure, not strategy P&L generalisation. But: if Phase 3
   proceeds, the orchestrator's TRUE holdout is still
   Aug 2025 → Feb 2026 (per reviewer's recommendation to expand
   the data window).
4. **Charge constants must match authoritative sources.** The only
   code change permitted in Phase 3-pre is fixing
   `src/core/constants.py::CHARGES` if a verified source disagrees.
   Document the source in the constants file's docstring.
5. **No iterating on H4 cutoffs.** "70% slippage gap" and "50% single-
   cost-component" thresholds in §4 are locked. If the result is
   borderline, accept the borderline — don't relax thresholds.

## 6. Deliverables

### Scripts

- `scripts/truthup_compare_price_sources.py` — GDFL vs chain_snapshot
  bid/ask reconciliation (§3.2)
- `scripts/truthup_chain_replay.py` — strategy entries replayed on
  chain prices (§3.3, §3.4)
- `scripts/truthup_cost_decompose.py` — per-leg cost breakdown (§3.5)

### Report

- `reports/phase3_pre/fee_truthup_v1.md` — final verdict report
  containing:
  - §A. Charge rate verification table (memory vs reviewer vs source)
  - §B. GDFL vs chain spread reconciliation by stratum
  - §C. Per-strategy net P&L at 1 / 5 / 10 lots
  - §D. Cost decomposition stacked breakdown
  - §E. Verdict: PASS / YELLOW / FAIL with locked criteria
  - §F. If PASS: revised cost expectations to feed into Phase 3a
  - §F. If YELLOW: revised max_lots constraint
  - §F. If FAIL: which of the three replan options is supported by
    the cost structure

### Memory entry

- `memory/phase3_pre_truthup_apr25.md` — short summary + verdict +
  link to the report. Indexed in `memory/MEMORY.md`.

## 7. Timeline

| Day | Work |
|---|---|
| 1 (morning) | Verify charge rates against authoritative sources; fix `constants.py` if needed |
| 1 (afternoon) | Build & run `truthup_compare_price_sources.py`; spread reconciliation report |
| 2 (morning) | Build `truthup_chain_replay.py`; replay 19 days of decisions |
| 2 (afternoon) | Build `truthup_cost_decompose.py`; per-strategy net edge tables |
| 3 (morning) | Write `fee_truthup_v1.md` with verdict |
| 3 (afternoon) | Operator review + decision: PASS / YELLOW / FAIL |

## 8. Off-ramps

If during the truth-up we discover:

- **Charge rates in `constants.py` are wrong by > 10%:** stop, fix
  the constants, re-run all prior validation reports. The audit
  cycle effectively continues.
- **Chain snapshot data is corrupt or systematically incomplete:**
  the truth-up is moot until data quality is fixed. Pause Phase 3.
- **GDFL parquet itself doesn't have bid/ask (only ltp):** stronger
  conclusion — the entire validation harness has been guessing
  spreads. This becomes Bug 5 of the audit cycle. Pause Phase 3
  until fill model is rebuilt against chain_snapshot data.

## 9. What this does NOT do

- Does NOT change strategy parameters
- Does NOT add new strategies (those are Phase 3a if we PASS)
- Does NOT touch the regime detector (that's Phase 3b)
- Does NOT touch the orchestrator (that's Phase 3d)
- Does NOT use the holdout window (Aug 2025 → Feb 2026)
- Does NOT include 0DTE or BANKNIFTY pairs analysis (Phase 3+ if we
  PASS and have appetite)

## 10. Open questions for operator before starting

- [ ] Do you have authoritative source links for the FY2024-25 STT
      rate on options sell premium? If yes, share; otherwise we
      verify against the budget memorandum PDF directly.
- [ ] Is the project's primary broker still Zerodha? Brokerage rates
      depend on broker.
- [ ] Stamp duty: are we Maharashtra-based (0.003%) or different state?
- [ ] PASS threshold: 1-lot net Sharpe > 0.5 is the locked bar.
      Acceptable, or should we relax to > 0.3 (more permissive) or
      tighten to > 1.0 (more conservative)?
- [ ] If FAIL, which of the three replan options would you prefer
      to commit to investigating first?
      (a) Institutional size, (b) Different strategies, (c) Retire?

## 11. Status

This document is the proposal. Phase 3-pre does NOT start until
operator commits to:
1. The charge rate verification approach (§3.1)
2. The PASS / YELLOW / FAIL criteria in §4
3. The replan-options menu in §4.3 if we FAIL
4. The discipline rules in §5

Once committed, Day 1 starts with charge rate verification — the
single most informative few hours of work this entire phase will
produce.
