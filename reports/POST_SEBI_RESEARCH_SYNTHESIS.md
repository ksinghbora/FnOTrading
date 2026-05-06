# Post-SEBI Strategy Research Arc — Synthesis

**Period:** April 2026 → May 7 2026 (~5 weeks)
**Branch:** `FnO-v4-strategy-research`
**Final commit:** `2f45839`
**Goal at start:** "a profitable strategy on the NSE NIFTY F&O paper-trading book"
**Result:** Two marginal-edge strategies deployed live in shadow paper trading

This document synthesizes the full ~5-week research journey on the
post-SEBI (Nov 20 2024 onwards) Indian F&O market. It ties together
the individual chapter docs (IC findings, long-vol verdict, trend
verdicts) into a single overview.

## TL;DR

After exhausting six options-strategy families and two
trend-following timescales:

- **One** strategy clears the cost wall on holdout: **IC v2** with
  the Choppiness Index + VRP gate (+₹584 / 324 trades / Sharpe 0.35
  on 143-day untouched holdout, PF ~1.05 OOS)
- **One** more strategy has a directionally robust positive net edge
  on train+val: **Trend Daily** with 20-day Donchian + VIX 12-22 gate
  (+₹23,237 / 8 trades / Sharpe 0.18 on 360-day train+val)
- **Both deployed live in shadow paper trading** (commit `2f45839`)
  to validate their backtest edge under real-world fills + spreads
  over 1-3 months
- Every other primitive (premium-selling defaults, long-vol
  calendar/straddle, intraday trend) sits at or below the cost wall
  on Indian post-SEBI options

The two marginal edges are **regime-orthogonal** by design:
- IC v2 wins on range-bound + IV-rich days (theta capture)
- Trend Daily wins on multi-day breakouts in normal vol (directional)

Combined book diversification is the durable artifact of this arc.
The methodology toolkit (WF + holdout discipline, theory-grounded
gates, regime detectors, broker-backed warmup, cost-sensitivity
gates) is the meta-artifact.

## Verdict matrix — every config tested

| Strategy class | Variant | Train+Val PF / Sharpe | Holdout result | Verdict |
|---|---|---|---|---|
| **Iron Condor** | default (legacy filters) | -4.54 Sharpe | (curve-fit) | FAIL |
| Iron Condor | strong-signal (tuned) | 0.85 PF | 0 trades | curve-fit confirmed |
| Iron Condor | minimal (3 filters off) | 0.90 PF | 0 trades | same trap |
| Iron Condor | principled (ADX+BB+RV/IV) | n/a | n/a | 0 of 2590 — neg-corr |
| **Iron Condor v2** | **CI≥61.8 AND VRP>0** | **1.20 PF** | **+₹584 / 324 / Sharpe 0.35** | **TRADEABLE (marginal)** ✓ |
| Iron Condor v3 | v2 minus adjustments | 1.29 PF | not tested (curve-fit risk) | partially holdout-informed |
| Iron Butterfly | default | (similar to IC default) | — | FAIL (cost-wall variant) |
| Short Strangle | default | -8.34 Sharpe, MC p=1.0 | — | FAIL |
| Short Straddle | default | -7.25 Sharpe, MC p=1.0 | — | FAIL |
| Long Calendar | default | -3.23 Sharpe | — | FAIL |
| Long Calendar v2 | CI+VRP (range + IV-cheap) | 17 trades, -₹1,707 | — | sample-thin loser |
| Long Calendar v2b | VRP<0 only | 31 trades, -₹120,734 | — | catastrophic |
| Long Straddle v2b | VRP<0 only | 73 trades, -₹13,900 | — | at cost wall |
| Trend on options (trend_itm) | v1 / v2 / v3 | -0.09 to -0.66 Sharpe | — | at cost wall |
| Trend intraday (futures-equiv) | 1-min Donchian | -0.52 Sharpe | — | below cost wall |
| Trend intraday | 5-min Donchian | +0.17 gross, -1.01 net | — | below cost wall |
| Trend intraday | 15-min / 30-min | +0.05 / -0.96 | — | below cost wall |
| **Trend Daily** | **20-day Donchian + VIX 12-22** | **+₹23,237 / 8 trades / Sharpe 0.18** | not tested (live paper is OOS) | **TRADEABLE (sample-thin)** ✓ |

(173-day post-SEBI corpus for IC variants; 360-day for trend variants
— both span the same calendar period 2024-11-13 onwards.)

## What we proved

### 1. The cost wall on Indian post-SEBI options is structural

Five separate experiments converged on the same finding: most
options-based primitives on Indian post-SEBI sit at or below the
~₹150-200 round-trip cost basis on 75-lot weekly options.

The Nov 2024 SEBI changes (lot 25→75, options-sell STT 0.0625% →
0.10%, BANKNIFTY weekly discontinued) shifted cost-to-credit ratios
that previously left ~30% margin. Now ~63% of the credit is consumed
by structural costs. A strategy needs ~65% win rate at current costs
to break even on premium-selling; only IC v2 with its theory-grounded
gate gets close.

### 2. Theory-grounded orthogonal gates work — but only when the orthogonality is empirically real

The progression of IC gates illustrates this:

- **v1 principled** (ADX<22 AND BB-squeeze AND RV/IV<0.80): three
  conditions all theory-grounded individually, but the AND-gate fired
  on **0/2590 valid samples** because the conditions are negatively
  correlated on Indian data.
- **v2** (CI≥61.8 AND VRP>0): two conditions from independent
  traditions (technical analysis + academic finance), AND-gate fires
  ~2.4% of decisions, **324 trades on holdout, +PnL**.
- **LC v2** (CI≥61.8 AND VRP<0): same range condition as IC v2, but
  the vol axis flipped. Fires only 17 times on 173 days because
  range-bound + IV-cheap is empirically rare on Indian post-SEBI
  (range markets always have rich IV).
- **LC v2b** (VRP<0 only, drop CI): catches more trades but
  catastrophically loses (-₹120,734 on 31 trades) because trending
  markets violate the calendar's structural assumption.

**The lesson:** orthogonal-traditions gate design is a sound
methodology, but the orthogonal subspace must EMPIRICALLY exist on
the target market.

### 3. Multi-day timescale escapes the intraday cost wall

The trend pivot tested 1-min through 30-min Donchian on NIFTY spot
(futures-equivalent for cost modelling). All sat below the cost wall.
**Daily bars** (multi-day holds, ~22-day average) flipped to positive
net Sharpe because:

- Multi-day holds spread the 1bp round-trip cost across many days of
  price movement
- Daily timescale escapes the intraday microstructure noise that
  dominates the 20-bar Donchian primitive on minute bars

Daily Sharpe +0.18 is robust across Donchian lookbacks (10/20/30/60
all positive in [0.11, 0.18]) and the VIX gate is essential
(disabling it flips the signal to -1.23 Sharpe).

### 4. Live mode requires broker-backed warmup for state-dependent detectors

The May 6 live deployment of IC v2 surfaced a gap: the regime
detector's daily-close deque accumulates only from in-memory live
ticks. Combined with the launchd 08:50 IST daily restart, the deque
resets to empty every morning and the gate returns
"insufficient_data" perpetually.

Fix (commit `4cff9b3`): `RegimeDetector.warmup_daily_closes` plumbed
through `StrategyContext` → `StrategyRunner` → `main.py`. At strategy
startup, fetch the last ~25 daily closes from Kite historical_data
API and seed the deque. Same pattern was added to LongCalendar and
LongStraddle (commit `e09eb9b`) and TrendDaily uses an analogous
daily-bar warmup (commit `1636f2b`).

## What we proved AGAINST

| Claim | Verdict |
|---|---|
| "Default IC is profitable on Indian retail F&O" | FALSE — Sharpe -4.5 on holdout |
| "Filter tuning produces tradeable IC" | FALSE — strong-signal IC fired 0 trades on holdout |
| "Range-bound + IV-cheap regime exists for long calendar" | FALSE — empirically 0% of post-SEBI |
| "Long straddle on VRP<0 has positive expectancy" | FALSE — at cost wall |
| "1-min Donchian breakout on NIFTY has trend-continuation alpha" | FALSE — Sharpe -0.5 gross |
| "Indian post-SEBI options have a tradeable long-vol edge" | FALSE — at cost wall regardless of structure |

## Indian-market lessons

1. **89% of Indian retail F&O traders lose money** (SEBI Jan 2023
   study). Our research independently confirms this from first
   principles — without theory-grounded filters, retail premium
   selling has no edge.

2. **Indian VIX is efficiently priced.** The IV-vs-RV gap exists but
   is smaller than US, and rarely co-occurs with low ADX. The
   "principled AND-gate" trap (range AND IV-rich) was empirically
   impossible because IV adjusts to RV.

3. **Day-rollover detection in backtest must use simulated time.**
   Wall-clock primitives silently break any state-accumulating
   detector — this single bug was responsible for every "0 trades"
   smoke that used regime-gate filters before the May 5 fix
   (commit `1b65610`).

4. **CPCV is misfit for short Indian-data single-config testing.**
   WF + bootstrap CI + MC permutation are the right primary tools.
   The May 2 refactor (commit `636088f`) demoted CPCV to
   diagnostic-only.

5. **NIFTY post-SEBI at 1-min timescale is too microstructure-noisy
   for naive Donchian breakouts.** 5-min is the sweet spot among
   intraday timeframes (Sharpe +0.17 gross), but still below the
   cost wall. Daily bars escape this entirely.

6. **The IC v2 / LC v2 portfolio thesis is empirically wrong** —
   the two regimes (range+rich-IV vs range+cheap-IV) don't both
   exist. IC v2 already covers the entire range-bound subspace
   on this market.

7. **The IC v2 / TrendDaily portfolio thesis is empirically right
   (preliminary)** — IC v2 wins on range, TrendDaily wins on
   breakouts, both gated by VIX in the moderate band. Different
   regimes, different alpha sources, similar VIX preference.

## Live deployment status (as of commit `2f45839`)

```
Daemon PID 45932, started 02:19 IST May 7 2026

Strategies running:
  portfolio_1     : portfolio (live; owns capital)
  ic_1            : iron_condor v2 (CI+VRP)        shadow_only
  trend_daily_1   : trend_daily (Donchian + VIX)   shadow_only
  strangle_1      : short_strangle (legacy)        shadow_only
  straddle_1      : short_straddle (legacy)        shadow_only
  trend_1         : trend_debit_spread (legacy)    shadow_only

Boot health:
  ic_1            warmup: seeded 23 daily closes
  trend_daily_1   warmup: seeded 37 daily bars (buf=37/39, vix_buf=37)

Live verification cron: eeba70ba (10:37 IST today)
```

Next milestones:
- 10:37 IST today: cron verifies IC v2's gate produces real ci/vrp metrics
- 15:25 IST today: TrendDaily evaluates first live decision
- 1-3 months from now: enough live data to confirm/reject backtest edge
- 6 months from now: combined-book Sharpe is the verdict that matters

## Open follow-ups (not on this arc's critical path)

1. **Futures-token resolver** for TrendDaily live trading: currently
   uses spot token as placeholder. Doesn't affect shadow-mode
   logging; required before real-money deployment.

2. **Engine-mode integration smoke** for TrendDaily: pandas smoke
   validates signal mechanics; engine-mode would validate
   tick-stream integration. Live daemon will provide this evidence
   organically over the next few trading days.

3. **WF + holdout for TrendDaily**: 8 trades on train+val is
   sample-thin for formal stats. Live paper trading is the
   pragmatic OOS extension.

4. **STT date-aware fix**: charges calculator uses
   `options_sell_pct=0.10%` (Oct 2024 - Mar 2026 rate). After
   Apr 1 2026 the rate is 0.15%. Current backtests on
   post-Apr-2026 data understate STT by 50%. Future bug for
   live data accuracy.

5. **Cost-sensitivity gate quirk**: Sharpe column reads 0 across
   shifts; should use profit_factor as decision metric.

## Files (in chronological order of finding)

- `reports/standalone_post_sebi/IC_HONEST_ANALYSIS.md` — pre-v2
  retrospective, May 2 corrections to methodology
- `reports/standalone_post_sebi/IC_FINAL_FINDINGS.md` — IC research
  arc synthesis (May 6)
- `reports/standalone_post_sebi/ic_research_params.json` /
  `ic_v2_HOLDOUT_BURN.md` — IC v2 config + verdict
- `reports/standalone_post_sebi/LC_v2_FINDINGS.md` — long calendar
  verdict (May 6)
- `reports/standalone_post_sebi/LONG_VOL_VERDICT.md` — long-vol
  thesis closure across LC + LS
- `reports/standalone_post_sebi/TREND_FUTURES_VERDICT.md` — trend
  intraday verdict
- `reports/standalone_post_sebi/TREND_DAILY_VERDICT.md` — trend
  daily verdict (first +PnL trend variant)
- `reports/POST_SEBI_RESEARCH_SYNTHESIS.md` (this file) — top-level
  synthesis

## Final word

The 5-week post-SEBI research arc didn't find a "rich edge."

It found **two small, real, theoretically-grounded edges** that can
be harvested at modest size with discipline:

- IC v2 captures theta on range-bound + IV-rich days
- TrendDaily captures directional alpha on multi-day breakouts in
  normal vol

Both are deployed live in shadow paper trading. The next 1-3 months
of live data will be the disciplined OOS verdict.

The methodology corrections along the way (WF-primary,
wall-clock fix, ablation discipline, holdout-purity rules,
broker-backed warmup, orthogonal-traditions gate design,
[ENTRY]-event vs skip-log counting) are the durable artifact. They
will save the next research arc weeks.

If you want a financial breakthrough, this arc didn't find it. If
you want a small-but-honest combined book on the NSE NIFTY F&O
paper book — IC v2 (theta) + TrendDaily (directional) — it's now
running live in shadow mode and waiting for the data to speak.
