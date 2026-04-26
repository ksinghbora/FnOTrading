# Validation Report — portfolio

- Run ID: `portfolio_20260424_1849`
- Split train_end: 2024-11-29
- Split val_end: 2024-12-31
- Split holdout_end: 2025-01-31

## 1. Executive Summary

| Gate | Status | Reason |
|---|---|---|
| cpcv_stability | FAIL | median=-0.140 (>0.5), p05=-0.180 (>0) |
| cpcv_pbo | PASS | PBO not computed (single-config CPCV) — WARN |
| dsr | FAIL | dsr=0.000 (>0.95) |
| wf_decay | PASS | median_decay=0.000 (<0.5) |
| wf_coverage | FAIL | fraction_positive_test=0.00 (>=0.7) |
| regime | FAIL | failing: high_vix(sharpe=-2.62, n=1674), trending(sharpe=-8.00, n=684) |
| cost_sensitivity | FAIL | sharpe@+0.5 shift = -0.010 (>0) |
| capacity | FAIL | pnl_per_lot declines up to 300 lots: [-3.9539, -10.9039, -19.5039] |

**Final Verdict: FAIL**

## 2. Split

- Train window: ends 2024-11-29 (61 days)
- Val window: 2024-11-29 → 2024-12-31 (21 days)
- Holdout window: 2024-12-31 → 2025-01-31 (0 days)

## 3. CPCV Distribution

- Paths: 45
- Mean Sharpe: -0.029
- Median Sharpe: -0.140
- 5th pct Sharpe: -0.180
- 95th pct Sharpe: 0.460
- PBO: N/A (single-config)
- DSR: 0.000
- PSR: 0.178

```
[ -0.290.. -0.215] ## (1)
[ -0.215.. -0.140] ################################################## (23)
[ -0.140.. -0.065] ############# (6)
[ -0.065.. +0.010]  (0)
[ +0.010.. +0.085] ################# (8)
[ +0.085.. +0.160]  (0)
[ +0.160.. +0.235]  (0)
[ +0.235.. +0.310]  (0)
[ +0.310.. +0.385]  (0)
[ +0.385.. +0.460] ############### (7)
```

## 4. Walk-Forward

- Windows: 0 | median_decay=0.000 | frac_positive=0.00 | mean_test_sharpe=0.000

| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |
|---|---|---|---|---|---|---|

## 5. Regime Stratification

| regime | num_trades | total_pnl | sharpe | win_rate | max_dd | passed |
|---|---|---|---|---|---|---|
| **high_vix** | **1674** | **-485520.28** | **-2.6169** | **59.92** | **-487597.35** | **FAIL** |
| mid_vix | 2888 | 252547.75 | 1.1719 | 61.63 | -417327.0 | PASS |
| low_vix | 2415 | 548459.25 | 3.0141 | 53.66 | -374901.75 | PASS |
| expiry_week | 3792 | 574672.47 | 2.3062 | 61.16 | -349623.75 | PASS |
| event_day | 774 | 207401.99 | 2.546 | 66.8 | -20248.5 | PASS |
| **trending** | **684** | **-932276.25** | **-7.9975** | **35.09** | **-939477.75** | **FAIL** |
| range_bound | 4066 | 799499.97 | 2.6615 | 53.62 | -317894.25 | PASS |

## 6. Cost Sensitivity

| shift | sharpe | profit_factor | total_pnl |
|---|---|---|---|
| -1.00 | 0.240 | 1.04 | 7479.09 |
| -0.50 | 0.160 | 1.03 | 4909.4 |
| -0.25 | 0.120 | 1.02 | 3624.55 |
| +0.00 | 0.070 | 1.01 | 2339.71 |
| +0.25 | 0.030 | 1.01 | 1054.87 |
| +0.50 | -0.010 | 1.0 | -229.98 |
| +1.00 | -0.090 | 0.99 | -2799.66 |

## 7. Capacity

| lot_size | total_pnl | pnl_per_lot | avg_slippage_bps | synth_fallback_legs |
|---|---|---|---|---|
| 75 | -296.54 | -3.9539 | 8.08 | 66 |
| 150 | -1635.58 | -10.9039 | 11.04 | 66 |
| 300 | -5851.16 | -19.5039 | 15.48 | 66 |
| 750 | -24437.9 | -32.5839 | 25.19 | 66 |
| 1500 | -75215.79 | -50.1439 | 40.19 | 66 |

```
   75 |    -3.9539 ---
  150 |   -10.9039 ---------
  300 |   -19.5039 ----------------
  750 |   -32.5839 --------------------------
 1500 |   -50.1439 ----------------------------------------
```

## 8. Holdout

NOT ACCESSED (holdout preserved)

