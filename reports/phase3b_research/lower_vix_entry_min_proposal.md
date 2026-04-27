# Phase 3b — Lower vix_entry_min for IC (Hypothesis Pre-Registration)

**Created:** 2026-04-27
**Branch:** `FnO-v4-strategy-research`
**Status:** PRE-REGISTERED — for testing on Slice B only (Oct 1 – Nov 30 2025)
**Discipline reference:** §VII.2 (single-access), §VII.6 (one change per run), §VII.7 (no iterative tuning)
**Slice allocation:** [holdout_allocation.md](holdout_allocation.md) §5

---

## 1. Hypothesis statement

**H1 (alternative):** Lowering `iron_condor.vix_entry_min` from 16.0 to 14.0 will materially improve walk-forward coverage by capturing trading days in the VIX 14-16 band that are currently filtered out.

**H0 (null):** No difference in wf_coverage when vix_entry_min = 14.0 vs 16.0.

The test should reject H0 in favor of H1 if wf_coverage improves by >= 10 percentage points.

---

## 2. Rationale grounded in Phase 3a-revised data

### 2.1 The Window 7 problem

From `reports/standalone_v1/iron_condor_validation.md` §4:

| Window | Period | Test Sharpe | # trades | Notes |
|---|---|---|---|---|
| 7 | Jun 19 – Jul 30 2025 | **0.00** | **0** | **Zero entries** — VIX dropped below 16 |

Window 7 is 1 of 8 walk-forward windows. With zero entries, it counts as 0 in `fraction_positive_test` (62%) — pulling the metric down. If it had even 30% positive trade days, wf_coverage would be 75%.

### 2.2 The Window 2 small-sample problem

| Window | Period | Test Sharpe | # trades |
|---|---|---|---|
| 2 | Feb 25 – Apr 11 2025 | -2.95 | 12 |

Only 12 IC entries over 35 days. The window's loss came from 4 of those 12 trades being on Apr 7-9 (tariff shock days). Most VIX-16+ days were in the shock period; days with VIX 14-16 were filtered.

### 2.3 Why VIX 14-16 days are likely profitable for IC

- IC is defined-risk (wings cap losses), so it tolerates lower-vol days where naked strangle would suffer
- Phase 3a-revised regime gate showed IC PASSES across all regime buckets
- IC's cost_sensitivity gate passes at +0.5× shift (Sharpe +0.50), suggesting real edge survives modest premium reduction
- Lowering threshold to 14 captures days where strangle can't trade (its band is 13-16) but IC's wing protection matters less

### 2.4 Risk: dilution of edge

VIX 14-16 entries collect less premium than VIX 16-22. There IS a real risk that adding these trades dilutes the per-trade Sharpe, even if total trade count rises.

**Quantitative risk bound:** if mean Sharpe drops more than 30% from baseline (e.g., +2.30 → +1.60 or below), the dilution exceeds the coverage gain. We treat this as a failure threshold.

---

## 3. The exact change to test

**Single parameter change:**
```
IronCondorParams.vix_entry_min: 16.0 → 14.0
```

**No other params changed.** No threshold sweep, no combined Gate B test, no different wing width.

**Code mechanism:** Runtime override via `--baseline-params-json '{"vix_entry_min": 14.0}'`. No code modification required. The default stays at 16.0 to protect existing behavior.

---

## 4. Pre-registered expected impact

### 4.1 wf_coverage

Phase 3a-revised baseline: **62%** (5 of 8 windows positive)

Expected with vix_entry_min=14.0:
- Window 7 should now have entries (was 0). Likely outcome: +0 to +1 window flips positive
- Window 2 should have more entries (was 12). Likely outcome: ~30-50 trades, may stay negative due to Apr 7 shock but with smaller magnitude
- Other windows: minimal change (most already had VIX 16+)

**Pre-registered expected wf_coverage:** **70-75%** (+8-13pp from 62%)

### 4.2 Mean CPCV Sharpe

Phase 3a-revised baseline: **+2.30**

Expected with vix_entry_min=14.0:
- Lower-vol days have thinner premium → per-trade Sharpe lower
- More trades → law-of-large-numbers makes outliers less impactful
- Net effect uncertain; conservatively expect slight degradation

**Pre-registered expected mean Sharpe:** **+1.80 to +2.20** (-5% to -22% from +2.30)

### 4.3 Cost sensitivity

Phase 3a-revised baseline @ +0.5× shift: **+0.50**

Expected: minimal change. Lower-vol entries have proportionally smaller spreads, so cost sensitivity should be similar.

---

## 5. Success / Failure criteria (locked, no negotiation post-test)

**SUCCESS** (claim H1 supported, lower vix_entry_min improves IC):
- wf_coverage improves by >= 10pp from baseline (Slice B baseline run, NOT Phase 3a-revised's 62%)
- AND mean Sharpe degrades by less than 30% from baseline

**PARTIAL SUCCESS** (mixed signal, do not deploy without further validation):
- wf_coverage improves by 5-10pp AND Sharpe holds within 30%
- → Not sufficient for inclusion in FINAL config; revisit in Phase 3b' research

**FAILURE** (claim H0 cannot be rejected):
- wf_coverage improvement < 5pp, OR
- Mean Sharpe drops > 30%
- → Lower vix_entry_min is NOT included in FINAL config; baseline 16.0 remains

---

## 6. Why we test on Slice B specifically

Slice B = Oct 1 – Nov 30 2025 (43 trading days).

**Expected VIX environment in Slice B:**
- Late 2025 generally lower-vol post-summer-spike regime
- Likely contains both VIX 14-16 days (where the change matters) and VIX 16+ days (where it doesn't)
- Independent of Slice A (Aug-Sep) which contains different macro events
- Independent of FINAL (Jan-Feb 2026) which preserves the largest unburned window

This is appropriate sample for a hypothesis whose effect is concentrated in low-VIX days.

---

## 7. What this hypothesis does NOT address

1. **wf_decay = 1.20 (overfitting signal):** Lower vix_entry_min doesn't address train-vs-test divergence. That requires harness-level changes (shorter WF train, rolling recalibration).

2. **Capacity scaling at scale:** Slippage curve is independent of entry threshold. Need capacity-aware sizing as separate hypothesis.

3. **Apr 7 type macro shocks:** These hit IC regardless of entry threshold. Would need a fundamentally different risk-sizing approach.

---

## 8. Rollback / deployment decision

**If H1 supported in Slice B test:**
- The winning IC configuration becomes: `vix_entry_min=14.0, intraday_vix_spike_enabled=<Slice A result>`
- Confirm on FINAL slice as a single combined run (per holdout_allocation.md §7)
- If FINAL passes → deploy live
- If FINAL fails → do NOT deploy. The combined config didn't validate.

**If H1 rejected in Slice B test:**
- Keep `vix_entry_min=16.0` (unchanged)
- IC's wf_coverage failure must be addressed by a different mechanism (e.g., wf-decay-targeted harness change, or accept the 62% as deployment-time risk)

---

*Pre-registered 2026-04-27. Slice B has not been accessed as of writing. Per discipline §VII.7, this hypothesis cannot be re-tested with different threshold values on Slice B — one shot only. To test vix_entry_min = 13.0 or 15.0, use a different unburned slice (C or FINAL, with appropriate cost).*
