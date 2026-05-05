# Validation Report — iron_condor

- Run ID: `iron_condor_20260505_1856`
- Split train_end: 2025-05-30
- Split val_end: 2025-07-31
- Split holdout_end: 2026-02-27

## 1. Executive Summary

| Gate | Status | Reason |
|---|---|---|
| wf_test_sharpe_mean | PASS | mean_test_sharpe=2.184 (>=0.3) |
| wf_decay | PASS | median_decay=-4.670 (<1.0) |
| wf_coverage | PASS | fraction_positive_test=0.60 (>=0.55) |
| wf_test_sharpe_p25 | PASS | p25_test_sharpe=0.000 (>=-0.5; n_windows=5) |
| cpcv_fold_stability_diagnostic | PASS | median=0.000 p05=0.000 (diagnostic only — never gating; WF is the primary OOS verdict) |
| regime | PASS | all regimes within bounds |
| cost_sensitivity | FAIL | sharpe@+0.5 shift = 0.000 (>0) |
| capacity | FAIL | pnl_per_lot declines up to 300 lots: [-6.0, -6.6, -7.2] |
| mc_skill_pvalue | FAIL | p=0.7734 (<0.1; 10000 stationary-bootstrap perms, ~15d blocks) |
| bootstrap_sharpe_ci | FAIL | 90%-CI=[-1.272, +1.207] (lower>-0.1) |

**Final Verdict: FAIL**

## 2. Split

- Train window: ends 2025-05-30 (129 days)
- Val window: 2025-05-30 → 2025-07-31 (44 days)
- Holdout window: 2025-07-31 → 2026-02-27 (0 days)

## 3. Walk-Forward (Primary OOS Verdict)

- Windows: 5 | median_decay=-4.670 | frac_positive=0.60 | mean_test_sharpe=2.184

| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |
|---|---|---|---|---|---|---|
| 0 | 2024-11-21..2025-02-13 | 2025-02-17..2025-03-18 | -1.030 | 3.640 | -4.670 | 12 |
| 1 | 2024-12-19..2025-03-17 | 2025-03-19..2025-04-21 | -2.070 | 3.640 | -5.710 | 12 |
| 2 | 2025-01-17..2025-04-17 | 2025-04-22..2025-05-20 | 3.330 | 0.000 | 3.330 | 0 |
| 3 | 2025-02-14..2025-05-19 | 2025-05-21..2025-06-17 | -2.070 | 3.640 | -5.710 | 16 |
| 4 | 2025-03-18..2025-06-16 | 2025-06-18..2025-07-15 | 0.000 | 0.000 | 0.000 | 0 |

## 4. CPCV (skipped via --skip-cpcv)

- Evaluation mode: `train_in_sample` — skipped — diagnostic-only after May 2 2026 refactor

## 5. Regime Stratification

| regime | num_trades | total_pnl | sharpe | win_rate | max_dd | passed |
|---|---|---|---|---|---|---|
| high_vix | 2 | -720.0 | -11.225 | 50.0 | -720.0 | PASS |
| mid_vix | 1 | -892.5 | 0.0 | 0.0 | 0.0 | PASS |
| low_vix | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| expiry_week | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| event_day | 1 | 0.0 | 0.0 | 100.0 | 0.0 | PASS |
| trending | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| range_bound | 3 | -1612.5 | -18.0235 | 33.33 | -1612.5 | PASS |

## 6. Cost Sensitivity

| shift | sharpe | profit_factor | total_pnl |
|---|---|---|---|
| -1.00 | 0.000 | 0.93 | -206.25 |
| -0.50 | 0.000 | 0.92 | -264.37 |
| -0.25 | 0.000 | 0.91 | -293.44 |
| +0.00 | 0.000 | 0.9 | -322.5 |
| +0.25 | 0.000 | 0.89 | -351.56 |
| +0.50 | 0.000 | 0.88 | -380.62 |
| +1.00 | 0.000 | 0.87 | -438.75 |

## 7. Capacity

| lot_size | total_pnl | pnl_per_lot | avg_slippage_bps | synth_fallback_legs |
|---|---|---|---|---|
| 75 | -450.0 | -6.0 | 39.63 | 0 |
| 150 | -990.0 | -6.6 | 53.81 | 0 |
| 300 | -2160.0 | -7.2 | 67.99 | 0 |
| 750 | -5670.0 | -7.56 | 76.51 | 0 |
| 1500 | -11520.0 | -7.68 | 79.34 | 0 |

```
   75 |    -6.0000 -------------------------------
  150 |    -6.6000 ----------------------------------
  300 |    -7.2000 --------------------------------------
  750 |    -7.5600 ---------------------------------------
 1500 |    -7.6800 ----------------------------------------
```

## 8. Holdout

NOT ACCESSED (holdout preserved)

