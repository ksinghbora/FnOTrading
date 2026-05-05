# Validation Report — iron_condor

- Run ID: `iron_condor_20260502_2211`
- Split train_end: 2025-05-30
- Split val_end: 2025-07-31
- Split holdout_end: 2026-02-27

## 1. Executive Summary

| Gate | Status | Reason |
|---|---|---|
| wf_test_sharpe_mean | PASS | mean_test_sharpe=0.728 (>=0.3) |
| wf_decay | PASS | median_decay=-0.870 (<1.0) |
| wf_coverage | FAIL | fraction_positive_test=0.40 (>=0.55) |
| wf_test_sharpe_p25 | FAIL | p25_test_sharpe=-3.640 (>=-0.5; n_windows=5) |
| cpcv_fold_stability_diagnostic | PASS | median=0.000 p05=0.000 (diagnostic only — never gating; WF is the primary OOS verdict) |
| regime | PASS | all regimes within bounds |
| cost_sensitivity | FAIL | sharpe@+0.5 shift = 0.000 (>0) |
| capacity | FAIL | pnl_per_lot declines up to 300 lots: [-20.8, -21.4, -22.0] |
| mc_skill_pvalue | FAIL | p=0.9633 (<0.1; 10000 stationary-bootstrap perms, ~15d blocks) |
| bootstrap_sharpe_ci | FAIL | 90%-CI=[-2.762, +0.000] (lower>-0.1) |

**Final Verdict: FAIL**

## 2. Split

- Train window: ends 2025-05-30 (129 days)
- Val window: 2025-05-30 → 2025-07-31 (44 days)
- Holdout window: 2025-07-31 → 2026-02-27 (0 days)

## 3. Walk-Forward (Primary OOS Verdict)

- Windows: 5 | median_decay=-0.870 | frac_positive=0.40 | mean_test_sharpe=0.728

| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |
|---|---|---|---|---|---|---|
| 0 | 2024-11-21..2025-02-13 | 2025-02-17..2025-03-18 | -3.050 | 3.640 | -6.690 | 8 |
| 1 | 2024-12-19..2025-03-17 | 2025-03-19..2025-04-21 | -2.070 | -3.640 | 1.570 | 8 |
| 2 | 2025-01-17..2025-04-17 | 2025-04-22..2025-05-20 | -0.870 | 0.000 | -0.870 | 0 |
| 3 | 2025-02-14..2025-05-19 | 2025-05-21..2025-06-17 | -2.070 | 3.640 | -5.710 | 8 |
| 4 | 2025-03-18..2025-06-16 | 2025-06-18..2025-07-15 | 0.000 | 0.000 | 0.000 | 0 |

## 4. CPCV (skipped via --skip-cpcv)

- Evaluation mode: `train_in_sample` — skipped — diagnostic-only after May 2 2026 refactor

## 5. Regime Stratification

| regime | num_trades | total_pnl | sharpe | win_rate | max_dd | passed |
|---|---|---|---|---|---|---|
| high_vix | 2 | -2520.0 | -290.1224 | 0.0 | -1211.25 | PASS |
| mid_vix | 1 | -1698.75 | 0.0 | 0.0 | 0.0 | PASS |
| low_vix | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| expiry_week | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| event_day | 1 | -1308.75 | 0.0 | 0.0 | 0.0 | PASS |
| trending | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| range_bound | 3 | -4218.75 | -86.5385 | 0.0 | -2910.0 | PASS |

## 6. Cost Sensitivity

| shift | sharpe | profit_factor | total_pnl |
|---|---|---|---|
| -1.00 | 0.000 | 0.78 | -1318.12 |
| -0.50 | 0.000 | 0.77 | -1377.19 |
| -0.25 | 0.000 | 0.77 | -1406.72 |
| +0.00 | 0.000 | 0.76 | -1436.25 |
| +0.25 | 0.000 | 0.76 | -1465.78 |
| +0.50 | 0.000 | 0.76 | -1495.31 |
| +1.00 | 0.000 | 0.75 | -1554.37 |

## 7. Capacity

| lot_size | total_pnl | pnl_per_lot | avg_slippage_bps | synth_fallback_legs |
|---|---|---|---|---|
| 75 | -1560.0 | -20.8 | 43.51 | 0 |
| 150 | -3210.0 | -21.4 | 61.0 | 0 |
| 300 | -6600.0 | -22.0 | 78.49 | 0 |
| 750 | -16770.0 | -22.36 | 88.99 | 0 |
| 1500 | -33720.0 | -22.48 | 92.49 | 0 |

```
   75 |   -20.8000 -------------------------------------
  150 |   -21.4000 --------------------------------------
  300 |   -22.0000 ---------------------------------------
  750 |   -22.3600 ----------------------------------------
 1500 |   -22.4800 ----------------------------------------
```

## 8. Holdout

NOT ACCESSED (holdout preserved)

