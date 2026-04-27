# Phase 3b — Holdout Slice Allocation

**Created:** 2026-04-27
**Branch:** `FnO-v4-strategy-research`
**Status:** Pre-registration — slices unburned as of writing
**Discipline reference:** §VII.2 (single-access holdout), §VII.6 (one change per validation run), §VII.7 (no iterative tuning on holdout)

---

## 1. Why slice the holdout

Phase 3a-revised reserved a single 7-month holdout window (Aug 2025 – Feb 2026, ~150 trading days). Per discipline §VII.2 it's single-access — burning the whole window on one hypothesis test would consume the entire holdout.

Phase 3b research surfaced **multiple distinct hypotheses** that each need independent fresh-data validation:
- Gate B (intraday VIX spike filter)
- Lower vix_entry_min for IC (address Window 7 zero-trades)
- Capacity-aware position sizing (potential)
- Possibly more

Per López de Prado, testing N hypotheses on the same data without correction inflates false-positive rate. The clean answer: **partition the holdout into independent slices, each used at most once.**

---

## 2. The allocation

| Slice | Period | Trading days | Purpose | Status |
|---|---|---|---|---|
| **A** | Aug 1 – Sep 30 2025 | ~44 | Gate B (intraday VIX spike) test | Reserved |
| **B** | Oct 1 – Nov 30 2025 | ~43 | Lower vix_entry_min test | Reserved |
| **C** | Dec 1 – Dec 31 2025 | ~22 | Reserve for ONE more pre-registered hypothesis (e.g., capacity sizing) | Reserved |
| **FINAL** | Jan 1 – Feb 27 2026 | ~42 | UNTOUCHABLE — final validation of winning combination | Sealed until last |

**Total: ~151 trading days, fully partitioned.**

---

## 3. Discipline rules per slice

1. **Single-shot per slice.** Each slice may be used for one validation run only. No threshold tuning, no parameter sweeps.

2. **Slice consumption is irreversible.** Once a slice is used (regardless of outcome), it's burned. Cannot be reused for any future hypothesis.

3. **FINAL slice is sealed.** No access to FINAL until either:
   - All test slices (A, B, C) have been used and a winning combination identified
   - OR a definitive deployment decision needs FINAL as the conclusive proof
   - The FINAL run uses the BEST configuration found across A/B/C, applied as a single config — no per-test cherry-picking

4. **Slice C is held for proven need.** Don't burn slice C on a low-confidence hypothesis. If the first two tests both pass, C may not be needed.

5. **All hypotheses must be pre-registered BEFORE accessing the slice.** Pre-registration requires:
   - Specific parameter values (no ranges)
   - Expected outcome direction
   - Success and failure criteria
   - Reasoning grounded in prior validation data (NOT this slice's data)

---

## 4. Slice-A test plan: Gate B (intraday VIX spike)

**Pre-registered in:** [regime_gate_proposal.md](regime_gate_proposal.md) §4 Gate B
**Implementation:** commit `7ecb81b` — `intraday_vix_spike_enabled=True` opt-in via params
**Strategy:** `iron_condor`
**Slice:** A (Aug 1 – Sep 30 2025)

**Run command:**
```bash
# Two runs back-to-back: with vs without Gate B, to isolate effect.
# Both use Slice A only.

# Baseline (no Gate B):
uv run python scripts/validate_strategy.py \
  --strategy iron_condor \
  --train-end 2025-09-15 --val-end 2025-09-30 --holdout-end 2025-09-30 \
  --parquet-dir data/gdfl_snapshots --underlying NIFTY \
  --workers 10 --cpcv-folds 8 --cpcv-n-test-folds 2 --cpcv-max-paths 30 \
  --wf-train-days 30 --wf-test-days 14 --wf-step-days 7 \
  --out reports/phase3b_holdout/sliceA_ic_baseline.md

# Treatment (Gate B enabled):
echo '{"intraday_vix_spike_enabled": true}' > /tmp/sliceA_gate_b.json
uv run python scripts/validate_strategy.py \
  --strategy iron_condor \
  --train-end 2025-09-15 --val-end 2025-09-30 --holdout-end 2025-09-30 \
  --parquet-dir data/gdfl_snapshots --underlying NIFTY \
  --workers 10 --cpcv-folds 8 --cpcv-n-test-folds 2 --cpcv-max-paths 30 \
  --wf-train-days 30 --wf-test-days 14 --wf-step-days 7 \
  --baseline-params-json /tmp/sliceA_gate_b.json \
  --out reports/phase3b_holdout/sliceA_ic_gate_b.md
```

**Success criterion:** Gate B run improves wf_coverage AND/OR Sharpe by a non-trivial margin vs baseline. Specifically:
- wf_coverage delta >= +5pp, OR
- Mean Sharpe delta >= +0.3

**Failure criterion:** No directional improvement in either metric, or both worsen.

**Slice A is consumed regardless of outcome.**

---

## 5. Slice-B test plan: Lower vix_entry_min

**Pre-registered in:** [lower_vix_entry_min_proposal.md](lower_vix_entry_min_proposal.md)
**Implementation:** No code change needed — runtime param override
**Strategy:** `iron_condor`
**Slice:** B (Oct 1 – Nov 30 2025)

**Run command:**
```bash
# Baseline (vix_entry_min=16, default):
uv run python scripts/validate_strategy.py \
  --strategy iron_condor \
  --train-end 2025-11-15 --val-end 2025-11-30 --holdout-end 2025-11-30 \
  --parquet-dir data/gdfl_snapshots --underlying NIFTY \
  --workers 10 --cpcv-folds 8 --cpcv-n-test-folds 2 --cpcv-max-paths 30 \
  --wf-train-days 30 --wf-test-days 14 --wf-step-days 7 \
  --out reports/phase3b_holdout/sliceB_ic_baseline.md

# Treatment (vix_entry_min=14):
echo '{"vix_entry_min": 14.0}' > /tmp/sliceB_lower_vix.json
uv run python scripts/validate_strategy.py \
  --strategy iron_condor \
  --train-end 2025-11-15 --val-end 2025-11-30 --holdout-end 2025-11-30 \
  --parquet-dir data/gdfl_snapshots --underlying NIFTY \
  --workers 10 --cpcv-folds 8 --cpcv-n-test-folds 2 --cpcv-max-paths 30 \
  --wf-train-days 30 --wf-test-days 14 --wf-step-days 7 \
  --baseline-params-json /tmp/sliceB_lower_vix.json \
  --out reports/phase3b_holdout/sliceB_ic_lower_vix.md
```

**Success criterion:** Lower vix_entry_min run improves wf_coverage materially:
- wf_coverage delta >= +10pp (Window 7 zero-trades effect should be large), AND
- Mean Sharpe delta does NOT drop more than 30%

**Failure criterion:** wf_coverage doesn't improve OR mean Sharpe drops > 30%.

**Slice B is consumed regardless of outcome.**

---

## 6. Slice-C: Reserved for one more pre-registered hypothesis

**Status:** UNALLOCATED. Will be pre-registered if A/B results suggest a clear next hypothesis worth testing.

**Likely candidates** (no commitment yet):
- Capacity-aware position sizing implementation
- Tighter entry-time window (e.g., 9:20-11:00 only)
- Adaptive wing width based on VIX
- Combined Gate B + lower vix_entry_min (if both A/B pass individually, want to confirm they stack)

**Decision criteria:**
- IF A passes AND B passes → C may test combined config
- IF only one passes → C tests the next-most-promising standalone hypothesis
- IF both fail → STOP. Don't burn C on speculation. Revisit Phase 3a roster.

---

## 7. FINAL slice (Jan 1 – Feb 27 2026)

**Status:** SEALED. Untouchable until decisive moment.

**Use criteria:**
- Final validation of the winning configuration before live capital
- Single combined config — Gate B + lower vix_entry_min + (optionally) C's outcome
- Single run, no comparison to "baseline" — this slice is the live-capital go/no-go

**Failure of FINAL:** the strategy does not deploy live, period. No second-chance tests.

---

## 8. What this allocation does NOT cover

The following Phase 3b research items would require the FINAL slice or a NEW data acquisition:

1. **wf_decay = 1.20 fix.** Requires harness changes (shorter WF train window, rolling re-calibration). Should be tested on a sandbox window outside this allocation, then confirmed on FINAL only if it shows promise.

2. **Capacity scaling on real-money simulation.** GDFL parquet bid/ask is shown to match live broker fills (per cost_model_audit.md). But true-live deployment will reveal scaling effects this corpus can't predict.

3. **Cross-strategy interactions.** If iron_condor deploys, does running it alongside the existing portfolio_strategy cause regime conflicts? Not addressable from single-strategy backtest.

These are post-deployment learning problems, not pre-deployment validation problems.

---

*Allocation locked 2026-04-27. To modify, requires explicit operator approval and documented reasoning in the decision log of `docs/PROJECT_STATE.md`.*
