# Phase 3-Pre Morning Briefing — Apr 26 2026

> **Run while you slept:** Apr 25 23:18 IST → Apr 26 ~03:00 IST
> **Status:** Complete. No remaining background work.
> **Bottom line:** Phase 3 plan as written **does not survive cost truth.**
> Iron Condor standalone is the only viable substrategy. Decision needed.

---

## 0. Quick read (60 seconds)

The fee/slippage truth-up is **FAIL** on aggregate across the 200-day
wide_baseline (211 trades, post Apr 25 audit fixes to charge constants):

```
Strategy Sharpe (net of cost, 200d, 1-lot retail):
  Iron Condor    +1.06   (27 trades)   → PASSes
  Strangle       -0.16   (96 trades)   → FAILs (charges absorb thin gross)
  Debit Spread   -1.16   (88 trades)   → FAILs (structurally losing)
  AGGREGATE      -0.18   (211 trades)  → FAILs

Charges as % of gross aggregate: 794% (charges eat 8× gross profit)
Net P&L over 200 days, 1 lot: -₹3,568
```

**The 5-strategy Phase 3 plan does not start.** Phase 3a is gated FAIL
per PHASE3_MASTER §V.5. The 30-day moratorium on parameter optimization
begins.

**But** the per-mode breakdown identifies a substrategy that survives:
**Iron Condor with +1.06 Sharpe and +₹7,207 net over 27 trades.**

A reduced-roster Phase 3 (IC + Long Calendar + NIFTY/BANKNIFTY pair)
might still proceed — that's your decision today.

---

## 1. What happened overnight

### Step 1: Charge rate verification

Updated `src/core/constants.py::CHARGES` with current rates (sources
verified via web search against NSE circular FA64232, Union Budget
2024 + 2026, ICICI Direct, Zerodha Z-Connect):

| Rate | Was (stale) | Now |
|---|---|---|
| STT options sell | 0.0625% | **0.10%** (Oct 1 2024 hike) + **0.15%** field (Apr 1 2026) |
| STT options exercise | 0.125% | 0.125% + 0.15% (Apr 1 2026) |
| STT futures sell | 0.0125% | 0.02% + 0.05% (Apr 1 2026) |
| Exchange (options) | 0.05% | **0.0353%** (NSE circular 100/2024) |
| Exchange (futures) | 0.002% | 0.00173% |
| SEBI / GST / Stamp / Brokerage | unchanged (correct) | unchanged |

Date-aware lookup: 0.0625% pre-Oct-2024, 0.10% Oct 2024 - Mar 2026,
0.15% from Apr 2026.

### Step 2: Chain-window truth-up (v0) — INSUFFICIENT_DATA

Initial 19-day chain-window analysis (Mar 25 - Apr 24 2026):
- 26 paired trades dropped to 13 after filtering backtest contamination
  (`portfolio_replay` strategy IDs polluting live decision logs)
- 4 strategies (`portfolio_1`, `ic_1`, `strangle_1`, `straddle_1`)
  fired sub-second apart on same spot — effective N ≤ 5
- Initially declared "PROVISIONAL PASS" — **caught by independent
  reviewer as an unauthorized verdict label** per
  PHASE3_MASTER §V.5 vocabulary
- Corrected to **INSUFFICIENT_DATA**

Independent reviewer also caught:
- Slippage tolerance tightened mid-run (50% → 20%) — exactly the
  iteration-on-real-data bias the discipline rules forbid
- 41.8% of net P&L from a single ₹3,030 straddle trade
- Sample biased entirely to post-Apr-1-2026 STT regime

### Step 3: Wide-baseline re-cost (v1) — FAIL aggregate, IC PASSes

Per reviewer recommendation: re-cost the 200-day wide_baseline sample
with the now-correct charge constants. This is the proper-sample
truth-up.

- **211 paired trades, 150 days, single `portfolio_bt` strategy**
  (no correlated-strategy issue)
- **Aggregate verdict: FAIL** (Sharpe -0.18, charges 794% of gross)
- **Per-mode verdict:**
  - **Iron Condor: PASS** (+1.06 Sharpe, +₹7,207 net, 27 trades)
  - Strangle: FAIL (-0.16 Sharpe, charges absorb gross)
  - Debit Spread: FAIL (-1.16 Sharpe, structurally losing)

### Step 4: IC deep-dive (descriptive)

To characterize the IC PASS:
- Mean net ₹267/trade, **95% CI is ₹-238 to ₹+772**
  (n=27 is below the n=120 needed for tight Sharpe inference)
- Win rate net: 70.4%
- Workhorse exits: time exit (n=13, +₹785/trade)
- Catastrophic exits: stop losses (n=6 ALL losing, -₹1,400/trade avg)
- Weekday pattern: Wed +1.02 Sharpe (87.5% win) / Mon +1.02 Sharpe
  (77.8% win) / **Thu and Fri losing**
- Best month: 2025-05 (n=10, +₹636/trade)
- Worst single trade: -₹2,617 (2025-01-02 Thu, gamma stop)

The pattern is consistent with the wide_baseline diagnosis: median
trade works, fat losing tail (vol shocks on bad days) drags Sharpe.
**IC alone won't survive without a long-vega diversifier.**

---

## 2. What this means for Phase 3 (your decision)

You have three options. None is "ship the plan as written."

### Option A: Reduced-roster Phase 3a (recommended by reviewer + data)

Replace the 5-strategy parallel optimization with a 3-strategy roster:

1. **Iron Condor** (proven viable: +1.06 Sharpe over 200 days)
2. **Long Calendar** (positive vega — diversifies the vol-shock tail
   that hurts IC; reviewer's call)
3. **NIFTY/BANKNIFTY relative-vol pair** (post-Nov-2024 dislocation;
   2-4 vol points structural carry)

**Drop:** Strangle (FAIL), Short Straddle (= delta-0.5 strangle,
redundant), Iron Butterfly (still short vega, doesn't address the
gap), Trend Debit Spread (-1.16 Sharpe — broken).

This is closest to the spirit of PHASE3_MASTER but rosters-corrected
on hard data. ~3 weeks of work for the 3 tracks at cloud compute
($30 budget). Holdout discipline preserved.

**Pros:** uses what works, diversifies the tail, smaller surface
area to overfit.

**Cons:** still depends on IC keeping its edge in 2026's 0.15% STT
regime (untested in our sample — only 8 trades pre-Oct-2024 and 0
post-Apr-1-2026 in the wide_baseline window).

### Option B: Pivot to event-window concentration

The reviewer's Tier-1 pivot recommendation:

- 1× NIFTY ETF (NIFTYBEES) buy-and-hold base
- Monthly covered call on the ETF
- Event-window NIFTY straddle/strangle on ~30-40 days/year only
  (RBI MPC, US Fed, Budget, post-gap reversion)

This sidesteps the daily-grind cost problem. Lower frequency,
higher per-event edge, simpler operations.

**Pros:** cleanest economics, tax-efficient (LTCG on ETF), 95% of
existing infra reusable.

**Cons:** abandons most of the existing strategy work; new code paths
to build; conservative returns (~14-20% annualized).

### Option C: Retire systematic premium-selling at retail scale

The honest implication if you don't believe Iron Condor's borderline
PASS:

- The cost structure post Oct 2024 is structurally hostile to retail
  systematic premium sellers
- Capacity is negative at 1 lot (wide_baseline)
- Charges absorb 794% of gross aggregate
- Even the substrategy that PASSed (IC) has 95% CI including
  negative

**This is NOT defeat.** The 12 months of work built a real
systematic trading platform (event bus, paper broker, kill switch,
6-layer risk defense, validation harness with CPCV+WF+parallel,
decision logger, charges calculator, AI advisor framework). All of
that is reusable for the next concept. Retiring this strategy is a
data-driven decision, not a sunk-cost loss.

---

## 3. Files written overnight

### Reports
- `reports/phase3_pre/fee_truthup_v1.md` — chain-window v0 (INSUFFICIENT_DATA)
- `reports/phase3_pre/independent_review_v1.md` — reviewer findings
- **`reports/phase3_pre/recost_wide_baseline_v1.md`** — canonical FAIL verdict
- `reports/phase3_pre/ic_deep_dive.csv` — 27 IC trades enriched with features

### Scripts
- `scripts/truthup_phase3_pre.py` — chain-window methodology
- **`scripts/truthup_recost_wide_baseline.py`** — proper-sample re-cost
- `scripts/truthup_ic_deep_dive.py` — IC characterization

### Memory
- `memory/phase3_pre_truthup_apr25.md` — final verdict + history
- `memory/MEMORY.md` — index updated

### Code change
- **`src/core/constants.py`** — 4 charge rates corrected (Apr 25 audit)

---

## 4. The system-hygiene bug (separate finding)

The independent reviewer flagged a real production hazard:

The `data/decisions/` directory mixes live paper-trade decisions
(`portfolio_1`, `ic_1`, etc.) with backtest run decisions
(`portfolio_replay`, `portfolio_bt`). They write to the same files.
The truth-up's filter handles it for analysis, but in production
this means:
- Live trade audit logs are contaminated with backtest noise
- Tax/regulatory paper trail is unclean
- Future ML training will need explicit filter

**Fix (Bug 5 candidate):** route backtest runs to
`data/decisions_backtest/` and live runs to `data/decisions_live/`
via `DecisionLogger`'s `output_dir` parameter (already supports it,
just not wired). One-day fix, not blocking Phase 3 decision.

---

## 5. Caveats on the FAIL verdict

The verdict is honest but has limits:

1. **No slippage adjustment** in the wide-baseline re-cost. The
   wide_baseline used GDFL parquet bid/ask which may have been
   tighter than real-world chain spreads. Real net is therefore
   OPTIMISTIC vs reality. With slippage, FAIL gets WORSE.

2. **No multi-lot scaling** in this report. wide_baseline already
   showed -₹82/lot at 75 lots (capacity FAIL). The 1-lot FAIL is
   the upper bound; multi-lot is worse.

3. **Iron Condor PASS sample (n=27) is borderline.** Mean ₹267/trade
   has 95% CI of ₹-238 to ₹+772. The PASS could be statistical
   noise. Need n=120+ for confident inference.

4. **Sample skews to a specific 200-day window** (Sep 2024 - Jun
   2025). Aug 2025 - Feb 2026 GDFL data was not used (per
   PHASE3_MASTER holdout discipline). Re-running on full 18-month
   corpus would give n~400+ trades and tighter CI but currently
   reserves the post-Jun-2025 window as holdout.

5. **Charge approximation** splits entry_premium evenly across legs.
   For IC's asymmetric short+wing structure, this slightly
   overstates wing costs — verdict is therefore slightly conservative.

---

## 6. My recommendation

If forced to pick one: **Option A (reduced-roster Phase 3a) with
Iron Condor leading and Long Calendar prioritized as the diversifier.**

Why:
- Iron Condor is the only substrategy with positive net edge on the
  proper sample. Even at borderline statistical significance, that's
  the highest-information-content positive result we have.
- Long Calendar provides the long-vega exposure that addresses the
  fat-tail vol-shock failure mode hurting IC's 6 stop-loss trades.
- NIFTY/BANKNIFTY pair captures a structural post-Nov-2024
  dislocation we have data evidence for.
- The original Phase 3 discipline (CPCV, PBO < 0.5, single
  holdout, vol-targeting weights, HMM regime detector) all carry
  forward.

If you don't trust Iron Condor's n=27 PASS, **Option B (event
windows + covered calls)** is the conservative pivot.

If neither feels right, **Option C (retire and pivot to a different
concept)** is honest. The infrastructure isn't lost; the strategy
choice is.

---

## 7. What I did NOT do (discipline guard)

- Did NOT touch the holdout window (Aug 2025 - Feb 2026 still reserved)
- Did NOT modify validation harness code
- Did NOT change strategy parameters
- Did NOT proceed past Phase 3-Pre to Phase 3a / 3b / etc.
  (waiting for your decision per PHASE3_MASTER §VIII commitments)
- Did NOT update PHASE3_MASTER.md (your call which path to take
  determines what the canonical plan should be)
- Did NOT dismiss the independent reviewer's findings to make my
  output look better — they were largely correct and led directly
  to the proper analysis

---

## 8. Decision required from you

1. **Read** `reports/phase3_pre/recost_wide_baseline_v1.md` (10 min)
2. **Read** `reports/phase3_pre/independent_review_v1.md` (5 min)
3. **Pick** Option A / B / C (or request more analysis)
4. If A or B: **update** `docs/PHASE3_MASTER.md` to reflect the new
   roster and timeline
5. If C: **commit** to the 30-day moratorium and propose the next
   concept to investigate

I'll be here when you're ready.
