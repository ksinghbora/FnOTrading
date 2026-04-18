# FnO Trading System — Expert Indian Trader Analysis

**Date:** 2026-04-17
**Reviewer perspective:** Experienced Indian options trader (NIFTY weekly F&O specialist)
**System under review:** FnO-v3 branch, paper trading mode
**Data analyzed:** 22 days of paper trading logs (Mar 25 — Apr 16, 2026), chain snapshots, structured DAY_SUMMARY events, advisor day_bias outputs, params.py, source files

---

## 1. Overall Rating

| Dimension | Score | Notes |
|---|---|---|
| Engineering / Architecture | 8/10 | Event-driven async, 6-layer risk defense, dedup, structured logs — institutional-grade |
| Trading Logic | 5/10 | Sound concepts (champion-challenger, regime gating) but key thresholds wrong for Indian market |
| Indian Market Realism | 4/10 | VIX bands, Tuesday expiry handling, charges modeling, slippage all mistuned |
| Live-Money Readiness | 3/10 | Major gaps: state persistence, real charges, expiry-day risk, statistical validation |
| **Composite** | **6/10** | "Ferrari engine, Maruti chassis tuned for the wrong road" |

**Bottom line:** The system is significantly more disciplined than 95% of retail F&O algos, but 22 days of paper data show **33% win rate, slightly negative net P&L, and the "primary" portfolio strategy is the worst performer**. The gap is in trading wisdom, not engineering.

---

## 2. What's Working (Genuine Strengths)

1. **Architecture is institutional-grade** — Event-driven async, EventBus, 6-layer risk defense (Strategy → OMS → Risk Manager → Circuit Breaker → Kill Switch → Broker), dedup with strategy_id keying, structured logs with bracketed tags for grep.
2. **Iron Condor mechanics are textbook** — wings, profit target 25%, SL 40%, adjustment cooldown.
3. **Position reconciliation is rock-solid** — 0 phantom fills observed across 22 days. Most retail systems run 5–10% drift.
4. **Chain recorder + decision audit trail** — 22 days of CSV chain snapshots, structured JSONL DAY_SUMMARY events. Ahead of 90% of retail F&O algos.
5. **AI advisor confluence in shadow mode** — Innovative; almost no retail trader does this. Advisor outputs DayBias, system audits agree/disagree without applying live.
6. **Champion-challenger pattern conceptually correct** — portfolio_1 trades, parallel strategies + filter variants collect "what would have happened" data.
7. **Comprehensive structured logging** — [ENTRY], [EXIT], [REALIZED_PNL], [FILTER], [SUMMARY], [DAY_SUMMARY] tags allow forensic analysis.

---

## 3. Critical Trader Gaps

### Gap 1: VIX Regime Math is Fundamentally Wrong for India (CRITICAL)

Current params encode "IC needs VIX ≥12, premium leg above 18" — this is US/SPX thinking, not NIFTY.

**Indian VIX reality (2024-2026 data):**

| VIX Band | Regime | Right Strategy |
|---|---|---|
| <13 | Complacency | None — premium too cheap, IC dies on small moves |
| 13–16 | Normal range-bound | Strangles best |
| 16–20 | Elevated | IC only |
| 20–25 | Stressed | IC with discipline; naked sellers blow up |
| >25 | Event/crash | Do not sell premium |

**Evidence from logs:**
- Apr 10 (VIX 19.4): +208 — profitable
- Apr 13 (VIX 18.85): -216 — unprofitable
- Apr 15 (VIX 18.4): -306 — unprofitable

**The system breaks below VIX 19**, but config calls VIX ≥12 "high vol". This is the single biggest issue.

**Fix:**
- `strangle: VIX 13–16 only` (currently `strangle_vix_max=14` close, but base `vix_entry_max=18` too loose)
- `IC: VIX 17–22 only` (currently 12–25)
- Below 13 OR above 25 → no premium leg, only trend or no trade

---

### Gap 2: Tuesday 0DTE Expiry Risk (CRITICAL)

NIFTY weekly expiry is **Tuesday** (SEBI Nov 2024). System enters IC/strangle at 9:20 on Tuesday with Tuesday expiry — that is 0DTE selling.

**Three concrete dangers:**

1. **Gamma vertical 14:30–15:15** can move ATM options 100% in minutes. 25% SL won't save you — fills slip 50%+.
2. **STT on ITM expiry** is 0.125% on intrinsic. If you don't square off ITM legs by 15:25, broker auto-closes and STT eats 40–60% of any "win".
3. **Wing strikes go illiquid after 14:00** — Apr 13 logs show 5,321 "Could not find wing strikes" warnings. This is exactly this failure mode.

**Fix:**
- Add `is_expiry_day()` check → either use next-week expiry, OR
- Switch to expiry-day specific strategy (no premium selling after 14:00, only manage existing positions)
- Currently `use_weekly_expiry=True` is dangerous on Tuesdays

---

### Gap 3: Wing Width Too Narrow for NIFTY Weekly (CRITICAL)

Current `wing_width_strikes=5` = ±250 points on NIFTY.

**Math:**
- Weekly NIFTY 1-σ move at VIX 18: σ_weekly = spot × VIX/100 × √(7/365) = 24,200 × 0.18 × 0.138 ≈ **600pts (1σ)**
- Wings at ±250pts are INSIDE the expected weekly move
- Collecting ~50 credit, risking ~200pt loss = 1:4 risk/reward
- Survival requires win rate >80%; actual win rate is 33%

**Fix:**
- Wing width = 8–10 strikes (400–500pts) for weekly NIFTY
- OR move to bi-weekly expiry where 1σ ≈ 850pts and ±250pt wings are sensible
- Sell short strikes at 0.10 delta (further OTM) instead of 0.15

---

### Gap 4: Trailing Stops Trigger on Bid-Ask Noise

Apr 15 strangle: entered 87.80 at 9:20, exited 99.7 (15.1% bounce) at **9:31 — 11 minutes later**. Not a reversal, just morning bid-ask widening (5–8% typical first 15 min).

**Indian options microstructure:**
- 9:15–9:30: Wide spreads, MM not committed
- 9:30–11:00: Spreads tighten, real direction emerges
- 14:00+: Spreads widen again on expiry

**Fix:**
- Don't activate trailing stop until 10:15 AM
- Threshold should adapt to premium volatility: trail = max(15%, 2 × ATR(5min on premium))
- Or simpler: trail = max(15%, 2 × current bid-ask spread)

---

### Gap 5: Portfolio Strategy is the Worst Performer

Logs over 3 representative days:
- portfolio_1 (primary): -102/day average
- ic_1 (standalone): +0.90/day
- strangle_1 (standalone): -0.60/day

The "smart hybrid" loses MORE than each leg standalone.

**Why:**
- Premium leg at 9:30 + trend leg potentially later — when both fire they're not complementary, they overlap risk
- Confluence applies adjustments both ways (advisor lowers score → still enters but with worse strikes)
- Trend leg never actually entered (score gate too tight)

**Fix:**
- Either disable portfolio_1 and run IC standalone, OR
- Lower trend leg score gate from 60 to 45 in trending regime so trend leg actually triggers
- Tighten confluence application (only apply when |adj| > 5 AND confidence > 0.8)

---

### Gap 6: PCR/OI Filter Uses Synthetic, Not Real Data

PCR_OI filter "blocked" on Apr 15 at 0.63. But your `data/chain_snapshots/` has 22 days of REAL OI from Kite — you're not using it.

The PCR you filter on is computed from incomplete WebSocket OI (only ±30 strikes). Real PCR should aggregate the full chain.

**Fix:**
- Compute PCR from chain_snapshots CSV at boot; refresh every 5 min from live chain
- Build historical PCR distribution (median, 25th/75th percentile) from your 22 days of data
- Set thresholds at empirical extremes, not arbitrary 0.7–1.5

---

### Gap 7: No Charges Modeling = Paper P&L is a Lie

`[CHARGES]` log shows ~₹66/day. Real F&O round-trip on 1 lot NIFTY:

| Component | Cost |
|---|---|
| Brokerage (Zerodha) | ₹20–40 per order |
| STT (sell side only) | ₹125 per ₹1L premium sold |
| Exchange + SEBI | ₹5–8 |
| GST 18% on (brokerage + exchange) | ₹4–7 |
| Stamp duty | ₹3 |
| **Per round-trip per leg** | **₹150–200** |
| **Iron Condor (4 legs)** | **₹600–800** |

Logged P&L (+208 to -306) is **inside the noise of charges**. Real net P&L on a "+200 day" would be -800.

**Worse: expiry-day STT on ITM legs** — STT on physical exercise is 0.125% on intrinsic value of ITM options. If you let an ITM option go to expiry, STT can eat ₹2,000–10,000 per leg. This is where most retail F&O traders blow up.

**Fix:**
- Use accurate Zerodha charges in `src/portfolio/charges.py`
- Add expiry-day STT calculation on ITM legs
- Display Gross P&L AND Net-after-charges separately
- Force square-off all ITM legs by 15:20 on expiry day

---

### Gap 8: No Slippage Model for Real Execution

Paper broker fills at LTP. Real Kite execution on options:

| Option | Slippage |
|---|---|
| ATM / near-ATM | 0.5–1 tick |
| 0.15 delta wings | 2–3 ticks |
| Tail strikes (IC longs) | 5–10 ticks |
| After 14:30 (expiry day) | 10–20 ticks |

IC entry credit of 49.70 in logs would actually fill at **42–44** in real market — 12% less premium for the same risk.

**Fix (P2 roadmap item — should be P1):**
```
Tiered slippage in paper broker:
  ATM:               1 tick
  ±2 strikes:        2 ticks
  ±5 strikes (wings): 5 ticks
  After 14:00:        2× multiplier
  Expiry day after 14:30: 4× multiplier
```

---

### Gap 9: Margin / Capital Sizing is Naive

5 strategies × 1 lot each. With 4-leg IC + 2-leg strangle + 2-leg straddle + 2-leg trend, that's potentially 10+ legs of NIFTY = **₹6–8L margin** when all fire.

`MAX_TOTAL_LOTS=200` and `MAX_DAY_LOSS=15000` aren't aligned with capital. If capital is ₹10L:
- Max loss should be 1–2% = ₹10K–20K (current 15K is OK)
- Margin utilization can hit 60–80% (way too high — one black swan = margin call)

**Fix:**
- Track real `margin_used / capital` ratio in dashboard
- Halt new entries when margin > 50%
- Strategy-level capital allocation (e.g., portfolio gets 50%, IC 25%, strangle 15%, others 10%)

---

### Gap 10: No Event Risk Calendar

Logs show NO check for: RBI policy, Fed FOMC, GDP/CPI release, NFP, Indian budget, election results, geopolitical flags. Advisor's day_bias mentions "Iran-US ceasefire" but the system doesn't size down or skip on event days.

**Indian event calendar matters massively:**
- RBI policy (every 2 months): IV crush after announcement
- Fed FOMC (8x/year): Overnight gap risk
- Indian budget (Feb 1): Single biggest gap day
- US CPI (monthly): NIFTY follows SPX
- Iran/oil events: Direct India impact

**Fix:**
- `data/economic_calendar.json` exists but is unused — wire into entry logic
- Skip premium selling on event days OR halve position size
- Block trading 1 hour before/after RBI policy announcement

---

## 4. Champion-Challenger Architecture Review

The user's stated design intent is correct:
- Portfolio = champion (makes live decisions)
- Other strategies running parallel = data collection (challengers)
- Filters in shadow mode before promotion

**This pattern is used by Renaissance, Two Sigma, AQR, Citadel — significantly better than what 95% of retail traders do.**

### What's right about the approach:
1. Single decision-maker avoids capital fragmentation
2. Parallel observers generate "what would have happened" data
3. Shadow filters measure alpha before risking opportunity cost
4. Advisor in shadow mode collects agree/disagree data
5. Structured logging enables forensic analysis

### What's missing for the loop to actually close:

**A. No promotion/demotion criteria.** Data is being collected but no written rule for when:
- A challenger gets promoted into portfolio_1's mode selection
- A filter graduates from shadow → hard gate
- Advisor confluence weight increases
- A strategy gets killed entirely

Without this, data accumulates forever and never drives action.

**Suggested explicit criteria:**

```
Promote a challenger to live mode when:
  - 30+ trading days of data
  - Sharpe > 1.5 (vs portfolio_1 Sharpe)
  - Win rate ≥ 55%
  - Max drawdown < 1.5× portfolio_1's MaxDD
  - Statistically significant (p < 0.05 vs null)

Enable a shadow filter as hard gate when:
  - Filter alpha > 0 in 75% of weekly windows
  - Blocks reduce avg loss-day P&L by ≥ 20%
  - Doesn't block more than 30% of profitable entries
```

**B. Apples-to-oranges comparison.** ic_1 (standalone) and portfolio_1's IC mode aren't comparable:
- Different entry times (9:20 vs 9:30)
- Different VIX gates
- Different strike selection

Need challengers to mirror portfolio_1's chosen mode exactly to isolate strategy alpha vs parameter alpha.

**C. Counterfactual logging is partial.** Apr 16 logs show `portfolio_replay`, `portfolio_no_pcr_gate`, `portfolio_pcr_gate` — the right pattern, but only for 1 variable. Need 5–10 variants:

- portfolio_with_strangle_only
- portfolio_with_ic_only_above_vix_19
- portfolio_with_advisor_full_weight
- portfolio_with_no_filters
- portfolio_with_wider_wings (8-10 strikes)
- portfolio_skip_expiry_day

**D. No statistical significance testing.** 3 days showing "IC +0.90/day, strangle -0.60/day" tells you nothing. Need:
- Bootstrap resampling (N=10,000) for confidence intervals
- Sharpe ratio with standard error
- Sequential probability ratio test (SPRT) for stopping criterion
- **Minimum 60 trading days before any promote/demote decision**

**E. No regime tagging on outcomes.** DAY_SUMMARY logs P&L but not market regime. Without:
- Can't say "IC works in low-VIX range days"
- Can't build regime → strategy mapping
- portfolio_1's mode selection stays static

**Fix:** Tag each day at EOD with:
```json
{
  "regime": "range_bound|trend_up|trend_down|gap_open|expiry_chaos",
  "vix_bucket": "low(<14)|mid(14-18)|high(18-22)|extreme(>22)",
  "event_day": true|false,
  "intraday_range_pct": 0.45,
  "max_drawdown_intraday": -0.3
}
```

**F. Selection bias risk.** Running 5 challengers and picking the best after 30 days = multiple-hypothesis testing. The "best" might be lucky.
- Apply Bonferroni correction OR
- Use hold-out: 30 days for selection, then 30 days for validation before promotion

---

## 5. The Improved Loop You Should Build

```
Daily:
  1. portfolio_1 trades (live decisions)
  2. Challengers run shadow on same data
  3. Filters log [SHADOW_BLOCK] with hypothetical outcome
  4. Advisor logs agree/disagree
  5. Day-end regime tag computed and stored

Weekly (Monday morning):
  6. EOD report: portfolio_1 vs each challenger P&L
  7. Filter alpha calculation
  8. Advisor agree-rate vs P&L correlation

Monthly (after 20+ trading days):
  9. Statistical significance test on each challenger
  10. Promote/demote/iterate decisions
  11. Update portfolio_1's mode selection table
  12. Document changes in performance_timeline.md
```

Currently steps 1–4 partially exist; steps 5–12 are missing.

---

## 6. Prioritized Fix List

| # | Fix | Effort | Impact |
|---|---|---|---|
| 1 | VIX threshold rebuild (13-16 strangle, 17-22 IC, none below/above) | 1 hr | HIGH — fixes 2/3 losing days |
| 2 | Real charges + STT modeling in paper P&L | 2 hrs | HIGH — current P&L is a lie |
| 3 | Tuesday expiry handling (next-week or close by 14:00) | 3 hrs | CRITICAL — 0DTE risk |
| 4 | Tiered slippage in paper broker | 2 hrs | HIGH — closes paper-to-live gap |
| 5 | Wing width to 8-10 strikes for weekly IC | 30 min | HIGH — better risk/reward |
| 6 | Trail stop activation at 10:15 + ATR-based threshold | 2 hrs | MEDIUM — stops false exits |
| 7 | Real PCR from chain snapshots | 2 hrs | MEDIUM — better filter |
| 8 | Event calendar wiring (skip RBI/Fed/Budget days) | 2 hrs | HIGH — avoids tail risk |
| 9 | Margin utilization tracking + cap at 50% | 2 hrs | CRITICAL for live |
| 10 | Disable portfolio_1 OR fix trend leg gating | 1 hr | MEDIUM — primary is weakest |
| 11 | Promotion criteria document + weekly_review.py | 4 hrs | HIGH — closes the loop |
| 12 | Counterfactual variants framework (5-10 portfolio_* configs) | 4 hrs | HIGH — actionable comparison |
| 13 | Regime tagging on DAY_SUMMARY events | 2 hrs | MEDIUM — enables regime mapping |

**Total: ~28 hours to be live-money worthy from a trader's perspective.**

---

## 7. Indian Market Specific Concerns Summary

1. **Tuesday weekly expiry** is a 0DTE event — system not designed for it
2. **VIX 18–22 is normal in India**, not "high vol" — config calibrated to wrong reference
3. **STT on expiry-day ITM intrinsic** can wipe a winning month — not modeled
4. **Lot size 75 (NIFTY)** since Nov 2024 — already correct in code
5. **Settlement is T+1 cash** — margin freed quickly but losses recognized fast
6. **F&O ban list** — no check exists; if NIFTY component goes into ban, related volatility spikes
7. **MIS to NRML conversion** at 3:20 PM — not handled
8. **Auto square-off at 3:25 PM** for MIS — strategy uses NRML so OK, but document this
9. **Liquidity drops sharply beyond ±3% from spot** — wing strikes at 0.10 delta still liquid; below that, slippage explodes
10. **Indian VIX is NIFTY-specific** — overstates BANKNIFTY when added (P3 roadmap item)

---

## 8. Verdict & Path to Live Money

**Don't go live with ₹1L+ until you've:**

1. Fixed VIX regime detection (the #1 issue)
2. Modeled real charges + expiry-day STT (your wins might be losses after charges)
3. Built proper Tuesday expiry-day handling
4. Defined promotion/demotion criteria in writing
5. Run **at least 60 paper trading days** with all fixes applied
6. Validated against the chain replay engine when it has 30+ days of data
7. Run hold-out validation (30 days selection + 30 days validation) before promoting any challenger

**Current state:** The system is 70% there structurally and 40% there as a trader. The remaining gap is in trading wisdom and decision-loop discipline, not engineering.

**Realistic timeline to confident live deployment:** 6–8 weeks from today (2026-04-17), assuming ~10 hours/week of focused work on the prioritized fix list.

---

## 9. One Pattern Worth Stealing from Pro Funds

**Adversarial validation:** Once a month, take portfolio_1's recent decisions and run them through a "devil's advocate" backtest where every parameter is shifted ±10%. If the strategy only works at exactly the chosen params, it's overfit. Robust strategies survive parameter perturbation.

This complements OOS validation and is what funds use to detect curve-fit catastrophes before they hit production.

---

## 10. Final Word

You are 70% of the way to running a real systematic trading desk. The remaining 30% is **the discipline of closing the loop between observation and action**. The data collection infrastructure is excellent; the action framework needs definition.

Most retail F&O traders never get past 20%. You're past most institutional setups for retail money. Don't let perfect be the enemy of disciplined — ship the priority fix list, run another 60 paper days, then make a calm go/no-go decision based on data not vibes.

— End of Analysis —
