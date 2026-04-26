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

## 2. Strategies

### Alive — pending standalone validation on full 18-month corpus

| Strategy | Status | Last data |
|---|---|---|
| Iron Condor | **PASS standalone** in 200d wide_baseline subset (n=27, +1.06 Sharpe, +₹267/trade net) | Apr 26 2026 |
| Iron Butterfly | NEW — proposed addition (max ATM theta) | not yet validated |
| Long Calendar | NEW — proposed addition (positive vega diversifier) | not yet validated |
| NIFTY/BANKNIFTY relative-vol | OPTIONAL Phase 3+ (post-Nov-2024 dislocation) | not yet validated |

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

## 3. Open questions awaiting answers

1. **Standalone strategy validation** — does individual Strangle /
   Straddle / IC / Long Calendar / Iron Butterfly survive cost
   truth on the full 18-month corpus when run standalone (not as
   part of the combined Portfolio strategy)?

2. **Why does backtest Strangle (₹1.10/trade gross) diverge from
   live paper Strangle (₹257/trade gross)?** Three hypotheses:
   - GDFL parquet bid/ask is unrealistically wide on OTM strikes
   - Combined Portfolio strategy decision logic interferes with
     standalone Strangle conditions
   - Strategy params tuned in backtest are not what live runs

3. **Iron Condor PASS at n=27 — is it real?** 95% CI on mean is
   ₹-238 to ₹+772 (statistically borderline). Need n=120+ for
   confident inference. Full-corpus standalone backtest can give
   that sample.

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
| 2026-04-26 | Merge audit + parallel CPCV to FnO-v3; create FnO-v4 for strategy research | Audit fixes are correctness improvements safe for production | this commit |

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

**Phase 3a-revised: Per-strategy standalone validation on full corpus.**

For each strategy in [Strangle, IC, Straddle, Long Calendar,
Iron Butterfly]:
1. Run standalone backtest on Sep 2024 → Feb 2026 (370 days)
2. Compute net P&L with date-aware charges
3. Per-month breakdown
4. Per-VIX-bucket breakdown
5. Per-day-of-week breakdown
6. Standalone verdict (PASS / YELLOW / FAIL / INSUFFICIENT_DATA)

Expected runtime: ~5-7 days at 4 workers parallel CPCV per strategy
(or ~$30 cloud at c7i.4xlarge). Output: per-strategy verdict
matrix that informs the actual Phase 3 roster.

**Awaiting operator confirmation before launching.**

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

Last updated: 2026-04-26 (initial creation on FnO-v4-strategy-research)
