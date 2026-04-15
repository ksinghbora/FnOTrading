# Strategy Versioning, Deployment & Rollback Plan

## Core Principle
**Every change must be reversible in under 60 seconds. No restart needed.**

---

## 1. Three-Layer Configuration

### Layer 1: Code (git branches)
- Strategy logic, entry/exit conditions
- Normal git workflow: `main` (live) -> `develop` (staging) -> `feature/*`
- Changes require full test pipeline

### Layer 2: Parameter Versions (YAML files, git-tracked)

```
config/
  params/
    portfolio_v1.yaml          # Original params
    portfolio_v2.yaml          # Tighter SL experiment
    portfolio_v3.yaml          # Current production
    active.yaml                # Points to current version
  history/
    param_changes.jsonl        # Append-only audit trail
```

Each file includes metadata:
```yaml
_meta:
  version: 3
  created: "2026-04-02"
  reason: "Tighter premium SL after 5-day paper results"
  parent_version: 2
  backtest_sharpe: 2.8
  paper_days_tested: 10
  promoted_from: "shadow"

portfolio:
  premium_stop_loss_pct: 25.0
  premium_trail_stop_pct: 10.0
  premium_profit_target_pct: 12.0
  # ... all params
```

### Layer 3: Runtime Overrides (hot-reloadable, NOT in git)
```
config/runtime_overrides.yaml    # Applied without restart
```
- File watcher detects changes every 1 second
- Validates before applying — keeps last-known-good if invalid
- Every override logged to `param_changes.jsonl`

---

## 2. Promotion Pipeline

Every change goes through these stages. **Never skip a stage.**

```
BACKTEST -> SHADOW -> PAPER -> CANARY (1 lot) -> FULL LIVE
```

### Stage Gates

| Stage | Min Duration | Gate Criteria |
|-------|-------------|---------------|
| **Backtest** | 60+ OOS days | Sharpe > 1.5, MaxDD < 20K |
| **Shadow** | 5 trading days | Shadow P&L >= Live P&L, MaxDD not 20% worse |
| **Paper** | 10 trading days | P&L matches backtest within 60% discount, 0 operational errors |
| **Canary** | 3 trading days | Max 1 lot, day loss < 5K, slippage < 2% |
| **Full Live** | Ongoing | All metrics sustained |

### Shadow Mode Implementation

New params run alongside live — same ticks, no real orders:
```
Live strategy:   portfolio_v3 (produces real orders)
Shadow strategy: portfolio_v4 (logs hypothetical orders only)
```

Both process identical ticks. Daily comparison report via Telegram:
```
Shadow v4 vs Live v3 (Day 3/5):
  Shadow P&L: +2,400  |  Live P&L: +1,800
  Shadow Sharpe: 3.1   |  Live Sharpe: 2.4
  Status: SHADOW LEADING — continue monitoring
```

---

## 3. Risk Classification

### Class 1 — Low Risk (auto-promotable)
- Logging changes, dashboard UI, non-functional refactors
- Gate: CI tests pass

### Class 2 — Medium Risk (requires shadow)
- Parameter changes within ±20% of current values
- Adding a filter that can only BLOCK trades
- Gate: 5 days shadow + comparison report

### Class 3 — High Risk (requires full pipeline)
- New strategy logic, new entry/exit conditions
- Parameter changes > 20% from current
- Removing safety filters, increasing position size
- Gate: Full pipeline (backtest -> shadow -> paper -> canary)

---

## 4. Rollback Mechanisms

### Automatic Triggers (no human needed)

| Trigger | Action |
|---------|--------|
| Day loss > max_day_loss | Kill switch + revert to last-known-good |
| 3 consecutive losing days after param change | Auto-revert + Telegram alert |
| Single day loss > 2x historical avg day loss | Auto-revert + Telegram alert |
| Fill price > 5% from expected | Halt strategy + alert |

### Manual Rollback (one command)
```bash
# Via CLI
python -m src.deployment.rollback --strategy portfolio --to-version 2

# Via API
curl -X POST localhost:8000/api/deployment/rollback \
  -d '{"strategy": "portfolio", "version": 2, "reason": "v3 SL too tight"}'
```

What happens:
1. Load `config/params/portfolio_v2.yaml`
2. Validate against Pydantic schema
3. Hot-swap on running strategy (no restart)
4. Log to `param_changes.jsonl`
5. Telegram alert: "ROLLBACK: portfolio v3 -> v2"
6. Reset P&L tracking for new epoch

### Last-Known-Good
Always maintain a pointer to the last profitable params (5+ consecutive days):
```
config/params/last_known_good.yaml -> portfolio_v2.yaml
```
Auto-rollback always targets this — never an untested version.

---

## 5. One Change at a Time

**Rule: Change one parameter per version.**

Bad:
```
v3: Changed SL 30->25 AND trail 15->10 AND PT 15->12 (can't isolate which helped)
```

Good:
```
v3: Changed SL 30->25 (shadow 5 days, promoted)
v4: Changed trail 15->10 (shadow 5 days, promoted)
v5: Changed PT 15->12 (shadow 5 days, promoted)
```

If v4 degrades, roll back to v3 — keep SL change, revert trail.

### When You Must Bundle Changes

Tag parameter groups:
```yaml
_meta:
  changes:
    - group: "exit_tightening"
      params: ["premium_stop_loss_pct", "premium_trail_stop_pct"]
    - group: "entry_widening"
      params: ["premium_call_delta", "premium_put_delta"]
```

Partial rollback:
```bash
python -m src.deployment.rollback --strategy portfolio \
  --rollback-group entry_widening \
  --keep-group exit_tightening
```

---

## 6. Performance Comparison Report

Generated automatically after every param change:

```
=== Parameter Change Report: portfolio v2 -> v3 ===
Change: premium_stop_loss_pct 30.0 -> 25.0
Period: 2026-03-20 to 2026-03-25 (5 days)

                          v2 (old)    v3 (new)    Delta
─────────────────────────────────────────────────────────
Total P&L                 +4,200      +3,800      -400
Win Rate                  60%         80%         +20%
Avg Win                   +1,400      +950        -450
Avg Loss                  -1,200      -800        +400
Max Drawdown              -2,100      -1,500      +600 ✓
Sharpe                    2.1         2.8         +0.7 ✓

Verdict: PROMOTE (Sharpe improved, MaxDD improved)
```

Recommendation logic:
- Sharpe improved AND MaxDD not 20% worse → **PROMOTE**
- Sharpe < 80% of old OR MaxDD > 150% of old → **ROLLBACK**
- Otherwise → **CONTINUE TESTING**

---

## 7. Pre-Promotion Checklist

Before any change goes live:

```
[ ] Backtest: Sharpe > 1.5 on OOS data (60+ days)
[ ] Shadow: 5+ days, P&L >= live P&L
[ ] Paper: 10+ days, no operational errors
[ ] Comparison report generated and reviewed
[ ] Change logged to param_changes.jsonl with reason
[ ] Previous version archived for rollback
[ ] Kill switch tested with new params
[ ] Telegram alerts configured
[ ] Hot-swap tested (can revert without restart?)
[ ] Position size starts at 1 lot for first 3 live days
[ ] Tested against 3 worst historical days
[ ] Max position can't exceed limits with new params
```

---

## 8. Implementation Priority

| Task | Effort | Priority |
|------|--------|----------|
| Create `config/params/` with YAML versioning | 2 hrs | Week 1 |
| Add `param_changes.jsonl` audit trail | 1 hr | Week 1 |
| Hot-reload file watcher for runtime overrides | 3 hrs | Week 1 |
| Rollback API endpoint + CLI command | 3 hrs | Week 2 |
| Shadow strategy runner (dual execution) | 4 hrs | Week 2 |
| Comparison report generator | 3 hrs | Week 3 |
| Auto-rollback triggers (3 losing days) | 2 hrs | Week 3 |
| Promotion gates with blocking | 2 hrs | Week 4 |
| Telegram deployment notifications | 1 hr | Week 4 |

**Total: ~21 hours over 4 weeks**

---

## 9. Key Principles

1. **Parameters are data, not code.** Version them separately with metadata.
2. **Pipeline: backtest -> shadow -> paper -> canary -> live.** Never skip.
3. **Rollback must be instant.** One command, no restart.
4. **Prove it won't misbehave**, not that it will profit.
5. **One change at a time.** Tag groups if you must bundle.
6. **Always maintain last-known-good.** Auto-rollback targets this.
7. **Log everything.** Every change, every comparison, every rollback.
8. **The 60% discount is real.** Never promote on backtest P&L alone.
