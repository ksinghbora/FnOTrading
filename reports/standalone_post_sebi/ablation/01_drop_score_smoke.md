# Validation Report — iron_condor

- Run ID: `iron_condor_20260502_1755`
- Split train_end: 2025-05-30
- Split val_end: 2025-07-31
- Split holdout_end: 2026-02-27

## 1. Executive Summary

| Gate | Status | Reason |
|---|---|---|
| wf_test_sharpe_mean | FAIL | mean_test_sharpe=-0.414 (>=0.3) |
| wf_decay | PASS | median_decay=0.950 (<1.0) |
| wf_coverage | FAIL | fraction_positive_test=0.20 (>=0.55) |
| wf_test_sharpe_p25 | FAIL | p25_test_sharpe=-5.230 (>=-0.5; n_windows=5) |
| cpcv_fold_stability_diagnostic | PASS | median=0.000 p05=0.000 (diagnostic only — never gating; WF is the primary OOS verdict) |
| regime | FAIL | failing: high_vix(sharpe=-5.19, n=21), range_bound(sharpe=-4.32, n=34) |
| cost_sensitivity | FAIL | sharpe@+0.5 shift = 0.000 (>0) |
| capacity | FAIL | pnl_per_lot declines up to 300 lots: [-49.9, -54.7, -59.5] |
| mc_skill_pvalue | FAIL | p=0.8280 (<0.1; 10000 stationary-bootstrap perms, ~15d blocks) |
| bootstrap_sharpe_ci | FAIL | 90%-CI=[-2.599, +0.955] (lower>-0.1) |

**Final Verdict: FAIL**

## 2. Split

- Train window: ends 2025-05-30 (129 days)
- Val window: 2025-05-30 → 2025-07-31 (44 days)
- Holdout window: 2025-07-31 → 2026-02-27 (0 days)

## 3. Walk-Forward (Primary OOS Verdict)

- Windows: 5 | median_decay=0.950 | frac_positive=0.20 | mean_test_sharpe=-0.414

| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |
|---|---|---|---|---|---|---|
| 0 | 2024-11-21..2025-02-13 | 2025-02-17..2025-03-18 | -1.880 | -0.430 | -1.450 | 16 |
| 1 | 2024-12-19..2025-03-17 | 2025-03-19..2025-04-21 | -0.390 | 6.540 | -6.930 | 24 |
| 2 | 2025-01-17..2025-04-17 | 2025-04-22..2025-05-20 | 0.860 | -0.090 | 0.950 | 24 |
| 3 | 2025-02-14..2025-05-19 | 2025-05-21..2025-06-17 | 3.710 | -2.860 | 6.570 | 40 |
| 4 | 2025-03-18..2025-06-16 | 2025-06-18..2025-07-15 | 0.120 | -5.230 | 5.350 | 16 |

## 4. CPCV (skipped via --skip-cpcv)

- Evaluation mode: `train_in_sample` — skipped — diagnostic-only after May 2 2026 refactor

## 5. Regime Stratification

| regime | num_trades | total_pnl | sharpe | win_rate | max_dd | passed |
|---|---|---|---|---|---|---|
| **high_vix** | **21** | **-4395.0** | **-5.1894** | **47.62** | **-4417.5** | **FAIL** |
| mid_vix | 13 | -1698.75 | -2.922 | 61.54 | -2265.0 | PASS |
| low_vix | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| expiry_week | 8 | -2235.0 | -10.597 | 37.5 | -2118.75 | PASS |
| event_day | 3 | -1451.25 | -10.2119 | 33.33 | -1822.5 | PASS |
| trending | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| **range_bound** | **34** | **-6093.75** | **-4.3229** | **52.94** | **-5797.5** | **FAIL** |

## 6. Cost Sensitivity

| shift | sharpe | profit_factor | total_pnl |
|---|---|---|---|
| -1.00 | 0.000 | 0.93 | -1850.63 |
| -0.50 | 0.000 | 0.91 | -2314.69 |
| -0.25 | 0.000 | 0.9 | -2546.72 |
| +0.00 | 0.000 | 0.89 | -2778.75 |
| +0.25 | 0.000 | 0.88 | -3010.78 |
| +0.50 | 0.000 | 0.87 | -3242.81 |
| +1.00 | 0.000 | 0.86 | -3706.87 |

## 7. Capacity

| lot_size | total_pnl | pnl_per_lot | avg_slippage_bps | synth_fallback_legs |
|---|---|---|---|---|
| 75 | -3742.5 | -49.9 | 43.13 | 0 |
| 150 | -8205.0 | -54.7 | 63.0 | 0 |
| 300 | -17850.0 | -59.5 | 82.86 | 0 |
| 750 | -46785.0 | -62.38 | 94.78 | 0 |
| 1500 | -95010.0 | -63.34 | 98.75 | 0 |

```
   75 |   -49.9000 --------------------------------
  150 |   -54.7000 -----------------------------------
  300 |   -59.5000 --------------------------------------
  750 |   -62.3800 ---------------------------------------
 1500 |   -63.3400 ----------------------------------------
```

## 8. Holdout

NOT ACCESSED (holdout preserved)

