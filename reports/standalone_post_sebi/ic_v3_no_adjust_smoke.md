# Validation Report — iron_condor

- Run ID: `iron_condor_20260506_1343`
- Split train_end: 2025-05-30
- Split val_end: 2025-07-31
- Split holdout_end: 2026-02-27

## 1. Executive Summary

| Gate | Status | Reason |
|---|---|---|
| wf_test_sharpe_mean | FAIL | mean_test_sharpe=0.000 (>=0.3) |
| wf_decay | FAIL | median_decay=2.810 (<1.0) |
| wf_coverage | FAIL | fraction_positive_test=0.00 (>=0.55) |
| wf_test_sharpe_p25 | PASS | p25_test_sharpe=0.000 (>=-0.5; n_windows=5) |
| cpcv_fold_stability_diagnostic | PASS | median=0.000 p05=0.000 (diagnostic only — never gating; WF is the primary OOS verdict) |
| regime | PASS | all regimes within bounds |
| cost_sensitivity | FAIL | sharpe@+0.5 shift = 0.000 (>0) |
| capacity | FAIL | pnl_per_lot declines up to 300 lots: [55.85, 51.7167, 47.5833] |
| mc_skill_pvalue | PASS | p=0.0735 (<0.1; 10000 stationary-bootstrap perms, ~15d blocks) |
| bootstrap_sharpe_ci | FAIL | 90%-CI=[-0.223, +3.929] (lower>-0.1) |

**Final Verdict: FAIL**

## 2. Split

- Train window: ends 2025-05-30 (129 days)
- Val window: 2025-05-30 → 2025-07-31 (44 days)
- Holdout window: 2025-07-31 → 2026-02-27 (0 days)

## 3. Walk-Forward (Primary OOS Verdict)

- Windows: 5 | median_decay=2.810 | frac_positive=0.00 | mean_test_sharpe=0.000

| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |
|---|---|---|---|---|---|---|
| 0 | 2024-11-21..2025-02-13 | 2025-02-17..2025-03-18 | -0.180 | 0.000 | -0.180 | 0 |
| 1 | 2024-12-19..2025-03-17 | 2025-03-19..2025-04-21 | 2.810 | 0.000 | 2.810 | 0 |
| 2 | 2025-01-17..2025-04-17 | 2025-04-22..2025-05-20 | 4.690 | 0.000 | 4.690 | 0 |
| 3 | 2025-02-14..2025-05-19 | 2025-05-21..2025-06-17 | 3.260 | 0.000 | 3.260 | 0 |
| 4 | 2025-03-18..2025-06-16 | 2025-06-18..2025-07-15 | -0.150 | 0.000 | -0.150 | 0 |

## 4. CPCV (skipped via --skip-cpcv)

- Evaluation mode: `train_in_sample` — skipped — diagnostic-only after May 2 2026 refactor

## 5. Regime Stratification

| regime | num_trades | total_pnl | sharpe | win_rate | max_dd | passed |
|---|---|---|---|---|---|---|
| high_vix | 10 | 851.25 | 1.931 | 70.0 | -322.5 | PASS |
| mid_vix | 10 | 543.75 | 1.0402 | 80.0 | -2591.25 | PASS |
| low_vix | 11 | 2793.75 | 11.4553 | 81.82 | -615.0 | PASS |
| expiry_week | 7 | 461.25 | 2.0421 | 71.43 | -195.0 | PASS |
| event_day | 4 | 540.0 | 9.1936 | 75.0 | 0.0 | PASS |
| trending | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| range_bound | 31 | 4188.75 | 3.379 | 77.42 | -1751.25 | PASS |

## 6. Cost Sensitivity

| shift | sharpe | profit_factor | total_pnl |
|---|---|---|---|
| -1.00 | 0.000 | 1.34 | 5630.62 |
| -0.50 | 0.000 | 1.31 | 5280.94 |
| -0.25 | 0.000 | 1.3 | 5106.09 |
| +0.00 | 0.000 | 1.29 | 4931.25 |
| +0.25 | 0.000 | 1.28 | 4756.41 |
| +0.50 | 0.000 | 1.27 | 4581.56 |
| +1.00 | 0.000 | 1.24 | 4231.88 |

## 7. Capacity

| lot_size | total_pnl | pnl_per_lot | avg_slippage_bps | synth_fallback_legs |
|---|---|---|---|---|
| 75 | 4188.75 | 55.85 | 52.76 | 0 |
| 150 | 7757.5 | 51.7167 | 80.05 | 0 |
| 300 | 14275.0 | 47.5833 | 107.33 | 0 |
| 750 | 33827.5 | 45.1033 | 123.7 | 0 |
| 1500 | 66415.0 | 44.2767 | 129.16 | 0 |

```
   75 |   +55.8500 ########################################
  150 |   +51.7167 #####################################
  300 |   +47.5833 ##################################
  750 |   +45.1033 ################################
 1500 |   +44.2767 ################################
```

## 8. Holdout

NOT ACCESSED (holdout preserved)

