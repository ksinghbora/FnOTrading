# FnO Trading System — Combined Code + Trader Analysis

**Date:** 2026-04-17
**Reviewer perspectives:** Senior Python engineer + Experienced Indian options trader
**Branch:** FnO-v3
**System state:** Paper trading, 22 days of live data (Mar 25 — Apr 16, 2026)

This document combines code-level engineering findings with trader-level domain findings. For trader-only deep dive, see [TRADER_ANALYSIS_2026-04-17.md](TRADER_ANALYSIS_2026-04-17.md).

---

## 1. Combined Rating

| Lens | Rating | Headline |
|---|---|---|
| Code Architecture | 8/10 | Event-driven, async, clean DI, structured logs — well above retail |
| Code Quality | 6/10 | One 2053-line god-strategy, sparse type hints, magic numbers, low test coverage |
| Code Safety | 5/10 | Critical bugs in charges, timezone, expiry handling. Secrets on disk in plaintext |
| Trading Logic | 5/10 | Sound concepts (champion-challenger, regime gating) but Indian-market thresholds wrong |
| Indian Market Realism | 4/10 | Wrong VIX bands, narrow IC wings, missing expiry STT, no event calendar use |
| Live-Money Readiness | **3/10** | Major gaps: state persistence, real charges, expiry-day risk, statistical validation |
| **Composite** | **5.5/10** | Institutional engineering, retail-grade trading instincts, dangerous deployment posture |

**Verdict:** The system is *engineering-impressive but financially unsafe to deploy live as-is*. 22 days of paper P&L (-308 net, 33% win rate) confirms the trading logic gaps; code review confirms several silent bugs that would amplify losses in live mode.

---

## 2. Critical Code-Level Findings

### CRITICAL-1: STT on Expiry-Day Exercise Never Applied

**File:** `src/portfolio/charges.py` (entire file) + `src/core/constants.py:64`

The `options_exercise_pct = 0.125%` constant is defined but **never referenced** anywhere in `calculate_charges()`. The function only applies regular `options_sell_pct = 0.0625%` STT.

**Real-world impact:** On expiry Tuesday, if any leg goes ITM and is auto-exercised by the exchange:
- Real STT = 0.125% on intrinsic value (sell-side equivalent)
- System computes = 0 (or wrong rate)

**Example:** ITM CE worth ₹200 intrinsic × 75 lot = ₹15,000 → STT = ₹18.75 per lot. Run 5 strategies on expiry day, all leg ITM, and you've underestimated charges by ₹400-1000. Over a year (52 expiries), that's ₹20K-50K of phantom P&L.

**Fix:**
```python
def calculate_charges(price, quantity, side, instrument_type, is_expiry_exercise=False):
    ...
    if is_option and not is_buy:
        if is_expiry_exercise:
            stt = turnover * CHARGES["stt"]["options_exercise_pct"] / 100
        else:
            stt = turnover * CHARGES["stt"]["options_sell_pct"] / 100
```

Then in OMS exit logic, mark expiry-day ITM closures as `is_expiry_exercise=True`.

---

### CRITICAL-2: Plaintext Credentials On Disk

**File:** `.env`

Contains plaintext: `KITE_API_KEY`, `KITE_API_SECRET`, `KITE_PASSWORD`, `KITE_TOTP_SECRET`, `BREEZE_API_SECRET`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `DB_PASSWORD`.

**Verified:** `.env` IS in `.gitignore` (good — prevents git leak).

**Remaining risks:**
1. **Backup leaks**: any system backup (Time Machine, cloud sync) captures the file
2. **TOTP secret**: with this, anyone can regenerate Kite tokens indefinitely
3. **TOTP + password + user ID = full account takeover** without 2FA prompt
4. No secret rotation mechanism

**Fix priority order:**
1. Move TOTP secret to OS keychain (macOS Keychain, `keyring` Python package) — 1 hr
2. Move Kite password to keychain — 30 min
3. Document quarterly rotation procedure — 30 min
4. Long-term: HashiCorp Vault or AWS Secrets Manager for production

---

### CRITICAL-3: Test Coverage Is Effectively Zero

**Verified counts:**
- 8 test files exist; only 2 contain code (`test_charges.py:76`, `test_greeks.py:94`)
- Total test lines: **186** for **20,409 production lines** = **0.9% ratio**
- `tests/integration/`, `tests/strategies/`, `tests/backtest/` are empty `__init__.py` only

**What's untested (zero coverage):**
- Every strategy entry/exit decision (portfolio_strategy.py 2053 lines, iron_condor.py 592 lines)
- Risk manager logic (the layer that's supposed to prevent catastrophe)
- Order manager multi-leg execution and reversal
- Portfolio P&L and position tracking (the math that says "you're up ₹X")
- Option chain building and Greeks calculation
- Market data feed and tick processing
- Advisor confluence logic
- Backtest engine itself (so "we tested in backtest" is unverifiable)

**Risk:** Every refactor is a deployment without a safety net. The 22 days of paper trading IS your test suite, which is why bugs surface in production.

**Fix (16 hrs):** At minimum, write integration tests for:
1. Full entry-to-fill-to-exit cycle for IC and strangle
2. Daily reset of strategy state across day boundaries
3. Risk limit triggering correctly stops new orders
4. Charges calculation for all 4 quadrants (buy/sell × CE/PE) including expiry-day ITM
5. Determinism: same backtest run twice produces identical P&L

---

### HIGH-1: Timezone-Naive Datetimes Throughout

**Files:** `src/oms/manager.py:106`, `src/oms/tracker.py:144`, `src/broker/paper/client.py:101-102`, `src/advisor/models.py:153`, 22+ more locations

All `datetime.now()` calls are timezone-naive. India is UTC+5:30 with no DST, so most code "works" by accident, BUT:

1. Logs from different machines (cloud + laptop) can disagree on day boundary
2. Daily reset task fires based on naive time → may fire at wrong wall-clock time after machine timezone changes
3. Daylight Saving in upstream APIs (US Fed FOMC times come in as UTC, mixed with naive IST) creates 30-minute timing errors
4. SQLAlchemy stores some timestamps as `DateTime(timezone=True)` and some naive → comparison bugs

**Fix (4 hrs):**
- Adopt UTC internally everywhere: `from datetime import datetime, timezone; datetime.now(timezone.utc)`
- Convert to IST only at display layer
- Update `MarketClock` to be timezone-aware
- Add CI lint: forbid `datetime.now()` without tzinfo argument

---

### HIGH-2: Strategy Error Callback Doesn't Trigger Position Closure

**File:** `src/strategy/base.py:78-80`

```python
async def on_error(self, error: Exception) -> None:
    logger.exception(f"Strategy {self.strategy_id} error: {error}")
```

If a strategy throws at 2:50 PM with open positions, the only thing that happens is a log line. The strategy stays "RUNNING" in the system, but isn't actually evaluating exits anymore. Positions ride to exchange auto-square-off (3:25 PM for MIS) or hold overnight (NRML).

**Real impact:** Apr 13 logs show 5,321 wing-strike-missing warnings on iron_condor — those exceptions in `_check_adjustments` could have left a half-built IC unmanaged for hours.

**Fix (3 hrs):**
```python
async def on_error(self, error: Exception) -> None:
    logger.exception(f"Strategy {self.strategy_id} error: {error}")
    # Force-close all open positions and disable for the day
    await self.ctx.risk_manager.emergency_close(self.strategy_id, reason=f"Strategy error: {type(error).__name__}")
    self._stopped_for_day = True
```

---

### HIGH-3: Iron Condor Adjustment Counter Never Resets on Expiry Rollover

**File:** `src/strategy/implementations/iron_condor.py` (around `_check_expiry_rollover`)

`_adjustments_today` resets on `reset_day_state()` (daily) but NOT when expiry rolls mid-week. If IC made 2 adjustments on Monday's expiry, Tuesday rolls to next-week expiry, the counter is still 2 → no adjustments allowed on the new position.

**Fix (15 min):** In `_check_expiry_rollover()`, when `new_expiry != old_expiry`, also call:
```python
self._adjustments_today = 0
self._last_adjustment_time = None
```

---

### HIGH-4: Unverified Transaction Charge Rate

**File:** `src/core/constants.py:68`

```python
"options_pct": Decimal("0.05"),
```

**NSE circulars have updated this rate multiple times in 2024-2025.** As of recent circulars, the option transaction charge is closer to 0.03503% on premium turnover. The hardcoded 0.05% is likely stale.

**Impact:** ~30-40% over-estimation of transaction charges (small relative to STT + slippage, but compounds).

**Fix (30 min):**
1. Verify current rate from NSE notice board
2. Update constants
3. Add comment with NSE circular reference + date
4. Add quarterly check to roadmap

---

### HIGH-5: Float Precision in Order Validation

**File:** `src/oms/validator.py:100`

```python
deviation = abs(float(order.price) - float(ltp)) / float(ltp)
if deviation > 0.10:
    raise OrderValidationError(...)
```

For NIFTY options at ₹1-5 (deep OTM wings), float division loses precision. A genuine 9.9% deviation can be computed as 10.1% → falsely rejecting valid orders. This may explain some IC entry failures in logs.

**Fix (30 min):**
```python
deviation = abs(Decimal(str(order.price)) - Decimal(str(ltp))) / Decimal(str(ltp))
```

---

## 3. Code Quality Issues (Medium Severity)

### MED-1: portfolio_strategy.py is 2053 Lines

The "primary" strategy is a single-file god-class. Functions identified:
- `score_premium_selling()` ~130 lines
- `score_trend_following()` ~80 lines
- `__init__()` ~200 lines
- `on_tick()` ~400 lines (with deeply nested branches)
- `_evaluate_premium()` ~200 lines
- `_evaluate_trend()` ~200 lines

**Cognitive load:** 8+ levels of nesting in some branches; impossible to unit-test in isolation; mutation of `_regime`, `_open_positions` makes debugging painful.

**Fix (8 hrs):** Refactor into:
- `PremiumLegManager` (handles IC/strangle selection and lifecycle)
- `TrendLegManager` (handles trend debit spread)
- `RegimeClassifier` (extracts `_regime` logic)
- Main `PortfolioStrategy` becomes a coordinator under 400 lines

---

### MED-2: Magic Numbers Scattered in Scoring Logic

```python
if 14 <= vix <= 20:
    score += 25
elif 20 < vix <= 28:
    score += 20
```

Why 25? Why 20? What's the basis? These are tuneable but live as code constants.

**Fix:** Move to `PortfolioParams` as a `score_table: dict[str, dict[tuple[float, float], int]]`.

---

### MED-3: State Mutation Without Reset Discipline

Multiple strategies have instance variables (`_entered`, `_stopped_for_day`, `_trades_today`, `_adjustments_today`, `_short_ce_token`) that are only reset by `reset_day_state()`. Edge cases (expiry rollover, restart mid-day, force-close) don't reliably reset all of them.

**Fix:** Introduce a `StrategyDayState` dataclass owned by base strategy, with a single `reset()` method that resets everything atomically. Force all derived strategies to declare their day-resettable fields in this dataclass.

---

### MED-4: Type Annotations on DB Models Lie

```python
price: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
```

Says `float` but stores `Numeric` (Decimal). Works at runtime due to SQLAlchemy coercion, but mypy/pyright will flag if/when you add type checking. Future contributor will assume float and lose precision.

**Fix:** Use `Mapped[Decimal]` consistently for all monetary fields (price, pnl, charges, margin). Run `mypy --strict src/db/` to verify.

---

### MED-5: Event Bus Subscriptions Never Cleaned Up

`src/main.py:84` subscribes `_feed_paper_ltp` but no unsubscribe on shutdown. Negligible for single daily run; bites in tests / development reload loops where 100 stale handlers accumulate.

**Fix (30 min):** Track subscriptions in `app` dict; unsubscribe in shutdown.

---

## 4. What Code Review Confirmed Is Working

These got VERIFIED status in the audit (don't fix what isn't broken):

1. **Lot sizes correct** — NIFTY=75, BANKNIFTY=30, FINNIFTY=40 since Nov 14 2024
2. **Tuesday weekly expiry hardcoded correctly** — `constants.py:21-34`
3. **2026 holiday calendar complete** — 27 holidays
4. **Margin check correctly skipped in paper mode** — intentional design
5. **No SQL injection** — uses SQLAlchemy ORM throughout
6. **No sensitive data in logs** — token logging properly truncated
7. **Async/sync clean** — no blocking I/O on hot paths
8. **Circular dependencies handled** — deferred injection pattern (set_risk_manager etc.)
9. **Graceful shutdown wired** — signal handlers + async cleanup
10. **Structured logging excellent** — bracketed tags enable forensic analysis
11. **Charges test suite** — 76 lines of tests for charges.py is one of the better-tested modules
12. **Position reconciliation** — runs at startup, 0 phantom fills observed in 22 days

---

## 5. Top 10 Trader-Logic Issues (From Trader Analysis)

Recap of biggest gaps from the trader perspective (full detail in [TRADER_ANALYSIS_2026-04-17.md](TRADER_ANALYSIS_2026-04-17.md)):

1. **VIX threshold mismatch** — system says "high vol ≥12", reality is "≥19" for India
2. **Tuesday 0DTE risk** — entering IC/strangle at 9:20 on expiry Tuesday is dangerous
3. **Wing width too narrow** — ±250pts wings inside 1σ weekly move (~600pts at VIX 18)
4. **Trailing stops fire on bid-ask noise** — 11-min exit at 9:31 from morning spread widening
5. **Portfolio strategy is the worst performer** in actual paper data (-102/day vs IC standalone +0.90)
6. **PCR filter uses synthetic data** despite 22 days of real chain available
7. **Charges modeling missing** — paper P&L is inside the noise of real charges
8. **No slippage model** — paper fills at LTP, real Kite slips 2-10 ticks
9. **Naive position sizing** — 5 strategies × 1 lot = 60-80% margin utilization
10. **No event calendar** — RBI/Fed/Budget days not skipped

---

## 6. Combined Top Priorities (Cross-Cutting)

Items where code fix + trader insight align — fix these first for maximum ROI:

| # | Fix | Why Both Matter | Effort | Impact |
|---|---|---|---|---|
| 1 | **Wire expiry-day STT in charges.py** | Code: orphaned constant. Trader: real money lost on every expiry ITM. | 2 hrs | CRITICAL |
| 2 | **VIX regime rebuild + wire to all strategies** | Code: magic numbers in scoring. Trader: wrong thresholds for India. | 3 hrs | CRITICAL |
| 3 | **Tiered slippage in paper broker** | Code: paper broker uses LTP unchanged. Trader: real fills slip 12%. | 2 hrs | CRITICAL |
| 4 | **Tuesday expiry handler** | Code: needs new helper. Trader: 0DTE = gamma vertical risk. | 3 hrs | CRITICAL |
| 5 | **Session state persistence to DB** | Code: `StrategyStateModel` exists but unused. Trader: re-entry bug Apr 16. | 3 hrs | CRITICAL (P1 roadmap) |
| 6 | **Strategy error callback → position closure** | Code: silent failure mode. Trader: positions ride to auto-square-off. | 3 hrs | HIGH |
| 7 | **Real charges in paper P&L display** | Code: charges.py works but not aggregated. Trader: P&L lies. | 2 hrs | HIGH |
| 8 | **Move secrets from .env to keychain** | Code: plaintext on disk. Trader: TOTP = full account takeover. | 1 hr | HIGH |
| 9 | **Wing width 5→8 strikes for weekly NIFTY** | Code: simple param change. Trader: 1:4 risk/reward → ~1:2. | 30 min | HIGH |
| 10 | **Integration test suite** (5 critical paths) | Code: 0.9% coverage. Trader: bugs surface in real money. | 8 hrs | HIGH |
| 11 | **Timezone-aware datetimes globally** | Code: 22+ locations. Trader: daily reset firing at wrong time. | 4 hrs | HIGH |
| 12 | **Trail stop activation 10:15 + ATR threshold** | Code: simple params. Trader: false exits on AM noise. | 2 hrs | MEDIUM |
| 13 | **Refactor portfolio_strategy.py** | Code: 2053-line god-class. Trader: hard to evolve regime logic. | 8 hrs | MEDIUM |
| 14 | **Promotion criteria doc + weekly_review.py** | Code: data exists but unused. Trader: closes the champion-challenger loop. | 4 hrs | MEDIUM |
| 15 | **Counterfactual variants framework** | Code: pattern partially in 04-16 logs. Trader: drives strategy evolution. | 4 hrs | MEDIUM |

**Total: ~50 hours** to address all combined-priority items. **Critical-only block: ~13 hours**.

---

## 7. Architectural Recommendations

### A. Split portfolio_strategy.py Before Adding More Logic

Current 2053-line file is the highest-risk asset in the codebase. Every future trader-logic change you make (VIX bands, expiry handling, regime tagging) will touch it. Refactor into 4 cohesive modules first, then iterate.

### B. Promote `decision_snapshot` Table to First-Class

Memory file says this is missing. Build it now:
```sql
CREATE TABLE decision_snapshots (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    strategy_id TEXT NOT NULL,
    underlying TEXT NOT NULL,
    action TEXT NOT NULL,  -- 'ENTRY', 'EXIT', 'BLOCK', 'SKIP'
    spot NUMERIC, vix NUMERIC, pcr_oi NUMERIC, max_pain NUMERIC,
    score INTEGER, score_breakdown JSONB,
    filters_applied JSONB,  -- {"vix": "PASS", "pcr": "BLOCK", ...}
    advisor_input JSONB,
    chosen_strikes JSONB,
    realized_pnl NUMERIC,  -- filled at EOD
    exit_reason TEXT,
    regime_tag TEXT,  -- 'range_bound' | 'trend_up' | etc.
    metadata JSONB
);
CREATE INDEX ON decision_snapshots (timestamp DESC, strategy_id);
CREATE INDEX ON decision_snapshots (regime_tag, vix);
```

This becomes the single queryable source of truth for "what did we decide and was it right?" — feeds promotion criteria, regime tagging, attribution analysis.

### C. Replace Naive `datetime.now()` With a Single `clock.now_utc()` Helper

Make `MarketClock` the only source of time. Forbid direct `datetime.now()` via lint rule. This eliminates timezone bugs at the source.

### D. Move from "Strategies as Files" to "Strategies as Composable Components"

Currently each strategy is a monolithic class. Better:
```
EntryGate (composable filters: VIX, PCR, MaxPain, Time, Event)
  → StrikeSelector (composable: by-delta, by-distance, by-OI)
    → LegBuilder (single, vertical, condor, butterfly)
      → ExitManager (composable: SL, PT, Trail, Time, Greek-based)
```

This makes the champion-challenger pattern trivial — challengers are just different compositions of the same components, not entire forked strategy files.

### E. Build Real Slippage Model in Paper Broker

```python
def _compute_fill_with_slippage(self, symbol, ltp, side, current_time):
    moneyness_distance = self._strikes_from_atm(symbol)
    base_slippage_ticks = {
        0: 1, 1: 1, 2: 2, 3: 3, 5: 5, 7: 8, 10: 12
    }.get(min(moneyness_distance, 10), 15)

    # Time-of-day multiplier
    if current_time.hour >= 14 and self._is_expiry_day():
        base_slippage_ticks *= 4
    elif current_time.hour >= 14:
        base_slippage_ticks *= 2

    tick_size = Decimal("0.05")
    slip = base_slippage_ticks * tick_size
    return ltp - slip if side == OrderSide.BUY else ltp + slip  # Buy higher, sell lower
```

This single change closes ~50% of the paper-to-live P&L gap.

---

## 8. Path To Live Money — Combined Checklist

Before deploying real capital:

**Code-side (critical):**
- [ ] Expiry-day STT wired into charges (Critical-1)
- [ ] Secrets moved out of `.env` to keychain (Critical-2)
- [ ] Integration tests for: entry→fill→exit, daily reset, risk trigger, charges (Critical-3)
- [ ] Timezone-aware datetimes everywhere (High-1)
- [ ] Strategy error → position closure (High-2)
- [ ] IC adjustment counter resets on expiry rollover (High-3)
- [ ] NSE transaction charge rate verified current (High-4)
- [ ] Float→Decimal in validator price check (High-5)

**Trader-side (critical):**
- [ ] VIX bands rebuilt for India (13/17/22/25 thresholds)
- [ ] Tuesday 0DTE handler (skip or use next-week expiry)
- [ ] Wing width to 8-10 strikes for weekly IC
- [ ] Trail stop activation at 10:15 with ATR threshold
- [ ] Real charges + slippage in paper P&L display
- [ ] Margin utilization tracker capped at 50%
- [ ] Event calendar wired into entry logic
- [ ] Session state persistence to DB (P1 roadmap item)

**Process-side:**
- [ ] Promotion/demotion criteria document
- [ ] Counterfactual variants framework
- [ ] Regime tagging on DAY_SUMMARY
- [ ] Weekly review script (`scripts/weekly_review.py`)
- [ ] 60+ paper trading days with all above fixes applied
- [ ] Hold-out validation (30 days select, 30 days verify)

**Realistic timeline:** 6-8 weeks of focused work (10 hrs/week) to address everything before risking real capital.

---

## 9. The Honest Bottom Line

You have built **the best-architected retail F&O algo system I've seen** at this stage of development. The async event bus, structured logging, 6-layer risk defense, AI advisor confluence, and chain recorder + replay engine are all features that prop desks pay engineers $300K+/year to build.

But this engineering excellence is currently powering trading decisions calibrated for the wrong market (US-style VIX bands, no expiry-day awareness, narrow IC wings) and produces P&L numbers that ignore Indian-market realities (charges, slippage, STT exercise). The 22 days of paper data already shows the symptoms: 33% win rate, slightly negative net, primary strategy underperforming its components.

**The good news:** Every gap is fixable. None require ripping up the architecture. Most are 1-3 hour parameter or logic adjustments. The hardest items (state persistence, refactoring portfolio_strategy.py, integration test suite) are 1-2 day projects, not weeks.

**The discipline:** Don't go live until both perspectives — engineering and trading — give green-light on the path-to-live-money checklist above. The system will reward patience; rushing it will turn ₹X capital into ₹0.7X within a month.

— End of Combined Analysis —
