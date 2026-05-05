# Validation Report — iron_condor

- Run ID: `iron_condor_20260502_1606`
- Split train_end: 2025-05-30
- Split val_end: 2025-07-31
- Split holdout_end: 2026-02-27

## 1. Executive Summary

| Gate | Status | Reason |
|---|---|---|
| wf_test_sharpe_mean | PASS | mean_test_sharpe=1.400 (>=0.3) |
| wf_decay | PASS | median_decay=-1.040 (<1.0) |
| wf_coverage | PASS | fraction_positive_test=0.60 (>=0.55) |
| wf_test_sharpe_p25 | FAIL | p25_test_sharpe=-4.180 (>=-0.5; n_windows=5) |
| cpcv_fold_stability_diagnostic | PASS | median=0.000 p05=0.000 (diagnostic only — never gating; WF is the primary OOS verdict) |
| regime | FAIL | failing: high_vix(sharpe=-4.72, n=31), range_bound(sharpe=-5.15, n=32) |
| cost_sensitivity | FAIL | sharpe@+0.5 shift = 0.000 (>0) |
| capacity | FAIL | pnl_per_lot declines up to 300 lots: [-18.2, -24.4667, -30.7333] |
| mc_skill_pvalue | FAIL | p=0.2468 (<0.1; 10000 stationary-bootstrap perms, ~15d blocks) |
| bootstrap_sharpe_ci | FAIL | 90%-CI=[-1.141, +3.791] (lower>-0.1) |

**Final Verdict: FAIL**

## 2. Split

- Train window: ends 2025-05-30 (129 days)
- Val window: 2025-05-30 → 2025-07-31 (44 days)
- Holdout window: 2025-07-31 → 2026-02-27 (0 days)

## 3. Walk-Forward (Primary OOS Verdict)

- Windows: 5 | median_decay=-1.040 | frac_positive=0.60 | mean_test_sharpe=1.400

| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |
|---|---|---|---|---|---|---|
| 0 | 2024-11-21..2025-02-13 | 2025-02-17..2025-03-18 | 2.600 | 3.640 | -1.040 | 16 |
| 1 | 2024-12-19..2025-03-17 | 2025-03-19..2025-04-21 | 4.710 | 5.250 | -0.540 | 28 |
| 2 | 2025-01-17..2025-04-17 | 2025-04-22..2025-05-20 | 4.650 | -4.180 | 8.830 | 72 |
| 3 | 2025-02-14..2025-05-19 | 2025-05-21..2025-06-17 | -0.920 | 2.290 | -3.210 | 64 |
| 4 | 2025-03-18..2025-06-16 | 2025-06-18..2025-07-15 | -1.090 | 0.000 | -1.090 | 0 |

## 4. CPCV (skipped via --skip-cpcv)

- Evaluation mode: `train_in_sample` — skipped — diagnostic-only after May 2 2026 refactor

## 5. Regime Stratification

| regime | num_trades | total_pnl | sharpe | win_rate | max_dd | passed |
|---|---|---|---|---|---|---|
| **high_vix** | **31** | **-11190.0** | **-4.7163** | **58.06** | **-13046.25** | **FAIL** |
| mid_vix | 1 | -986.25 | 0.0 | 0.0 | 0.0 | PASS |
| low_vix | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| expiry_week | 9 | -4635.0 | -6.3813 | 55.56 | -5392.5 | PASS |
| event_day | 8 | 1335.0 | 2.1802 | 75.0 | -3461.25 | PASS |
| trending | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| **range_bound** | **32** | **-12176.25** | **-5.1452** | **56.25** | **-14032.5** | **FAIL** |

## 6. Cost Sensitivity

| shift | sharpe | profit_factor | total_pnl |
|---|---|---|---|
| -1.00 | 0.000 | 1.25 | 8525.62 |
| -0.50 | 0.000 | 1.18 | 6057.19 |
| -0.25 | 0.000 | 1.14 | 4822.97 |
| +0.00 | 0.000 | 1.1 | 3588.75 |
| +0.25 | 0.000 | 1.07 | 2354.53 |
| +0.50 | 0.000 | 1.03 | 1120.31 |
| +1.00 | 0.000 | 0.97 | -1348.13 |

## 7. Capacity

| lot_size | total_pnl | pnl_per_lot | avg_slippage_bps | synth_fallback_legs |
|---|---|---|---|---|
| 75 | -1365.0 | -18.2 | 62.6 | 0 |
| 150 | -3670.0 | -24.4667 | 83.04 | 0 |
| 300 | -9220.0 | -30.7333 | 103.47 | 0 |
| 750 | -25870.0 | -34.4933 | 115.74 | 0 |
| 1500 | -53620.0 | -35.7467 | 119.82 | 0 |

```
   75 |   -18.2000 --------------------
  150 |   -24.4667 ---------------------------
  300 |   -30.7333 ----------------------------------
  750 |   -34.4933 ---------------------------------------
 1500 |   -35.7467 ----------------------------------------
```

## 8. Holdout

NOT ACCESSED (holdout preserved)

