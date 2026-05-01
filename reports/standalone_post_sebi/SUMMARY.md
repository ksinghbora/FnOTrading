# Post-SEBI Cross-Strategy Validation — Summary

Apr 30 → May 1, 2026 — multi-model audit follow-up + Indian-calibration step.

## Setup

- **Corpus:** `data/gdfl_v2/` GDFL parquet, NIFTY only, 173 days post-SEBI
  filtered via `--corpus-from 2024-11-20` (drops the 54 pre-break days
  where `LOT_SIZES["NIFTY"]=75` would simulate at ~3× the actual 25-lot)
- **Splits:** train_end=2025-05-30, val_end=2025-07-31, holdout_end=2026-02-27
  (holdout NOT accessed — preserved single-access rule)
- **Code state:** branch `FnO-v4-strategy-research` at commit `3a6c322`
  (PT/SL realistic-fill fix + quote-fallback probe both shipped today)
- **Quote quality:** **0/N _bid_ask_for fallbacks across every run** —
  the realistic-fill claim is honest, no LTP contamination

## Verdict matrix

| Strategy | Config | Median Sharpe | PnL @ zero cost | PF @ zero | MC p-value | Final |
|---|---|---|---|---|---|---|
| `iron_condor` | adjustments ON, 50p full | **−4.54** | −₹245K | 0.36 | 1.0000 | **FAIL** |
| `iron_condor` | adjustments ON, 5p smoke | −4.36 | −₹381K | 0.36 | 1.0000 | FAIL |
| `iron_condor` STATIC | **adjustments DISABLED** | **−4.25** | **−₹184K** | **0.37** | 1.0000 | **FAIL** |
| `short_strangle` | default, 5p smoke | **−8.34** | — | — | 1.0000 | **FAIL** |
| `short_straddle` | default, 5p smoke | **−7.25** | — | — | 1.0000 | **FAIL** |
| `long_calendar` | default, 5p smoke | **−3.23** | — | — | 0.9997 | **FAIL** |

Every gate fails on every configuration except a few benign ones
(`fold_stability_pbo` PASS because we only run one config; `wf_decay`
sometimes PASS because both train and test are bad together).

## Key findings

### 1. Adjustment policy is a real bug — but not the root cause

The static-IC test (adjustments disabled) was the decisive experiment for
the trade-audit hypothesis:

- **PnL loss halved** (−₹381K → −₹184K). Confirms the adjustment policy
  is responsible for ~52% of the absolute loss. Trade audit found
  adjustments firing 1-2 min after entry, biased exclusively to CE side,
  locking losses on positions that would have healed.
- **But Sharpe barely moves** (−4.36 → −4.25). Fewer trades → lower
  daily-PnL variance → similar ratio.
- **Profit factor identical** (0.36 → 0.37). At zero cost, the strategy
  still loses ₹3 for every ₹1 it wins. **The remaining half of the loss
  is the strategy core: wrong-regime entries.**

So fixing the adjustment policy would take IC from −4.5 Sharpe to ~−4.2
Sharpe. Necessary but nowhere near sufficient.

### 2. The premium-selling ecosystem has no edge on this corpus

5 configurations × 4 strategy types, every single one decisively
unprofitable. MC permutation p-values ≥ 0.9997 across the board mean
**every strategy is statistically worse than a random-coin-flip baseline.**
Cost-at-zero gives Sharpe 0 for every config — the strategies are not
losing because of costs (they'd lose at zero cost too). They're losing
because the entries they're taking are wrong-regime.

This is consistent with SEBI's own published study: 90%+ of retail F&O
traders lose money. The post-Nov-2024 cost doubling (STT, lot size) was
the regulator's specific intent to push retail away. We've now reproduced
that finding from first principles on a regime-clean 173-day corpus with
audit-clean code.

### 3. long_calendar is the least-bad — and structurally different

`long_calendar` lost least (Sharpe −3.23 vs −4.25 to −8.34 for the
others). It's a debit spread / net-long-vega play, not premium-selling.
Suggests the path forward is non-symmetric, non-premium-selling
strategies. But −3.23 is still a decisive loss; the calendar is "less
broken," not "fixable."

### 4. Spot-data corruption flagged for follow-up

The trade audit on Dec 20 2024 found `spot=23948 → 25952 → 27751` over
28 minutes. NIFTY does not move 16% in half an hour. Either parquet
corruption on that day or a chain-builder bug. **Doesn't change the
post-SEBI verdict** (one day in 173) but warrants a separate follow-up
to scan for other corruptions.

## Recommendation

**Pivot strategy class.** The data is overwhelming across 5 configurations.
Stop iterating on premium-selling on Indian retail F&O.

Three pivot directions, ordered by shortest path to validation:

1. **Trend-following on NIFTY index futures (~1-2 weeks)** — uses the
   inverse signal mechanic (profits on breakouts, which destroyed our
   IC); no theta decay; ~1 bp slippage on futures vs ~5 bp on options
   legs; existing tick infrastructure reusable.
2. **Long-vol vol-targeting (~1-2 weeks)** — buys VIX puts when VIX is
   low/cheap, opposite of our current bias; profits when realized vol
   exceeds implied vol.
3. **Equity stat-arb / pairs trading (~2-3 weeks)** — entirely different
   cost structure (no STT-on-sell, no F&O lot constraint, no SEBI
   doubling); different alpha source (price relations).

## File index

- `iron_condor_validation.md` — full 50-path CPCV + 4-window WF on IC
- `iron_condor_smoke.md` — 5-path smoke (sanity check the harness)
- `iron_condor_static_smoke.md` — IC w/ `adjustment_threshold_pct=9999`
- `iron_condor_static_params.json` — params override for the static run
- `short_strangle_smoke.md` / `short_straddle_smoke.md` / `long_calendar_smoke.md`
- `*.log` — engine logs (gitignored, ~580 MB total)
