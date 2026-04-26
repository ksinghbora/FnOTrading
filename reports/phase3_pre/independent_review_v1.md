# Independent Review of Phase 3-Pre Truth-Up

- Reviewer: independent agent (no access to operator's reasoning during review)
- Generated: 2026-04-25
- Files audited: `scripts/truthup_phase3_pre.py`, `reports/phase3_pre/fee_truthup_v1.md`,
  `src/core/constants.py`, `src/portfolio/charges.py`, the 19 chain CSVs, the 19 decisions CSVs
  in `data/decisions/`, and `reports/validation/wide_baseline_portfolio.md`.

## TL;DR

**Disagree with PROVISIONAL PASS. The honest call is FAIL — or at minimum INSUFFICIENT_DATA / PAUSE.**
The "PASS" rests on 13 outcomes that are not 13 independent observations: they are at most
**4 trading days** of decisions, and inside each day the 3-4 strategy IDs fired sub-second apart
on the same spot, so they are correlated bets, not independent samples. Once the n is corrected,
the per-trade Sharpe of +2.12 has a 95% confidence interval that straddles zero. The verdict
is also directly contradicted by `wide_baseline_portfolio.md`, which on a *much larger* sample
(200 days, 800+ trades) showed pnl/lot = -₹82 at the minimum lot size with stale (lower) charge
rates. That FAIL does not become a PASS because we measured 13 *post-Apr-1* paper-trade decisions
in a five-trading-day stretch.

## Charge rates verification

The charge rates that the operator placed in `src/core/constants.py::CHARGES` are correct as
far as published authoritative sources go.

| Charge | Code value | Authoritative source | Verdict |
|---|---|---|---|
| STT options sell, default | 0.10% | Union Budget 2024, effective Oct 1 2024 (per Zerodha Z-Connect, ICICIdirect, Outlook Money) | Correct |
| STT options sell, `_apr2026` | 0.15% | Union Budget 2026, effective Apr 1 2026 (per Bajaj Finserv, HDFC Bank, Cleartax) | Correct |
| STT options exercise, default | 0.125% | Pre-Apr-2026 rate on intrinsic value | Correct |
| STT options exercise, `_apr2026` | 0.15% | Budget 2026 (Bajaj Finserv) | Correct |
| STT futures sell, default | 0.02% | Oct 2024 hike from 0.0125% to 0.02% | Correct |
| STT futures sell, `_apr2026` | 0.05% | Budget 2026 (Bajaj Finserv) | Correct |
| Exchange options | 0.0353% | NSE circular FA64232 / SEBI True-to-Label Jul 2024, effective Oct 1 2024 (per Zerodha Z-Connect, Bajaj Broking) | Correct |
| Exchange futures | 0.00173% | Same NSE circular | Correct |
| SEBI turnover | 0.0001% | SEBI standard | Correct |
| GST | 18% on (brokerage + exchange + SEBI) | CBIC standard | Correct |
| Stamp duty options buy | 0.003% | Uniform across India since 2020 | Correct |
| Brokerage | min(0.03%, ₹20) | Zerodha pricing | Correct |

One lurking concern: the operator dates the script-aware STT cutover as `STT_TRANSITION_DATE = date(2026, 4, 1)` and labels rates as "0.10% (Oct 1 2024 - Mar 31 2026)" / "0.15% (Apr 1 2026 onwards)".
That is internally consistent and matches the public sources. **No charge-rate red flag.**

A *separate* point that the report does not flag but should: the 13 trades in the sample are
**all** post-Apr-1 (STT 0.15%). The "date-aware STT" infrastructure is correct in the script,
but with this sample it is exercised on exactly one branch — pre-Apr-1 STT was never tested by
this run. The infrastructure still doesn't guarantee correctness in production for the lower
rate; that branch was never executed against real trades.

## Methodology assessment

What the script does correctly:
- Loads paired ENTER+EXIT rows by `(strategy_id, leg, cumcount-per-decision)`. Same pairing
  logic the operator used to fix Bug 1 in the validation harness.
- Treats `outcome_pnl` as gross (correct — the strategy logs decay × qty without charges).
- Computes round-trip charges per leg using the date-aware STT, and uses `OrderSide.SELL` on
  premium-leg entry / `OrderSide.BUY` on exit (correct for short-premium strategies).
- Applies the `_replay` / `_bt` strategy-id filter to drop backtest contamination (essential —
  see "Backtest contamination handling" below).
- Drops exact-duplicate `(timestamp, strategy_id, leg, decision)` rows (mitigates Bug 4
  pollution).
- Caveats most of its known weaknesses in the report (entry-only slippage, linear lot scaling,
  small sample). Methodology disclosure is genuinely good.

What is wrong / hidden / biased:

1. **Sharpe formula conflates per-trade and annualised semantics.**
   `_per_trade_sharpe = mean / std × √N` returns the same number whether N is 13, 130, or 13,000
   provided the mean and std are stable. This is a *t-statistic*, not a Sharpe ratio. The PASS
   gate is written as "1-lot net Sharpe > 0.5" but the spec in `PHASE3_MASTER §V.5` doesn't
   define which Sharpe (annualised? per-trade?). The script's inflated number (+2.12) is what
   triggers the PASS — a daily-pnl Sharpe annualised by √252 over 4 days would not.

2. **N=13 is overstated. Effective sample is ≤4 days.**
   On 04-22, four strategies fire `ENTER` between `10:44:34.315` and `10:44:35.079`.
   On 04-23, four strategies fire `ENTER` between `09:54:57.085` and `09:54:57.271`.
   On 04-24, four strategies fire `ENTER` between `10:34:59.292` and `10:34:59.314`.
   These are sub-second apart on the same spot. They are **four positions sharing an entry
   decision** (the regime/score ENTER trigger), not four independent trades. `portfolio_1` and
   `strangle_1` even use the *same* mode (`strangle`) with the *same* entry_premium (`23.6`)
   and same quantity on 04-22 — they are literally duplicate positions with different IDs.
   Treating them as 13 independent draws inflates Sharpe by roughly √(13/4) ≈ 1.8×.

3. **Charge approximation systematically understates costs on iron condor.**
   The script splits `entry_premium` evenly across 4 legs for IC. But in reality, the IC's
   short legs (where STT applies) carry most of the premium — not 25% each. STT and exchange
   are charged on a higher per-leg notional than the script models. The operator caveats this
   ("conservative for cost magnitude") but it actually goes the other way for short-side
   exchange/STT calculations on the wings vs the body. Direction of bias: charges are
   *understated* by perhaps 10-25% on the IC trades, not overstated.

4. **Slippage tolerance was tightened mid-run from 50% to 20%.**
   Line 379: `if diff < best_diff and diff < target_total * 0.20:  # tightened`. The comment
   "tightened" admits the tolerance was changed. This is methodology iteration on real data —
   it falls afoul of `PHASE3_MASTER §V.5.5` which says "no iterating on cutoffs". Even if the
   tightening was the right call for accuracy, it should have been done before any data was
   examined and committed.

5. **8 of 13 reconstructed slippage cases excludes straddle entirely.**
   The script intentionally skips strike reconstruction for `mode == "straddle"`. Of the 5
   un-reconstructed cases, 3 are the straddle_1 trades — and those 3 trades contribute 76%
   of net P&L. So the slippage finding ("mean -0.1%") is computed on the cohort that
   contributes 24% of the P&L, not the cohort that drives the verdict. The most P&L-impactful
   trades have **no slippage truth-up at all**. The "essentially zero slippage" headline does
   not apply to where the money came from.

6. **Linear lot scaling makes the 5-lot Sharpe gate trivial.**
   By construction `Sharpe(5×) = Sharpe(1×)` under the linear approximation in `_verdict`.
   The PASS criterion "5-lot net Sharpe > 0" is therefore identical to "1-lot Sharpe > 0",
   which is a strictly weaker test than the 1-lot Sharpe > 0.5 already required. The 5-lot
   gate provides zero additional information in this script. The capacity gate in
   `wide_baseline_portfolio.md` was the actual binding test, and it FAILed at 75 lots.

7. **`outcome_pnl` for IC `portfolio_1` exits is sometimes computed from a partially-decayed
   premium.** Line 6 of decisions_2026-04-22.csv shows `entry_premium=23.47728` on the EXIT
   row for `portfolio_1` — that's the strategy's own running premium estimate, not the chain's
   bid/ask. The script uses the ENTER row's `entry_premium`, which is fine, but this confirms
   that the strategy's exit pnl is a model number, not a fill number — every "gross P&L" in
   the report inherits the strategy's mid-or-LTP exit assumption. Real exit slippage will
   eat some unknown chunk of every figure.

8. **Sample-size warning is correctly issued but inconsistent with verdict label.**
   The script computes `sample_size_warning` only when `n_trades < 120`, then renders the
   verdict as "PROVISIONAL PASS" (a label not in the locked criteria of `§V.5`). The locked
   verdicts are PASS / YELLOW / FAIL / INSUFFICIENT_DATA. "PROVISIONAL PASS" is a new label
   the script invented at line 578-585 to soften a PASS the operator was uncomfortable
   declaring as outright PASS. **`§V.5.5` explicitly forbids iterating on cutoffs:
   "Borderline → accept, don't relax."** The operator should either commit to PASS (with all
   that implies) or downgrade to one of the locked categories. Inventing a new category to
   sit between PASS and FAIL is the very kind of relaxation the discipline rules forbid.

## Sample size and statistical power

The report's own warning ("need ~120-180 trades for Sharpe estimation at 95% confidence") is
correct — and the verdict is then issued anyway on n=13.

Quantifying it: per-trade pnl mean = ₹558.27, std (rough) ≈ ₹950 from the dispersion in the
data. Per-trade Sharpe-as-t-stat = 558/950 = 0.587. With N=13, the standard error of that
ratio is approximately 1/√(13 - 1) ≈ 0.289. The 95% CI for the per-trade Sharpe is therefore
roughly **[+0.02, +1.16]** under the optimistic assumption that the 13 trades are independent.
Multiplied by √13 to get the script's reported number, that's [**+0.07, +4.18**]. The lower
bound only barely clears zero — it does not clear the +0.5 PASS threshold.

If we correct for the fact that trades are clustered in 4 days with 3-4 correlated positions
per day, the effective sample is closer to N_eff ≈ 5 (think one independent decision per day
plus a small contribution from same-day diversification). Under N_eff = 5, the 95% CI of the
per-trade Sharpe is closer to **[-0.4, +1.6]** — clearly straddles zero.

In other words, the math does not allow a confident "+2.12 Sharpe" claim. **The number is
inside the cone of statistical noise around zero on either side of the PASS gate.** This is
the single biggest reason the PASS verdict is wrong — not because charges are wrong, not
because the methodology is broken in some hidden way, but because thirteen sub-second-correlated
decisions over five trading days do not constitute evidence for or against the PASS bar.

## Concentration risk

Yes, this is a major red flag, and the operator's own memory file acknowledges it: "without
straddle_1, net would be ₹2,332 across 10 trades." That is honest. The script's verdict
machinery does not penalise concentration. Some specifics:

- straddle_1 contributes **₹4,925.62 net of ₹7,257 total = 67.9%** (operator says 76%; my
  arithmetic gives 67.9% — minor disagreement, likely from including the straddle slippage
  adjustment somewhere).
- One single trade — the 04-23 straddle exit at +₹3,030 net — is **41.8% of all net P&L** in
  the sample.
- Even within straddle_1's 3 trades, the win-loss profile is +1944 / +3030 / -50 → if
  the +3030 had instead been a -3030 (a vol-expansion shock day, exactly the failure mode
  flagged by `wide_baseline_portfolio.md`), the entire verdict flips to FAIL.

Concentration of this severity in a 13-trade sample means the verdict is one losing tail
event away from FAIL. Combined with the small-N statistical concern, this is a textbook
"do not declare an edge" situation.

Worth noting: the straddle is fundamentally short-vol, just like the strangle and IC. The
sample window happens to span one expiry week (04-22 was 6 DTE, 04-23 was 5 DTE, 04-24 was
4 DTE) where realised vol stayed contained. That is exactly the regime the wide_baseline
analysis identified as profitable for short-vol — the result is *consistent* with the prior
FAIL verdict, not contradicting it.

## Backtest contamination handling

Filtering `_replay` and `_bt` was necessary. Confirmed by inspecting the raw decision files:
13 of 19 chain dates contain ONLY `portfolio_replay` rows (with backtests writing into the
live decisions directory). 2 dates (04-16, 04-21) have no decisions file at all. Without the
filter, the truth-up would be measuring backtest output, not paper-trade output.

But two things are concerning about the contamination:

1. The fact that backtest and live trades are sharing the same directory at all is a system
   hygiene bug. `FNO_DISABLE_DECISIONS=1` was added in the validation harness to address
   parallel-CPCV racing, but it's not enabled by default for backtest runs that aren't part
   of validation. The truth-up script's filter is patching over a write-path bug elsewhere
   in the system.

2. Filtering also discarded the entire **pre-Apr-1-2026 portion of the chain window**.
   The chain CSVs span Mar 25 → Apr 24 (19 days). The 13 surviving trades are all from
   Apr 20-24 (4 days). All five days from Mar 25-30 plus Apr 1 had only `portfolio_replay`
   decisions and got dropped. So:

   - The truth-up has **zero data points at the 0.10% STT rate**.
   - The "5 days at 0.10% + 14 days at 0.15%" claim in the report's Charge Rate Methodology
     section is misleading — those 5 days had no surviving trades. The whole sample is post-Apr-1.

   This is the most consequential hidden bias: the truth-up cannot say anything about
   pre-Apr-1 cost regime, which is the regime that 96 of the 200 walk-forward training days
   used. The wide_baseline FAIL was anchored in pre-Apr-1 cost regime data.

## Lot-size scaling reality check

The 1-lot PROVISIONAL PASS contradicts the 75-lot (= 1-lot in the operator's NIFTY context,
since NIFTY lot size is 75 contracts) FAIL in `wide_baseline_portfolio.md`. Reconciling them:

| Source | Sample | Net per lot at 75 contracts | Verdict |
|---|---|---|---|
| `wide_baseline_portfolio.md` | 200 days, ~800+ trades, Sep 2024 → Jun 2025 | **−₹82.45** per lot | FAIL |
| `fee_truthup_v1.md` | 4 days, 13 trades, Apr 20-24 2026 | **+₹558** per lot (mean), +₹207 (median) | PROVISIONAL PASS |

These are not conflicting facts about the same population — they are two sub-samples drawn
from very different regimes. The wide_baseline run included the mid-2025 vol-expansion period
that the operator's diagnosis (`§II.3` of `PHASE3_MASTER`) shows is the killer. The truth-up
window is five quiet trading days post-Apr-1. **The truth-up does not falsify the wide_baseline
FAIL — it samples a non-overlapping, more-favourable mini-window.**

The operator's logic seems to be: "wide_baseline used stale (lower) charge rates and still
FAILed; under correct (higher) charge rates the same data would FAIL by even more; so the
question becomes whether the strategy has ANY edge at all in any window, and the truth-up
shows yes in this window." That's defensible logic about cost-truth, but it does not justify
the PROVISIONAL PASS verdict. The PASS criterion in `§V.5` is about *whether the strategy is
profitable enough to merit Phase 3a optimisation* — not about whether you can find five days
where it made money. By the latter criterion, you could PASS any strategy.

## The "PROVISIONAL PASS" label specifically

`§V.5` defines exactly three verdicts: PASS, YELLOW, FAIL. The script invents PROVISIONAL
PASS at line 578-585 and applies it whenever `n_trades < 120` AND PASS criteria are met.
This is methodology iteration *during* the truth-up, which the operator's own discipline
rules in `§V.6.5` forbid: "No iterating on §5.5 cutoffs. Locked. Borderline → accept, don't
relax."

If the verdict is PASS by the locked criteria, declare PASS and accept the consequence (Phase
3a starts at full scope on this evidence). If the operator is uncomfortable doing that — and
they should be — the locked alternative is INSUFFICIENT_DATA, which the script also exposes
at line 501. Adding a third label to soften the discomfort is exactly the relaxation the
discipline rules were written to prevent.

Ironically, "INSUFFICIENT_DATA" is the most accurate label for what was actually measured
here.

## My independent verdict

**FAIL or INSUFFICIENT_DATA.** Specifically:

1. The headline number (+2.12 Sharpe) is statistically indistinguishable from zero given
   N=13 with within-day correlation. Confidence interval crosses the +0.5 PASS gate, the
   0 break-even line, and most of the way down toward FAIL.

2. The sample is biased to one half of the date-aware STT regime (post-Apr-1 only) and to
   five consecutive quiet trading days within that. The pre-Apr-1 portion of the chain window
   produced no analyzable trades because of contamination — that is a sampling problem, not
   evidence of edge.

3. Concentration in straddle_1 (and one trade in particular) means a single counterfactual
   tail loss flips the verdict. The wide_baseline analysis already identified the strategy's
   failure mode as fat losing tails; we have not seen one in this 5-day window.

4. The "PROVISIONAL PASS" label is not a locked verdict and was invented during the run.
   That alone should have triggered a FAIL or INSUFFICIENT_DATA call under `§V.5.5`.

5. The 5-lot Sharpe gate is mathematically trivial under the script's linear scaling
   (Sharpe(5×) = Sharpe(1×) by construction), so the multi-lot PASS was decorative, not
   substantive. The capacity FAIL in wide_baseline remains the binding finding at retail
   scale.

Most likely real-world outcome if the operator proceeds to Phase 3a on this verdict:
**Phase 3a will reproduce the wide_baseline FAIL with cleaner methodology.** The operator
will spend 36 worker-days (or $30 of cloud compute) optimising five strategies, find that
the optimisation surface is overfittable (PBO > 0.5) for at least 2-3 of them, and arrive
at exactly the same conclusion the audit-corrected wide validation already supports: this
specific architecture (short-vol premium-selling at retail scale post-Oct-2024 cost regime)
does not have a positive net edge that survives realistic costs. The PROVISIONAL PASS will
have cost the operator three months of calendar time to confirm what the prior FAIL already
implied.

## What I would do differently

1. **Re-issue the verdict as INSUFFICIENT_DATA, not PROVISIONAL PASS.** That is one of the
   four locked labels and it accurately describes the sample (13 trades, 4 days, ≤5 effective
   independent draws).

2. **Include backtest replay decisions but separate them.** Don't filter `_replay` rows out
   entirely — read them, re-cost them with the same date-aware charges, and report a
   **separate** "backtest cohort" net P&L for Mar 25 → Apr 17 (13 days, ~150 trades worth of
   replay decisions). This adds a much larger comparison sample, lets the Mar-30 rate change
   actually be exercised, and answers the cost-truth question — the original goal of the
   truth-up — without conflating it with paper-trade-decision quality. Document the replay
   contamination as a known caveat (these are backtest decisions, not real fills) but stop
   discarding 96% of the data because of an avoidable system-hygiene bug.

3. **Compute Sharpe two ways: per-trade-as-t-stat (as is) AND daily-aggregated annualised.**
   Then the gate is meaningful for both interpretations. With four trading days the daily
   annualised Sharpe is a small number times √252 — also non-stable, but at least it makes
   the small-N problem visible at gate-evaluation time.

4. **Bootstrap the verdict.** With 13 trades it's trivial to do 10,000 bootstrap resamples
   of the per-trade pnl, compute Sharpe each time, and report the 5th-95th percentile. If
   the 5th percentile of bootstrap Sharpe is below 0.5, the PASS gate is not met at 95%
   confidence — verdict is INSUFFICIENT_DATA or FAIL. The script already imports numpy;
   adding a 10-line bootstrap is half an hour of work.

5. **De-duplicate the 4 "concurrent" strategies into one daily decision before computing
   Sharpe.** When 4 strategy IDs all enter at the same minute on the same spot, treat that
   day as one decision (pick the best mode by score? sum the pnl? — methodology choice). The
   Sharpe should then be computed across 4 days, not 13 trades. This will deflate Sharpe to
   its honest value.

6. **Don't relabel as PROVISIONAL PASS.** Either commit to the PASS or declare
   INSUFFICIENT_DATA. The discipline rules forbid in-flight relaxation.

7. **Fix the underlying directory-pollution bug.** Backtest runs writing decisions into
   `data/decisions/` adjacent to live paper trades is the root cause of the contamination
   that destroyed 13 of 19 candidate days. Until that's fixed, every future truth-up is
   one overlooked filter away from this exact same problem.

8. **Re-cost the wide_baseline run with the corrected charges and post it alongside.** If
   wide_baseline FAILed at -₹82/lot under stale (lower) charge rates, the same data under
   the new 0.10% / 0.15% STT and 0.0353% exchange rates is going to FAIL by a wider margin.
   *That* is the truth-up the system needs — apply correct costs to the 800-trade sample,
   not measure an N=13 mini-window with correct costs.

## Bottom line for the operator

The PROVISIONAL PASS is not honest; the data does not support the PASS half of the label,
and "PROVISIONAL" is the operator inventing a softer category to avoid declaring
INSUFFICIENT_DATA or FAIL. The locked discipline rules at `§V.5.5` and `§V.6.5` of
`PHASE3_MASTER` were written specifically to prevent this kind of relaxation.

Do not commit 36 worker-days (or $30 of cloud compute) to Phase 3a on this evidence. Re-cost
the existing 200-day wide_baseline run with the now-correct charges and use that as the
truth-up. If even *that* makes a +Sharpe case at 1 lot, then the strategy is worth Phase 3a;
otherwise the operator's earlier "FAIL but cleanly" verdict in `§II.1` already gave the
honest answer.
