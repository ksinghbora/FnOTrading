# Project State — FnO-v4-strategy-research

> **Branch:** FnO-v4-strategy-research
> **Forked from:** FnO-v3 @ commit 84fe00b (Apr 25 audit merge)
> **Active since:** 2026-04-26
> **Production-stable branch:** FnO-v3 (live paper trading runs from this)
>
> This file is **branch-aware** — `git checkout` brings up the active
> branch's state automatically. The single source of truth for what
> the project is doing right now on this branch.

---

## 1. Current truth (verified, dated)

| What | Value | Verified |
|---|---|---|
| Validation harness | Trustworthy after 4 audit bug fixes | Apr 25 2026 ([memory/validation_audit_apr25.md](../../.claude/projects/-Users-kundanbora-Documents-FnOTrading/memory/validation_audit_apr25.md)) |
| Charge constants | Current Indian rates (STT 0.10%, Exchange 0.0353%) | Apr 25 2026 (verified via NSE FA64232, Union Budget 2024 + 2026) |
| Parallel CPCV | ~3.5× speedup, deterministic | Apr 25 2026 ([test_parallel_cpcv_determinism](../tests/integration/test_parallel_cpcv_determinism.py)) |
| Test suite | 858 passing (3 pre-existing replay_engine failures, unrelated) | Apr 26 2026 |
| Last validation verdict (200d wide_baseline) | FAIL aggregate; Iron Condor +1.06 standalone PASS | Apr 26 2026 ([recost_wide_baseline_v1.md](../reports/phase3_pre/recost_wide_baseline_v1.md)) |
| **Phase 3a-revised standalone validation** | **All 4 strategies FAIL gates** — iron_condor closest (6/2, mean Sharpe +2.30, DSR 0.998); strangle borderline (5/3); butterfly bimodal (3/5); straddle structurally broken (2/6) | Apr 26 2026 ([SUMMARY.md](../reports/standalone_v1/SUMMARY.md)) |
| **Validation perf branch merged** | Numba @njit on pricing/IV (5.8× component) + msgspec for Greeks (11×) + parallel walk-forward + round() audit. ~1.5–2× end-to-end wall-time speedup verified | Apr 26 2026 (commit 9a0c54f) |

## 2. Strategies

### Validated standalone Apr 26 2026 — all FAIL, ranked by closeness to PASS

| Strategy | Phase 3a-revised verdict | Mean CPCV Sharpe | DSR | Cost @+0.5× | Pass/Fail gates |
|---|---|---|---|---|---|
| **Iron Condor** | **FAIL but real edge — keeper hypothesis.** Misses only wf_decay (1.20) and wf_coverage (62%). | **+2.30** | **0.998** | **+0.50** | **6/2** |
| Short Strangle | FAIL — borderline. Misses cpcv_stability (0.48) and wf_coverage (62%) by tiny margins. | +0.43 | 0.031 | +0.26 | 5/3 |
| Iron Butterfly | FAIL — bimodal high-variance. ATM body fails cost sensitivity. | -0.34 | 0.000 | -0.05 | 3/5 |
| Short Straddle | FAIL — structurally broken. Loses at zero costs. | -3.23 | 0.000 | -0.23 | 2/6 |
| Long Calendar | NEW — proposed positive-vega diversifier | not yet implemented | — | — | — |
| NIFTY/BANKNIFTY relative-vol | OPTIONAL Phase 3+ (post-Nov-2024 dislocation) | not yet validated | — | — | — |

**Key finding:** Apr-Jun 2025 post-election regime is the killer window for short-vol — windows 4 & 5 of walk-forward show universal losses except iron_condor's wing protection (window 5 +8.08 Sharpe vs everyone else's negative).

### Investigating — needs standalone validation before judgment

The user's Apr 26 push-back surfaced a methodological gap: the
wide_baseline ran the COMBINED `portfolio_bt` strategy. Its per-mode
breakdown shows what happens *inside* that combined strategy, NOT
what each strategy would do standalone. Live paper trading shows
materially different per-trade gross than backtest:

- Live paper `strangle_1`: ₹257/trade gross
- Backtest `portfolio_bt` Strangle subset: ₹1.10/trade gross (200×
  difference suggests combined-strategy decision interference OR
  GDFL fill simulation issue)
- Live paper `straddle_1`: ₹1,735/trade gross (3 trades — small but
  high-edge)

| Strategy | Live paper gross/trade | Backtest gross/trade | Action |
|---|---|---|---|
| Strangle | ₹257 (n=3) | ₹1.10 (n=96) — suspect | Run standalone on full corpus |
| Straddle | ₹1,735 (n=3) | not in wide_baseline | Run standalone on full corpus |
| Iron Condor | ₹406 (n=3) | ₹287 (n=27) — consistent | Already viable; revalidate at scale |
| portfolio_1 (combined) | ₹107 (n=4) | — | Compare against standalone IC |

### Retired — won't be in Phase 3a roster

- **Trend Debit Spread**: -1.16 Sharpe in wide_baseline. Loses in
  its own designed regime. Bug 3 (cross-day candles) explained part
  but not all of the loss.
- **Existing `delta_neutral.py`**: same vega sign as Iron Condor
  (short vol with futures hedge). Not a true diversifier. Reviewer-
  recommended retirement.

## 3. Open questions awaiting answers (Phase 3b research priorities)

1. **Cost-model audit (HIGHEST ROI).** Phase 3a-revised confirmed the
   200× backtest-vs-live divergence on Strangle isn't combined-strategy
   suppression — standalone strangle backtest shows ~₹50–100/trade
   gross, between the suspect ₹1.10 (combined) and live paper's ₹257.
   So **the combined portfolio WAS suppressing entries (0.4× standalone)
   AND the cost model is over-pessimistic by 3–5× vs real broker fills**.
   Reconcile GDFL parquet bid/ask spreads against the n=3 live paper
   trades. If costs are over-modeled by 2–3×, multiple strategies flip.

2. **Regime gate for iron_condor.** Phase 3a-revised showed iron_condor
   PASSES cpcv_stability + DSR + cost_sensitivity + regime, but FAILS
   wf_decay (1.20) + wf_coverage (62%, needs 70%). The 3 losing WF
   windows are (a) Feb-Apr 2025 small n=12, (b) Apr-May 2025 high vol
   regime, (c) Jun-Jul 2025 zero entries (VIX out of band). A
   VIX-of-VIX or realized-vol-vs-implied gate that disables entries
   in regime (b) + an entry-band fix for (c) might flip wf_coverage
   to 75-87% and clear the gate. **Pre-register before retesting.**

3. **Capacity-aware position sizing.** Even iron_condor (best) and
   strangle (next best) decline from +87/+71 ₹/lot at 75 lots to
   +20/+25 at 1500 lots — slippage destroys 70-77% of edge at scale.
   Real-money cap is ~300-750 lots before edge dissolves. Need
   capacity-aware sizing algorithm that clamps lot count to where
   slippage stays below per-lot edge.

4. **System-hygiene Bug 5 candidate** — `data/decisions/` directory
   mixes live and backtest decision rows because the `DecisionLogger`
   uses a shared default path. Truth-up scripts handle it via
   strategy_id filtering, but production hazard remains. Fix:
   route backtest to `data/decisions_backtest/` via the existing
   `output_dir` parameter.

## 4. Discipline rules in force (carried from PHASE3_MASTER §VII)

1. **Never tune parameters by looking at validation set output.**
2. **Holdouts are single-access.** Tweaks require new holdout window.
3. **No outcome features in regime labeling.**
4. **PBO > 0.5 → reject** (López de Prado canonical).
5. **Coarse grids over fine grids** (4 values per param, not 9).
6. **One change per validation run.**
7. **No re-running holdout to "see if a tweak helps."**
8. **Independent auditor pass before each phase declares done.**

## 5. Recent decisions log

| Date | Decision | Rationale | File |
|---|---|---|---|
| 2026-04-25 | Audit harness — found 4 bugs, fixed all | Validation reports were systematically biased | [validation_audit_apr25.md](../../.claude/projects/-Users-kundanbora-Documents-FnOTrading/memory/validation_audit_apr25.md) |
| 2026-04-25 | Update charge constants (STT 0.0625→0.10, Exchange 0.05→0.0353) | Matched current Indian rates; older rates underreported costs | [src/core/constants.py](../src/core/constants.py) |
| 2026-04-25 | P1.5 regime gate disabled by default | Net-negative on 82-day window; infrastructure retained for future | [memory/p15_regime_gate.md](../../.claude/projects/-Users-kundanbora-Documents-FnOTrading/memory/p15_regime_gate.md) |
| 2026-04-26 | INSUFFICIENT_DATA on chain-window v0 truth-up | Effective N≤5 due to correlated strategies | [reports/phase3_pre/independent_review_v1.md](../reports/phase3_pre/independent_review_v1.md) |
| 2026-04-26 | FAIL aggregate on wide-baseline re-cost; IC standalone PASS | 211 trades with date-aware charges | [reports/phase3_pre/recost_wide_baseline_v1.md](../reports/phase3_pre/recost_wide_baseline_v1.md) |
| 2026-04-26 | Merge audit + parallel CPCV to FnO-v3; create FnO-v4 for strategy research | Audit fixes are correctness improvements safe for production | dff8ff8 etc. |
| 2026-04-26 | Phase 3a-revised: all 4 strategies FAIL standalone gates; iron_condor identified as keeper | First proper standalone CPCV+WF on full Sep24-Jul25 corpus across 4 strategies; failure modes correctly differentiated by harness | [reports/standalone_v1/SUMMARY.md](../reports/standalone_v1/SUMMARY.md) |
| 2026-04-26 | Validation perf optimizations merged (Numba+msgspec+parallel WF+round audit) | ~1.5–2× end-to-end speedup verified; 5.8×/11× on hot pricing/Greeks; tests pass; determinism preserved | commit 9a0c54f |
| 2026-04-26 | Mac Pro project moved from `iCloud Drive (Archive)/` back to `~/Documents/FnOTrading` | iCloud Drive disabled by user; venv rebuilt at new path; LaunchAgent installed for caffeinate | this update |

## 6. DO NOT REVERSE these decisions without explicit operator approval

1. The 4 validation harness bugs are real bugs, fixed and tested.
   Do NOT revert any of:
   - regime.py ENTER+EXIT pairing
   - engine.py explicit `days` parameter
   - aggregator.py `clear_day()`
   - decision_logger.py `FNO_DISABLE_DECISIONS` env var

2. Charge rates in `src/core/constants.py` reflect Apr 26 2026
   reality. Do NOT revert without verifying current Indian budget
   memorandum + NSE circulars.

3. The wide_baseline aggregate FAIL is per-COMBINED-strategy. It
   does NOT prove standalone strategies fail. Do NOT use the
   aggregate to retire individual strategies — each needs its own
   standalone validation on the full 18-month corpus.

4. P1.5 regime gate stays disabled by default until a new
   validation cycle demonstrates net-positive impact.

## 7. Next concrete action (proposed)

**Phase 3b research — three workstreams in priority order:**

1. **Cost-model audit** (highest ROI). Compare GDFL parquet bid/ask
   spreads on the 3 live paper Strangle trades vs the actual broker
   fills. Quantify the over-pessimism factor. If 2-3×, justifies
   re-running standalone validation on a corrected cost model.

2. **Iron Condor regime gate research.** Analyze WF windows 4-5
   (Apr-Jun 2025 vol spike) — what regime indicator (VIX-of-VIX,
   realized-vs-implied, term-structure inversion) would have flagged
   "skip entries"? Propose specific gate, pre-register hypothesis,
   then validate on FRESH window (don't reuse Phase 3a-revised's
   train+val).

3. **Capacity-aware sizing algorithm.** Design lot-size clamper
   based on slippage curve. Validate on iron_condor's capacity table.

Phase 3a-revised standalone validation infra now runs ~1.5-2× faster
thanks to Numba+msgspec+parallel WF — ~6h end-to-end on a 4-strategy
batch with the 2-machine setup. **Holdout (Aug 2025 → Feb 2026) is
preserved per discipline §VII.2 — burn ONCE only after a strategy
clears all gates on a fresh validation.**

## 8. Branch references

| Branch | Role |
|---|---|
| `FnO-v3` | **Production-stable.** Running live paper trades. |
| `FnO-v3-validation-harness` | Audit + tooling (now merged into FnO-v3). |
| `FnO-v3-quant-integration` | Earlier feature branch. |
| `FnO-v4-strategy-research` | **This branch.** Strategy research, standalone validation, regime detector design. |

## 9. Files that drift between branches

These should differ by branch-active design:

- **`docs/PROJECT_STATE.md`** (this file) — branch-specific living state
- **`src/strategy/params.py`** — strategy roster + per-strategy params
- **`src/strategy/implementations/portfolio_strategy.py`** — combined-strategy logic
- (any new strategy implementations added on this branch)

These should NOT drift between branches (audit fixes apply universally):

- `src/backtest/` — engine + validation harness
- `src/portfolio/charges.py` — charge calculation
- `src/core/constants.py` — charge rates
- `src/market_data/aggregator.py` — OHLC aggregation
- `src/strategy/decision_logger.py` — decision CSV writer

If a fix to one of the "should not drift" files lands here, it
should also land on FnO-v3 (cherry-pick or PR merge).

## 10. Updates

Updated atomically on every significant decision. The file is the
canonical living dashboard for this branch. All other memory and
docs files become historical archive once superseded — they are
read-only after their creation date unless explicitly amended.

Last updated: 2026-04-26 23:30 IST (post Phase 3a-revised + perf merge + Pro project relocation)
