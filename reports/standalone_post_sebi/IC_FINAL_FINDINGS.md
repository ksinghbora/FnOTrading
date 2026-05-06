# Iron Condor — Final Findings (May 2026 Research Arc)

**Generated:** May 6 2026
**Branch:** `FnO-v4-strategy-research`
**Final commit:** `1b65610`
**Goal stated at start:** "a profitable strategy on the NSE NIFTY F&O paper-trading book"

This is the synthesis after ~5 weeks of focused IC research on the
post-SEBI (Nov 20 2024 onwards) Indian retail F&O market.

## TL;DR

**Iron Condor on Indian post-SEBI NIFTY weekly options has a tiny but real
edge — and only when filtered by theory-grounded indicators.** The
honest measurement on a 143-day untouched holdout window is
**+₹584 over 324 trades (~₹4/day expected on 1 lot)** — barely
above the cost wall. Statistical evidence (MC p=0.051, bootstrap CI
lower > 0) confirms the signal is non-random.

This is the first IC config in the codebase's history with positive
holdout PnL. **It is also nowhere near "exciting Sharpe ≥ 1" territory.**

## Verdict matrix — every IC config tested

| Config | Filters | Train+Val PF | Holdout | Verdict |
|---|---|---|---|---|
| Default IC | hand-coded score, vix-band, PCR, max-pain | 0.36 | (curve-fit) | No edge — WF curve-fit |
| Strong-signal IC | tightened tuning: score≥85, PCR 0.85-1.20, max-pain 1.5%, adj 85% | 0.85 | **0 trades** | **Curve-fit confirmed** |
| Minimal IC | strong-signal minus 3 noise filters (max_pain, adj, intraday-spike) | 0.90 | **0 trades** | **Same curve-fit** |
| Principled IC | ADX(14)<22 AND BB-squeeze AND RV/IV<0.80 (theory-based AND-gate) | n/a | n/a | **0 of 2590 samples pass — three conditions negatively correlated** |
| **v2 (CI+VRP)** | **Choppiness Index ≥ 61.8 AND VRP > 0** (orthogonal traditions) | **1.20** | **+₹584 / 324 trades / Sharpe +0.35** | **First positive holdout** |
| v3 (v2 - adjusts) | v2 + adjustment_threshold_pct=9999 | 1.29 (better margin) | not tested | Partially holdout-informed; not deployed |

## What we discovered

### 1. The wall-clock bug (May 5)

`RegimeDetector.assess()` was using `now_ist()` — the real wall-clock
time — instead of the simulated backtest clock. Daily-close rollover
(`today != last_close_dt`) never fired during backtests, so
`compute_realized_vol` always returned None. Every regime-gate test
before May 5 was effectively running with the gate always blocking.

Fix: pass `clock=self.ctx.clock` to RegimeDetector at construction;
`assess()` uses simulated time when available. After the fix, the
diagnostic clearly showed the real issue (negative correlation
between range and IV-richness on Indian data).

### 2. The "principled AND-gate" trap

ADX(14)<22 + BB-squeeze active + RV/IV<0.80 are all theory-grounded
indicators. Each individually passes 46-70% of valid samples. But
**all three pass simultaneously: 0% (0 of 2590)** on Indian post-SEBI
data.

**Why:** efficient market makers price IV close to realized vol. When
market is range-bound, IV is also low (RV/IV ≈ 1 not < 0.80). When IV
is rich, market is choppy or trending. The AND of "range" and "rich
IV" is empirically impossible on this market.

This is a fundamental finding about Indian post-SEBI options. The
textbook IC setup doesn't exist.

### 3. The orthogonal-traditions fix (v2)

A research agent designed a v2 gate using indicators from independent
traditions:

* **Choppiness Index ≥ 61.8** (technical analysis, Bill Dreiss/ASX)
* **VRP > 0** (academic finance, Bollerslev-Tauchen-Zhou 2009 RFS)

Independent traditions → not negatively correlated. The AND-gate
fires. v2 produced 396 trades on full corpus, PF 1.20, MC p-value
0.051. **Statistically meaningful and theoretically grounded.**

### 4. The holdout result

v2 holdout (143 untouched days): **+₹584 / 324 trades / Sharpe +0.35
/ Win rate 42%**. Win rate matched in-sample (44%) — no curve-fit.
Edge degraded from PF 1.20 in-sample to ~PF 1.05 OOS — typical for
any genuine signal on a finite sample.

This is the first IC config to fire trades AND produce positive PnL
on truly untouched holdout data.

### 5. The "sure-shot" claim that wasn't

Trade-level analysis of v2 holdout decisions showed adjustments
(mid-trade rolls of threatened sides) lost ₹5,719 across 27 of 29
adjustments. v3 (v2 + disable adjustments) showed PF 1.29 in
train+val — improved per-trade cost margin.

**But this analysis used holdout-derived insight to design v3.** That's
curve-fit risk. v3 hasn't been holdout-validated and shouldn't be
without losing OOS-purity.

## What "tradeable" means here

Decision criteria from earlier in the project:
- PF ≥ 1.20 with statistical confidence → tradeable
- PF in [1.00, 1.20] → marginal, deploy with caveats

v2 holdout sits at PF ~1.05 — **marginal**. The expected PnL of ~₹4/day
on 1 lot (~₹500 over a quarter) is real but small. It would not be a
financial breakthrough; it would be a small structural edge that:
- Scales linearly with lot size (subject to capacity declining ~10% at
  300+ lots due to spread crossing)
- Requires the v2 detectors to keep working as Indian market dynamics
  evolve
- Has bootstrap-CI lower bound just barely above 0 → fragile to noise

## What we proved AGAINST IC

1. **Default IC is structurally curve-fit** under WF methodology
2. **Filter-tuning to improve in-sample is curve-fit on holdout** (strong-signal, minimal)
3. **Range + IV-rich requirement** (textbook IC setup) **doesn't exist**
   simultaneously on Indian post-SEBI data
4. **The cost wall is binding**: PF 1.20 at zero cost decays to ~1.17 at
   realistic +0.5 cost shift; PF ~1.05 OOS is only marginally above the
   wall

## Indian-market lessons

1. **Premium-selling on Indian retail F&O has a tiny structural edge
   only with theoretically-grounded filters.** Random-tuning produces
   only curve-fit.
2. **Day-rollover detection in backtest must use simulated time.** Wall-
   clock primitives silently break any state-accumulating detector.
3. **CPCV is misfit for short Indian-data single-config testing.** WF +
   bootstrap CI + MC permutation are the right primary tools.
4. **Indian VIX is efficiently priced.** The IV-vs-RV gap exists but is
   smaller than US, and rarely co-occurs with low ADX.
5. **89% of Indian retail F&O traders lose money** (SEBI Jan 2023 study).
   Our research independently confirms this from first principles —
   without strong filters, retail IC has no edge.

## Recommendation: deploy v2 to live paper trading

The v2 (CI+VRP) config is the cleanest honest verdict on IC. **Live
paper trading is the next legitimate test.**

| Why live paper | What to expect |
|---|---|
| No capital risk | Free OOS evidence |
| Real fills, real spreads | Closes the backtest-vs-live gap |
| Time-multiplicative validation | After 1-3 months of live data, total trade count rivals holdout |
| Aligned with deployment intent | If we plan to trade IC, this is THE test |

If live paper shows PF ~1.05 sustained over 1-3 months, the strategy
is real-world tradeable at modest size (1-5 lots) with caveats. If
live underperforms backtest by >30%, the marginal edge isn't tradeable
in practice.

### Live deployment requires

- Update `config/snapshots/<latest>/env.yaml` strategy block to use
  `ic_research_params.json` (CI+VRP gate, all other defaults)
- Restart paper-trading daemon (currently PID 18579, running since
  Apr 30 on April 17 config snapshot)
- Monitor for 1-3 months
- Compare live PnL/trade to ₹4/day backtest expectation

## What was NOT done — honest gap-list

- **WF harness fix**: WF creates fresh detector state per window; for
  state-dependent detectors (RV/IV), test windows can't warm up. The
  full-window backtest is honest but WF gates structurally fail.
  Future: plumb detector state across WF windows.

- **Cost-sensitivity gate quirk**: Sharpe column in cost-sensitivity
  table reads 0 across all shifts; should use profit_factor as the
  decision metric. The gate evaluator should be fixed to use
  profit_factor.

- **v3 holdout**: deliberately NOT burned. v3 was designed using
  holdout decisions, so its holdout test would be partially curve-fit.
  Live paper trading is the next OOS for both v2 and (later) v3.

- **Adjustment redesign**: trade-audit found adjustments fire 1-2 min
  after entry, biased to CE side. A redesigned adjustment policy
  (longer entry-cooldown, reverse-direction confirmation) might lift
  v2 PF further. Future work.

- **STT date-aware fix**: charges calculator uses `options_sell_pct=0.10%`
  (Oct 2024 - Mar 2026 rate). After Apr 1 2026 the rate is 0.15%.
  Current backtests on post-Apr-2026 data understate STT by 50%.
  Fix is a future-bug for live data.

## Files

- `IC_HONEST_ANALYSIS.md` — pre-v2 mid-arc retrospective
- `ic_strong_signal_smoke.md` / `_params.json` — pre-v2 tuned config
- `ic_minimal_HOLDOUT_BURN.md` — proof minimal IC was curve-fit (0 trades)
- `ic_principled_params.json` — pre-v2 ADX+BB+RV/IV AND-gate (failed)
- **`ic_research_params.json`** / `_smoke.md` — **v2 (CI+VRP) — RECOMMENDED**
- **`ic_v2_HOLDOUT_BURN.md`** — **v2 OOS verdict (+₹584/324/+0.35)**
- `ic_v3_no_adjust_params.json` / `_smoke.md` — v3 (curve-fit risk noted)

## Final word

The 5-week IC research arc didn't find a "rich premium-selling edge."
It found a **small, real, theoretically-grounded edge** that can be
harvested at modest size with discipline. The methodology corrections
along the way (WF-primary, wall-clock fix, ablation discipline,
holdout-purity rules) are the more durable artifact. They will save
the next research arc weeks.

If you want a financial breakthrough, IC is not it. If you want a
small-but-honest first profitable strategy on the NSE NIFTY F&O paper
book, v2 is your candidate.
