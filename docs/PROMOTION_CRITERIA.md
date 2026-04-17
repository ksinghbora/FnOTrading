# Strategy Promotion Criteria

**Owner:** Kundan Bora
**Last reviewed:** 2026-04-17
**Why this exists:** Apr 17 trader analysis flagged that we have no formal gate between paper-validated and live-capital strategies. Past pattern: a single profitable backtest week → enable in live → loss. This doc defines the four gates a strategy must pass and the minimum sample sizes that make promotion decisions statistically defensible.

## Promotion ladder

```
Backtest          Shadow paper      Live paper         Live capital
(synthetic)  →   (no orders)   →   (paper orders)  →  (real money)
                                                       ─ small ─ scale
```

Each rung has a different bar. **Never skip a rung.** A strategy that backtests well but has never run shadow has zero evidence of working under real ticks.

---

## Gate 1 — Backtest → Shadow paper

**Purpose:** rule out clearly broken strategies before they touch the live event loop.

**Required:**
- ≥ 60 backtest days on real data (`historical_engine` with chain replay or BS engine — flag which).
- Sharpe ≥ 1.5 on the **net-of-charges** P&L (BS engine over-estimates P&L by ~62%, so apply 0.6x discount when using BS).
- Max drawdown ≤ 1.5x average month P&L.
- Win rate ≥ 30% (premium sellers naturally land here; trend strategies can be lower).
- No single day P&L > 5x median absolute daily P&L (concentration check — if one day made the month, strategy is fragile).
- Backtest config committed to git; rerun with `uv run python scripts/run_backtest.py --strategy <name> --days 60` reproduces within ±5%.

**Sample-size rule:** below 60 days, observed Sharpe is dominated by sampling noise. Don't promote on 30 days no matter how good the number looks.

---

## Gate 2 — Shadow paper → Live paper

**Purpose:** confirm the strategy survives real ticks (not synthetic ones), real chain pricing, and real microstructure.

**Required:**
- ≥ 30 trading days running in shadow mode (signals computed and logged but no orders sent).
- Shadow signal count within 50%-200% of backtest expectation. If backtest expects 1.5 entries/day and shadow shows 0.2/day, the live filters are killing more than expected — investigate before promoting.
- Signal quality logs (`[ENTRY_QUALITY]` tag) show **no missing data** on >80% of bars (gaps usually mean broker WS dropped or chain builder hadn't seeded).
- Latency: 95p tick→signal latency < 200 ms.
- Run `scripts/weekly_review.py` for each of the last 4 weeks; no week shows shadow signal count == 0.

---

## Gate 3 — Live paper → Live capital (small)

**Purpose:** confirm orders fill, charges land, P&L matches expectation. This is the **highest-failure rung** historically — most surprises happen here.

**Required:**
- ≥ 60 paper-trading days with real orders (`paper_trading=true`, `USE_LIVE_DATA=true`) post-shadow.
- Paper-net P&L positive on rolling 60d window. **Net of slippage AND charges** — gross P&L is not a promotion metric.
- Max single-day loss within `max_strategy_loss` config (default ₹5,000); no `[CIRCUIT_BREAKER]` trigger on the strategy.
- Paper-vs-shadow P&L tracking error < 30% (if paper says +5,000 and shadow predicted +20,000, there's a fill model gap).
- Slippage stat: median paper slippage_bps < 20 for premium sellers, < 50 for trend (both at default position size). If higher, bump to live with smaller initial capital.
- All `live_lessons_learned.md` items relevant to this strategy class addressed.

**Live capital rules:**
- Start at **10% of intended capital**. Hold there for 20 trading days.
- Promote to 50% only if rolling 20d net P&L is positive AND realised tracking error vs paper < 25%.
- Promote to 100% only after another 20 days at 50%.
- **Auto-demote on:** any day where realised loss > 1.5x configured max_strategy_loss; 3-day rolling Sharpe < -1; reconciliation discrepancy count > 0.

---

## Gate 4 — Continuous monitoring (after live)

**Required (runs every Monday morning before market open):**
- `scripts/weekly_review.py` — produces the weekly scorecard
- Compare current week's net P&L to trailing 4-week median. If current < 0.5x trailing, flag for review.
- Compare current week's slippage to trailing 4-week median. If current > 1.5x trailing, broker liquidity changed — investigate before next entry.
- Confirm `[RECONCILE]` tag shows zero discrepancies all week.

**Demote triggers (no debate, just demote):**
- 5 consecutive losing days at any capital tier
- One realised drawdown > 2x `max_day_loss`
- Reconciliation discrepancy persists > 24h
- Any unhandled exception that did not invoke `_emergency_close_positions`

---

## Reading the weekly_review output

`scripts/weekly_review.py` produces a markdown report with one section per strategy. Each section has:

| Field | What "good" looks like | What it means if bad |
|---|---|---|
| `entries` | within 50-200% of backtest expectation | filters too tight (low) or too loose (high) |
| `fills` | should equal `entries × legs_per_signal` | partial fills → broker liquidity gap |
| `net_pnl` | positive over 7d for promoted strategies | demotion candidate |
| `gross_pnl - net_pnl` | < 30% of gross | charges eating the strategy — re-tune |
| `median_slippage_bps` | < 20 (premium) / < 50 (trend) | broker microstructure issue |
| `circuit_breaker_trips` | 0 | risk system did its job; investigate root cause |
| `reconcile_discrepancies` | 0 | broker/local divergence — STOP TRADING and reconcile |

---

## Decision worksheet

Before promoting (or demoting) a strategy, fill this out and commit to memory:

```
strategy_id: ____________
current_tier: backtest | shadow | live_paper | live_10pct | live_50pct | live_100pct
proposed_tier: ____________
sample_size_days: ____   (must meet rung minimum)
trailing_window_pnl: ___________
trailing_window_sharpe: ___________
max_drawdown: ___________
slippage_median_bps: ___________
reconcile_discrepancies_in_window: ___________
unaddressed_lessons_learned_items: [_______]
go/no-go: ____________
reviewer: ____________
date: ____________
```
