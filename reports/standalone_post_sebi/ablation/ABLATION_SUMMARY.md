# IC Strong-Signal Filter Ablation — Summary

May 2 2026. Each row drops ONE filter from the strong-signal IC config back to its default value, runs a 5-window walk-forward (CPCV skipped), and compares WF mean_test_sharpe to the baseline. Methodology = WF-primary (post-May-2 refactor); CPCV demoted to diagnostic-only.

## Results

| Config | WF mean_test_sharpe | Δ vs baseline | frac_positive | median_decay | trades | verdict |
|---|---|---|---|---|---|---|
| Baseline (all 6 filters tightened) | +2.184 | — | 0.60 | -4.930 | 28 | FAIL |
| Drop score 85→60 | -0.414 | -2.598 | 0.20 | 0.950 | 288 | FAIL |
| Drop PCR 0.85-1.20→0.70-1.50 | -0.174 | -2.358 | 0.40 | 2.300 | 28 | FAIL |
| Drop max_pain 1.5%→3.0% | +2.184 | +0.000 | 0.60 | -4.930 | 28 | FAIL |
| Drop adj 85→60 | +2.184 | +0.000 | 0.60 | -4.670 | 36 | FAIL |
| Drop intraday VIX spike filter | +2.184 | +0.000 | 0.60 | -4.930 | 28 | FAIL |
| Drop vol-scaled exits | +0.728 | -1.456 | 0.40 | -0.870 | 36 | FAIL |
| Minimal (drop ALL 3 noise filters together) | +2.184 | +0.000 | 0.60 | -4.670 | 36 | FAIL |

## Decision Rule

* **|Δ| ≤ 0.10** → filter is NOISE — safe to drop without loss
* **−0.20 ≤ Δ < −0.10** → filter is MARGINAL — borderline, keep on inertia
* **Δ < −0.20** → filter is REAL SIGNAL — keep it; dropping degrades generalization
* **Δ > +0.10** → filter HURTS performance — actively drop it (rare but possible)

## Per-window Test Sharpe Distribution

| Config | Window 0 | Window 1 | Window 2 | Window 3 | Window 4 |
|---|---|---|---|---|---|
| Baseline (all 6 filters tightened) | +3.64 | +3.64 | +0.00 | +3.64 | +0.00 |
| Drop score 85→60 | -0.43 | +6.54 | -0.09 | -2.86 | -5.23 |
| Drop PCR 0.85-1.20→0.70-1.50 | +3.64 | +3.64 | +0.00 | -4.51 | -3.64 |
| Drop max_pain 1.5%→3.0% | +3.64 | +3.64 | +0.00 | +3.64 | +0.00 |
| Drop adj 85→60 | +3.64 | +3.64 | +0.00 | +3.64 | +0.00 |
| Drop intraday VIX spike filter | +3.64 | +3.64 | +0.00 | +3.64 | +0.00 |
| Drop vol-scaled exits | +3.64 | -3.64 | +0.00 | +3.64 | +0.00 |
| Minimal (drop ALL 3 noise filters together) | +3.64 | +3.64 | +0.00 | +3.64 | +0.00 |

## Recommendation

Filters where dropping them leaves WF mean_test_sharpe materially unchanged are noise — drop them and re-evaluate the simpler config. Filters where dropping them degrades WF significantly are pulling real weight — keep them.

The simplest config that matches baseline WF performance is the right config — fewer parameters = less curve-fit risk = more likely to hold up out of sample.