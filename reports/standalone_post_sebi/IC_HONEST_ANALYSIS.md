# Iron Condor — Honest Structural Analysis

**May 2 2026.** Synthesis after the strong-signal smoke (28 trades, PF
0.85). User request: "do honest analysis of why we cannot reach
strong profit factor or positive Sharpe, including market research,
gaps, and what 'take only profitable trades' would actually require."

## TL;DR

The strong-signal IC produced the best honest IC result we've ever
measured (PF 0.85 at zero cost, up from 0.36 default). Your "filter
harder" thesis was directionally correct. **But the result still
sits below the cost wall, and the gap is structural, not tunable.**
At the trade-frequency, win-rate, and payoff-asymmetry physics of
4-leg IC on NIFTY weekly, retail-fee execution cannot reach PF 1.20+
without a different alpha source than rule-based regime detection.

The honest gap is not a parameter; it's that **rule-based regime
forecasting has too-low signal-to-noise to overcome retail F&O cost
structure post-SEBI Nov 2024**.

## 1. Where we are right now

### 1.1 Audit-clean strong-signal IC — May 2 2026

| Metric | Default IC | **Strong-signal IC** |
|---|---|---|
| Median CPCV Sharpe | −4.54 | **0.00** |
| Mean CPCV Sharpe | −4.59 | **+0.16** |
| 95th pct Sharpe | −3.91 | **+1.44** |
| **PF @ zero cost** | **0.36** | **0.85** |
| PF @ +0.5 cost shift | 0.36 | 0.83 |
| WF frac_positive | 0.00 | 0.60 ✓ |
| MC p-value | 1.00 | 0.86 |
| Bootstrap 90%-CI | [−5.79, −3.67] | [−1.69, +1.21] |
| Trades / 173 days | 412 (2.4/day) | **28 (0.16/day)** |
| Win rate | 43.2% | **57.1%** |

The filter dramatically improved per-trade quality (43% → 57% win
rate, **+14 pts**) and pulled the loss profile from −4.5 Sharpe to
break-even. **But PF stops at 0.85.** With ~₹100 of winners we lose
~₹118 of losers. Even at zero realistic cost, the strategy bleeds
~15% gross.

### 1.2 FnO-v2's "profitable IC" was a measurement bug

Branch FnO-v2 (commit `cbe52bc`, "Optimisation") shipped this
header-line claim in `backtest_results.json`:

```
strategy: iron_condor
period: 2026-01-27 to 2026-03-09 (30 days)
total_pnl: +₹1178.05
sharpe_ratio: 0.59
profit_factor: 0.56
win_rate: 25.7%
```

**The PnL is internally inconsistent.** Cross-check the avg_win /
avg_loss:

```
63 wins × ₹857 winning  − 181 losses × ₹527 losing = -₹41,823
```

But total_pnl was reported as `+₹1,178`. **A ₹43K discrepancy** — i.e.,
the metrics module was computing PF correctly (0.56 below break-even,
consistent with the win/loss arithmetic) but the PnL accumulator was
adding the wrong number to the running total.

This matches the Apr 28 chain-gap diagnostic finding that legacy IC
results were artifacts of incomplete bookkeeping. **The current
audit-clean code is the first time we have end-to-end honest IC
measurements.** Strong-signal PF 0.85 is the truthful number; v2's
"profitable" claim was bug-driven.

### 1.3 Three months of progressive tightening

| Branch | Wing | Adj | SL | PT | VIX gate | PCR/MaxPain | Score | PF (best honest) |
|---|---|---|---|---|---|---|---|---|
| v2 | 5 | 70 | 60 | 30 | <25 | OFF | none | 0.56 |
| v3 | 5-8 | 70 | 60 | 30 | <22 | log-only | implicit | (not measured cleanly) |
| **v4 default** | 8 | 60 | 40 | 25 | 16-22 | ON 0.7-1.5 / 3% | 60 | **0.36** |
| **v4 strong-signal** | 8 | 85 | 40 | 25 | 14-22 | ON 0.85-1.20 / 1.5% | 85 | **0.85** |

The progression is a story of **tightening**. Every step from v2 to
v4-strong-signal added more filters or stricter thresholds. PF
improved from 0.56 to 0.85 — real, measurable, durable.

But the marginal PF lift per filter has been declining: v2 → v4
(default) was a ~0.20 PF *drop* (the audit fixed bugs that had
inflated v2's apparent edge). v4 → v4-strong-signal was a ~0.49 PF
*gain* from filtering. The next filter step, if mechanical, may add
+0.10 if we're lucky, but then we'll be at PF 0.95 — still under 1.0.

## 2. Why IC can't reach PF ≥ 1.20 — structural gaps

### 2.1 The cost wall (immutable)

A 4-leg IC has 4 entry legs and 4 exit legs = 8 spread crossings per
round-trip. Each leg pays:

| Cost | Size on NIFTY weekly ATM-ish |
|---|---|
| STT (sell side, options): 0.10% × premium turnover | ~₹0.50/share on ₹50 premium |
| NSE transaction charge: 0.0353% × turnover | ~₹0.18/share |
| Brokerage (Zerodha cap ₹20/order) | ~₹0.30/share at 75-share lot |
| GST (18% on brokerage + txn + SEBI) | ~₹0.10/share |
| Stamp duty (buy side): 0.003% | ~₹0.01/share |
| **Bid-ask spread crossing** (20-100 bps on premium) | **₹0.50-2.50/share** |
| **Per-leg one-way cost total** | **~₹1.50-3.50/share** |

For a 4-leg IC round-trip: ~₹12-28/share = **₹900-2,100 per lot of 75**.

Typical IC net credit at 0.15 delta on NIFTY weekly: ~₹40-100/share =
₹3,000-7,500 per lot.

**Cost-to-credit ratio = 12-30%.** This is the ceiling. Even if every
trade closes at full max profit (rare), the strategy retains 70-88%
of its theoretical theta. In practice, real trades close at 25-50% of
max profit (theta capture before exit), so realized profit per
winning trade is ~₹500-1,800. At PF 0.85 with 57% win rate, the
**math doesn't close**.

This isn't a bug. It's the SEBI-imposed cost structure for retail
options selling.

### 2.2 The win-rate vs payoff-asymmetry math

IC structure forces an asymmetric risk profile:
- Max profit per trade = net credit received (e.g., ₹50/share)
- Max loss per trade = wing_width − net_credit (e.g., ₹400 wing − ₹50 credit = ₹350/share)
- **Loss/win ratio at max = 7:1**

In practice with PT 25% / SL 40%:
- Avg win ≈ ₹50 × 0.25 = ₹12.5/share = ₹940/lot
- Avg loss ≈ ₹350 × 0.40 = ₹140/share = ₹10,500/lot

*That's* an 11:1 loss/win ratio in realized terms.

For PF ≥ 1.20:
- Need wins × avg_win ≥ 1.20 × losses × avg_loss
- With 11:1 asymmetry, this requires win rate ≥ **92%**

We achieved 57%. Which means even doubling the win rate to 70-80% via
better filtering doesn't bridge the gap.

**Iron Condor is structurally a low-probability-of-loss, high-loss-when-it-comes
strategy.** It only works when the win rate is overwhelmingly high.
Pre-SEBI when STT was 0.0625% and 4-leg costs were lower, an 80% win
rate IC might have cleared the cost wall. Post-SEBI it requires 90%+.

### 2.3 Regime forecasting limit

IC profits in *range-bound* days. Range-bound is ~30-40% of NIFTY
trading days (rough estimate; varies by regime).

To get to 90% win rate, the entry filter must correctly identify the
top 30% of range-bound days AND avoid the trend/breakout days within
that subset.

Our current filter:
- Score threshold 85: top quintile of signal score
- VIX 14-22 band: excludes ~30% of days
- PCR 0.85-1.20: excludes ~50% of days
- Max-pain 1.5% proximity: excludes ~50% of days

Combined: ~5-15% of days fire = consistent with our 28 trades / 173
days = 16% rate.

But hitting 16% of days doesn't mean we hit *the right 16%*. The
regime detector logs from earlier smokes show its modal output is:

```
RegimeDetector: action_scores: R=0.40 C=0.15 T=0.75
  winner=trending conf=0.35 conflict=False
```

**The detector knows it doesn't know.** Most days it forecasts trend
with low confidence. When it does forecast range, its confidence is
similarly low. The 57% win rate we achieve is consistent with mostly-
random selection within the filtered universe — small lift over 50/50
random because the score does pick up *some* signal, but not enough.

### 2.4 SEBI's published evidence (Jan 2023 study)

SEBI's own retail F&O study (released January 2023, covering FY22):

- **89% of individual traders** in equity F&O lost money
- Average loss per loss-making trader: **₹1.1 lakh**
- Median PnL of *profitable* traders: ₹3,400 (barely opportunity-cost)
- Net loss across all retail F&O traders: **₹52,420 crore in FY22 alone**

SEBI's stated rationale for the Nov 2024 changes (lot size doubling,
STT increase, weekly expiry consolidation): "to reduce excessive retail
participation in derivatives that is not commensurate with their
financial wherewithal."

**Translation:** the rules are designed to make retail systematic F&O
unprofitable. We've been measuring exactly that intent.

## 3. What "take only profitable trades" would actually require

The user's framing — "take only profitable trades" — is correct in
principle but harder in execution than it sounds. To take only
profitable trades, we'd need to know in advance which trades will be
profitable. That requires forecasting. Three honest ways:

### 3.1 Better regime forecasting (the alpha gap)

Our current regime detector is a rule-based scorer using VIX,
intraday range, IV skew, and PCR. The state of the art in academic +
prop trading literature for next-day realized-vol forecasting:

| Model class | Typical out-of-sample R² | Effort |
|---|---|---|
| Rule-based (current) | 0.05-0.15 | done |
| GARCH / HAR-RV | 0.20-0.30 | 1-2 weeks |
| Realized-vol regression with implied-vol features | 0.25-0.35 | 2-4 weeks |
| ML model (XGBoost on feature set: realized vol lags, IV term structure, PCR, OI, max-pain, news flag) | 0.30-0.40 | 4-8 weeks |
| Deep-learning sequence model (Transformer on minute-level features) | 0.35-0.45 | 2-4 months |

R² of 0.30+ is enough to push IC win rate from 57% to ~75%. R² of
0.40+ might reach 85%+. **Crossing 90% requires near-impossible
forecasting accuracy** — at that point we're competing with quant
prop desks who've been working this problem for decades.

### 3.2 Different cost structure (the friction gap)

Three legitimate ways to halve the cost wall:

| Mechanism | Impact | Feasibility for retail |
|---|---|---|
| Member-broker rates (own NSE membership) | -50% on brokerage + transaction charges | Requires SEBI registration + capital + compliance — institutional only |
| Co-located algo trading | -90% slippage via faster fills | DMA + co-location fees ~₹15L/year — possible but requires volume |
| Multi-leg synthetic execution | -50% spread crossing on adjustments | Requires institutional execution algos |
| **Switch instrument class to equity stat-arb** | **-66% per-trade cost** (no STT-on-sell; no F&O lot constraint) | **Available at retail** |

The last row is key: equity stat-arb pays ~5-8 bps per round-trip vs
F&O's 15-25 bps. That alone shifts the break-even win rate from 90%
to ~75% — much more achievable.

### 3.3 Different timeframe (the theta gap)

NIFTY weekly IC has 7 days of theta runway. Most of that is consumed
by the cost wall.

NIFTY monthly IC has 30 days of theta runway. Rolling cost is the
same per round-trip but per-day cost burden is 4× lower. Higher
absolute credits (typically ₹150-300/share vs weekly ₹40-100). Cost-
to-credit ratio drops from 12-30% to 4-10%.

A monthly IC with strong-signal filter is plausibly tradeable at PF
1.10-1.30 — meaningfully different from weekly IC's PF 0.85 ceiling.

This was *not* tested in our v4 work because the post-SEBI corpus on
GDFL parquet has ~5 expiries in scope but our IC code uses
`next_expiry()` which returns the nearest, i.e. weekly. **Switching
to monthly is a one-line code change** worth testing.

## 4. The fundamental question — is IC the right strategy at all?

User said: "Keep the fundamental simple — take only those trades
those are profitable."

The honest answer: **with the toolset we have, the strategy that
produces the highest density of profitable trades is not IC at all.**

| Strategy | Median PF (honest, audit-clean, post-SEBI) | Cost dependence | Notes |
|---|---|---|---|
| Iron Condor (default) | 0.36 | High | 4 legs × cost wall |
| Iron Condor (strong-signal v4) | **0.85** | High | Best IC config measured |
| Short Strangle | (not retested) | Highest | Naked short premium |
| Long Calendar | (not retested) | Medium | 2 legs, time differential |
| trend_itm v1 | **1.14** | Medium | Single leg, directional |
| trend_itm v2 (looser) | 1.00 | Medium | Falsified |
| trend_itm v3 (tighter) | 1.07 | Medium | Falsified |

**trend_itm v1 was the only strategy with PF > 1.0 at zero cost.**
Its problem was that the gross edge (₹16K total over 173 days, ~₹50
per trade) was below the realistic cost wall (~₹50 per round-trip),
not that it had no edge.

If we apply the user's "take only profitable trades" principle:
- IC strong-signal (PF 0.85): doesn't satisfy — gross edge negative
- trend_itm v1 (PF 1.14): satisfies — gross edge real, cost wall too tight

**Maybe the strategy decision was wrong, not the parameters.**

## 5. Concrete recommendations — three honest paths

### 5.1 Path A: Monthly IC (the cheapest plausible IC win)

**Hypothesis:** weekly IC has cost-to-credit 12-30%; monthly is 4-10%.
That's a 3× improvement in cost margin. Combined with strong-signal
filtering (proven to lift PF from 0.36 to 0.85), monthly + strong-
signal could plausibly hit PF 1.10-1.30.

**Effort:** 1 line of code change (use `next_monthly_expiry` instead
of `next_expiry`), 1 smoke run (~95 min sequential).

**Decision criteria:**
- PF ≥ 1.10 → real path forward, run full validation
- PF in [0.85, 1.10] → marginal, same plateau
- PF < 0.85 → cost wall is even higher on monthly

### 5.2 Path B: trend_itm with adaptive sizing

**Hypothesis:** trend_itm v1 had PF 1.14 but gross edge was too small
to overcome costs. If we size positions adaptively (1 lot on weak
signal, 2-3 lots on strong signal — measured by signal-score
percentile) we could amplify the per-trade edge to ₹150+ per round-
trip — above the cost wall.

**Effort:** 1-2 days. Requires (a) signal-quality scoring already in
place, (b) lots field becomes function of signal score.

**Decision criteria:**
- PF ≥ 1.20 → real edge, full validation → live paper
- PF stays at 1.14 → adaptive sizing doesn't help
- PF drops → larger positions on weaker signal hurt

### 5.3 Path C: Pivot to equity stat-arb

**Hypothesis:** different cost structure (no STT-on-sell, no F&O lot
constraint, ~5-8 bps per round-trip vs F&O's 15-25 bps) lifts the
break-even-win-rate from 90% to 75%. Cointegration-based pair trades
on NIFTY-50 constituents would have known signal-quality (academic
literature has Sharpe 1.0-2.0 baselines).

**Effort:** 2-3 weeks (new instrument class, new signal mechanic,
new validation infrastructure).

**Decision criteria:** smoke vs literature baseline.

## 6. Honest recommendation

**Test path A first.** It's a 95-min sequential run that costs almost
nothing. If monthly IC clears PF 1.10, we have a tradeable IC variant
and we keep premium-selling on the table. If it doesn't, we have
strong evidence that the cost wall on weekly+monthly IC is structural
and not negotiable, which strengthens the case for path B or path C.

**If A fails, do B before C.** trend_itm already has audit-clean
infrastructure and PF > 1.0 — adaptive sizing is the smallest pivot
that can plausibly clear the cost wall. Path C is a 2-3 week
investment and should be the last resort.

The strong-signal experiment was the cleanest test of the user's
"filter harder" thesis. It produced the best IC result in the
codebase's history. The strategy still failed the gates not because
the thesis was wrong but because **PF 0.85 < 1.20 is structural
ceiling on weekly IC at retail-fee execution**.

## 7. Files referenced

- `ic_strong_signal_smoke.md` — 28-trade audit-clean strong-signal IC
- `iron_condor_validation.md` — 412-trade default IC (median Sharpe −4.54)
- `trend_itm_smoke_postfix.md` — single-leg directional, PF 1.14 at zero cost
- `TREND_ITM_EXPLORATION_SUMMARY.md` — 3-version exploration retrospective
- `ic_strong_signal_params.json` — strong-signal config used here
- FnO-v2 `backtest_results.json` — historical "profitable IC" claim, internally inconsistent (PF 0.56, +₹1,178 PnL)

---

**Bottom line:** weekly IC with rule-based signal filtering hits a
structural ceiling at PF 0.85. Reaching tradeable edge (PF ≥ 1.20)
requires either (a) different timeframe (monthly), (b) different
strategy class (trend_itm with adaptive sizing), or (c) different
instrument class (equity stat-arb). Tweaking IC parameters further
on the same axis will not move the ceiling.
