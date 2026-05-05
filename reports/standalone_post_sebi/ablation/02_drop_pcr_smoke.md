# Validation Report — iron_condor

- Run ID: `iron_condor_20260502_1846`
- Split train_end: 2025-05-30
- Split val_end: 2025-07-31
- Split holdout_end: 2026-02-27

## 1. Executive Summary

| Gate | Status | Reason |
|---|---|---|
| wf_test_sharpe_mean | FAIL | mean_test_sharpe=-0.174 (>=0.3) |
| wf_decay | FAIL | median_decay=2.300 (<1.0) |
| wf_coverage | FAIL | fraction_positive_test=0.40 (>=0.55) |
| wf_test_sharpe_p25 | FAIL | p25_test_sharpe=-4.510 (>=-0.5; n_windows=5) |
| cpcv_fold_stability_diagnostic | PASS | median=0.000 p05=0.000 (diagnostic only — never gating; WF is the primary OOS verdict) |
| regime | PASS | all regimes within bounds |
| cost_sensitivity | FAIL | sharpe@+0.5 shift = 0.000 (>0) |
| capacity | FAIL | pnl_per_lot declines up to 300 lots: [-6.65, -7.1167, -7.5833] |
| mc_skill_pvalue | FAIL | p=0.7986 (<0.1; 10000 stationary-bootstrap perms, ~15d blocks) |
| bootstrap_sharpe_ci | FAIL | 90%-CI=[-1.686, +1.207] (lower>-0.1) |

**Final Verdict: FAIL**

## 2. Split

- Train window: ends 2025-05-30 (129 days)
- Val window: 2025-05-30 → 2025-07-31 (44 days)
- Holdout window: 2025-07-31 → 2026-02-27 (0 days)

## 3. Walk-Forward (Primary OOS Verdict)

- Windows: 5 | median_decay=2.300 | frac_positive=0.40 | mean_test_sharpe=-0.174

| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |
|---|---|---|---|---|---|---|
| 0 | 2024-11-21..2025-02-13 | 2025-02-17..2025-03-18 | -1.120 | 3.640 | -4.760 | 8 |
| 1 | 2024-12-19..2025-03-17 | 2025-03-19..2025-04-21 | -2.890 | 3.640 | -6.530 | 8 |
| 2 | 2025-01-17..2025-04-17 | 2025-04-22..2025-05-20 | 2.300 | 0.000 | 2.300 | 0 |
| 3 | 2025-02-14..2025-05-19 | 2025-05-21..2025-06-17 | -2.070 | -4.510 | 2.440 | 24 |
| 4 | 2025-03-18..2025-06-16 | 2025-06-18..2025-07-15 | 0.000 | -3.640 | 3.640 | 8 |

## 4. CPCV (skipped via --skip-cpcv)

- Evaluation mode: `train_in_sample` — skipped — diagnostic-only after May 2 2026 refactor

## 5. Regime Stratification

| regime | num_trades | total_pnl | sharpe | win_rate | max_dd | passed |
|---|---|---|---|---|---|---|
| high_vix | 2 | -300.0 | -2.9539 | 50.0 | -720.0 | PASS |
| mid_vix | 1 | -986.25 | 0.0 | 0.0 | 0.0 | PASS |
| low_vix | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| expiry_week | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| event_day | 1 | 420.0 | 0.0 | 100.0 | 0.0 | PASS |
| trending | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| range_bound | 3 | -1286.25 | -9.1114 | 33.33 | -1706.25 | PASS |

## 6. Cost Sensitivity

| shift | sharpe | profit_factor | total_pnl |
|---|---|---|---|
| -1.00 | 0.000 | 0.89 | -318.75 |
| -0.50 | 0.000 | 0.87 | -361.88 |
| -0.25 | 0.000 | 0.87 | -383.44 |
| +0.00 | 0.000 | 0.86 | -405.0 |
| +0.25 | 0.000 | 0.85 | -426.56 |
| +0.50 | 0.000 | 0.84 | -448.12 |
| +1.00 | 0.000 | 0.83 | -491.25 |

## 7. Capacity

| lot_size | total_pnl | pnl_per_lot | avg_slippage_bps | synth_fallback_legs |
|---|---|---|---|---|
| 75 | -498.75 | -6.65 | 40.62 | 0 |
| 150 | -1067.5 | -7.1167 | 55.93 | 0 |
| 300 | -2275.0 | -7.5833 | 71.24 | 0 |
| 750 | -5897.5 | -7.8633 | 80.42 | 0 |
| 1500 | -11935.0 | -7.9567 | 83.49 | 0 |

```
   75 |    -6.6500 ---------------------------------
  150 |    -7.1167 ------------------------------------
  300 |    -7.5833 --------------------------------------
  750 |    -7.8633 ----------------------------------------
 1500 |    -7.9567 ----------------------------------------
```

## 8. Holdout

NOT ACCESSED (holdout preserved)

