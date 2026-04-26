# Validation Report — portfolio

- Run ID: `portfolio_20260423_2351`
- Split train_end: 2025-02-10
- Split val_end: 2025-02-14
- Split holdout_end: 2025-02-21

## 1. Executive Summary

| Gate | Status | Reason |
|---|---|---|
| cpcv_stability | PASS | median=15.575 (>0.5), p05=11.962 (>0) |
| cpcv_pbo | PASS | PBO not computed (single-config CPCV) — WARN |
| dsr | FAIL | dsr=0.873 (>0.95) |
| wf_decay | FAIL | median_decay=12.760 (<0.5) |
| wf_coverage | PASS | fraction_positive_test=1.00 (>=0.7) |
| regime | PASS | all regimes within bounds |
| cost_sensitivity | PASS | sharpe@+0.5 shift = 1.500 (>0) |
| capacity | FAIL | pnl_per_lot declines up to 300 lots: [101.15, 100.15, 99.15] |

**Final Verdict: FAIL**

## 2. Split

- Train window: ends 2025-02-10 (6 days)
- Val window: 2025-02-10 → 2025-02-14 (4 days)
- Holdout window: 2025-02-14 → 2025-02-21 (0 days)

## 3. CPCV Distribution

- Paths: 2
- Mean Sharpe: 15.575
- Median Sharpe: 15.575
- 5th pct Sharpe: 11.962
- 95th pct Sharpe: 19.189
- PBO: N/A (single-config)
- DSR: 0.873
- PSR: 0.920

```
[+11.560..+12.363] ################################################## (1)
[+12.363..+13.166]  (0)
[+13.166..+13.969]  (0)
[+13.969..+14.772]  (0)
[+14.772..+15.575]  (0)
[+15.575..+16.378]  (0)
[+16.378..+17.181]  (0)
[+17.181..+17.984]  (0)
[+17.984..+18.787]  (0)
[+18.787..+19.590] ################################################## (1)
```

## 4. Walk-Forward

- Windows: 3 | median_decay=12.760 | frac_positive=1.00 | mean_test_sharpe=17.963

| idx | train | test | train Sharpe | test Sharpe | decay | num_trades |
|---|---|---|---|---|---|---|
| 0 | 2025-02-03..2025-02-05 | 2025-02-07..2025-02-10 | 29.220 | 15.870 | 13.350 | 8 |
| 1 | 2025-02-05..2025-02-07 | 2025-02-11..2025-02-12 | 21.190 | 8.430 | 12.760 | 12 |
| 2 | 2025-02-07..2025-02-11 | 2025-02-13..2025-02-14 | 17.640 | 29.590 | -11.950 | 16 |

## 5. Regime Stratification

| regime | num_trades | total_pnl | sharpe | win_rate | max_dd | passed |
|---|---|---|---|---|---|---|
| high_vix | 64 | 29190.04 | 14.611 | 78.12 | -1697.22 | PASS |
| mid_vix | 35 | 36177.75 | 11.9335 | 77.14 | 0.0 | PASS |
| low_vix | 0 | 0.0 | 0.0 | 0.0 | 0.0 | PASS |
| expiry_week | 57 | 38163.79 | 11.6359 | 78.95 | -1697.22 | PASS |
| event_day | 15 | 5673.03 | 7.0229 | 66.67 | -1697.22 | PASS |
| trending | 5 | 4234.5 | 55.0141 | 100.0 | 0.0 | PASS |
| range_bound | 73 | 50918.65 | 23.8622 | 76.71 | 0.0 | PASS |

## 6. Cost Sensitivity

| shift | sharpe | profit_factor | total_pnl |
|---|---|---|---|
| -1.00 | 1.760 | 1.31 | 8896.87 |
| -0.50 | 1.670 | 1.29 | 8460.94 |
| -0.25 | 1.630 | 1.28 | 8242.97 |
| +0.00 | 1.590 | 1.27 | 8025.0 |
| +0.25 | 1.550 | 1.27 | 7807.03 |
| +0.50 | 1.500 | 1.26 | 7589.06 |
| +1.00 | 1.420 | 1.24 | 7153.13 |

## 7. Capacity

| lot_size | total_pnl | pnl_per_lot | avg_slippage_bps | synth_fallback_legs |
|---|---|---|---|---|
| 75 | 7586.25 | 101.15 | 7.41 | 0 |
| 150 | 15022.5 | 100.15 | 8.83 | 0 |
| 300 | 29745.0 | 99.15 | 10.26 | 0 |
| 750 | 73912.5 | 98.55 | 11.11 | 0 |
| 1500 | 147525.0 | 98.35 | 11.4 | 0 |

```
   75 |  +101.1500 ########################################
  150 |  +100.1500 ########################################
  300 |   +99.1500 #######################################
  750 |   +98.5500 #######################################
 1500 |   +98.3500 #######################################
```

## 8. Holdout

NOT ACCESSED (holdout preserved)

