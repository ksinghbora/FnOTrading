# Roadmap to Top 1% Trading System

## Where We Are Today

A well-engineered retail F&O system (top 10-20%) with:
- Independent legs architecture (premium + trend)
- Regime-aware strategy switching (IC/strangle/debit spread)
- 6-layer risk defense, gamma-aware exits
- AI advisor in shadow mode
- Backtest: +88K/90d, Sharpe 6.77 (synthetic data)
- **0 days live trades** — unproven

## What Top 1% Means (Realistic Retail Target)

- 3+ years live track record, Sharpe > 2.0 sustained
- Multi-instrument (NIFTY + BANKNIFTY minimum)
- Multiple uncorrelated strategy families
- Execution edge (smart orders, slippage < 0.5%)
- Data-driven improvement loop (every trade teaches something)
- Drawdown recovery within 5 trading days, 95% of the time

---

## Phase 0: Execution Intelligence (Weeks 1-4) — THE FOUNDATION

**Why first**: You can't improve what you can't measure. Every phase after this depends on having rich, structured execution data to learn from.

### 0.1 Latency Pipeline Instrumentation

Add timing markers at each stage of the order lifecycle. Every order should carry a `timing` dict through the pipeline.

**Files to modify**:
- `src/market_data/feed.py` — stamp `tick_received_at` on each tick
- `src/strategy/implementations/portfolio_strategy.py` — stamp `decision_at` when signal is generated
- `src/oms/executor.py` — stamp `submitted_to_broker_at`
- `src/oms/tracker.py` — already has `ttf_ms`, extend with pipeline breakdown

**New log tag**:
```
[LATENCY] order_id={id} tick_to_decision_ms={X} decision_to_submit_ms={Y}
          submit_to_fill_ms={Z} total_ms={X+Y+Z}
```

**Target**: Total tick-to-fill < 500ms for market orders. Identify if bottleneck is strategy logic, OMS, or broker.

### 0.2 Slippage Tracker with Context

Extend order tracking to capture market state at submission time.

**Files to modify**:
- `src/oms/executor.py` — capture `mid_price_at_submit` (bid+ask/2 or LTP)
- `src/oms/tracker.py` — compute `market_slippage` = fill_price vs mid_at_submit
- `src/strategy/implementations/portfolio_strategy.py` — log bid-ask spread of target strikes at entry decision

**New log tag**:
```
[SLIPPAGE] order_id={id} symbol={sym} side={BUY/SELL}
           expected={mid_at_submit} actual={fill_price}
           slippage_bps={X} bid_ask_spread={Y}
           market_moved={price_change_during_fill}
```

**Daily summary**:
```
[SLIPPAGE_SUMMARY] date={d} orders={n} mean_slippage_bps={X}
                   p95_slippage_bps={Y} total_slippage_cost={Z}
```

### 0.3 Risk Limit Proximity Monitor

Log how close the system is to hitting risk limits — every 5 minutes and on every order.

**Files to modify**:
- `src/risk/manager.py` — after `validate_order()`, log proximity
- `src/strategy/implementations/portfolio_strategy.py` — periodic log in monitor

**New log tag**:
```
[RISK_PROXIMITY] day_pnl={X}/{max} ({pct}%) strategy_pnl={X}/{max} ({pct}%)
                 lots={X}/{max} ({pct}%) open_orders={X}/{max} ({pct}%)
```

**Alert threshold**: Log at WARNING level when any limit > 70% utilized.

### 0.4 Position Reconciliation Audit Trail

Make the existing reconciler log its results.

**Files to modify**:
- `src/portfolio/reconciliation.py` — add structured logging of every run
- `src/main.py` — add periodic intraday reconciliation (every 15 min)

**New log tag**:
```
[RECONCILE] status={ok|mismatch} timestamp={T} broker_positions={n}
            internal_positions={n} discrepancies={details}
```

### 0.5 Decision Enrichment

Extend the 36-column DecisionLogger to capture execution context.

**Files to modify**:
- `src/strategy/decision_logger.py` — add columns

**New columns (target: 50 total)**:
```
bid_ask_spread_ce, bid_ask_spread_pe     # Executability context
gamma_exposure_at_entry                   # Risk at entry
risk_limit_pct_at_entry                   # How much headroom
latency_tick_to_signal_ms                 # Speed
prev_trade_pnl                            # Autocorrelation check
streak_count                              # Win/loss streak
time_since_last_trade_min                 # Overtrading check
```

### 0.6 Structured Log Persistence

Currently logs vanish when process dies. Fix this.

**Implementation**:
- Route all `[TAG]` structured logs to a daily file: `logs/structured/YYYY-MM-DD.jsonl`
- Each line: `{"timestamp": "...", "tag": "ENTRY", "fields": {...}}`
- Retain 90 days of log files
- Add a `scripts/parse_logs.py` that converts JSONL to DataFrame for analysis

**Files to create**:
- `src/core/structured_logger.py` — JSON file handler for tagged logs
- `scripts/parse_logs.py` — log analysis utility

### 0.7 Execution Quality API & Daily Report

**New API endpoint**: `GET /api/metrics/execution?date=YYYY-MM-DD`
```json
{
  "fill_rate_pct": 98.5,
  "partial_fill_count": 1,
  "slippage_mean_bps": 12.3,
  "slippage_p95_bps": 45.0,
  "latency_mean_ms": 180,
  "latency_p95_ms": 420,
  "risk_limit_max_usage_pct": 62,
  "reconciliation_mismatches": 0,
  "filter_block_count": {"vix": 3, "pcr": 1, "trend": 5},
  "decisions": {"enters": 4, "exits": 4, "skips": 87}
}
```

**Daily Telegram report** (extend `scripts/nightly_audit.py`):
```
[Daily Execution Report]
P&L: +2,150 | Trades: 3 | Win Rate: 67%
Slippage: 8.2 bps avg | Latency: 195ms avg
Risk Usage: 42% peak | Reconciliation: OK
Filters: VIX blocked 3, PCR blocked 1
AI Advisor: shadow +120 (would have helped)
```

---

## Phase 1: Prove It Works (Weeks 1-8) — VALIDATION

Run the current system live and collect data. No new features — just observe.

### 1.1 Paper Trading with Full Logging (Weeks 1-4)

- Run every market day with Phase 0 logging active
- Collect: 20+ trading days of execution data
- Track: fills, slippage, latency, decision quality, P&L

**Success criteria**:
- System runs all 20 days without manual intervention
- Premium leg enters on 60%+ of days
- Trend leg enters on 20%+ of days
- No reconciliation mismatches
- P&L > 0 (even slightly)

### 1.2 1-Lot Real Money (Weeks 5-8)

Switch from paper to real 1-lot execution.

**Before switching**:
- Paper P&L positive for 3 consecutive weeks
- All reconciliations clean
- Slippage measured and acceptable (< 2% on ATM options)
- All risk limits tested (hit at least once in paper mode)

**Success criteria**:
- Live Sharpe > 1.5 over 20 days
- Max single-day loss < 3,000
- Slippage < 50% of paper estimate
- No unhedged positions, no manual interventions

### 1.3 First Performance Review (Week 8)

Analyze all collected data:
- Which filters blocked most entries? Were they correct?
- What's the actual slippage distribution?
- How does live P&L compare to paper? To backtest?
- Which regime (VIX level, time of day, DTE) produces best trades?
- Did AI advisor shadow signals improve or hurt?

**Output**: Tuning document with parameter adjustments for Phase 2.

---

## Phase 2: Multi-Instrument (Weeks 9-16) — DIVERSIFICATION

### 2.1 Add BANKNIFTY Live Chain Building

**Files to modify**:
- `src/main.py` — extend registration loop:
  ```python
  for underlying, step, num_strikes in [("NIFTY", 50, 20), ("BANKNIFTY", 100, 15)]:
  ```
- `src/core/constants.py` — verify BANKNIFTY spot token, lot size (30)
- `scripts/verify_system.py` — add BANKNIFTY checks

**BANKNIFTY specifics**:
- Lot size: 30 (vs NIFTY 75)
- Strike interval: 100 (vs NIFTY 50)
- Weekly expiry: Wednesday (vs NIFTY Tuesday) — spread risk across week
- Typically higher VIX contribution — may favor IC more often

### 2.2 BANKNIFTY Strategy Instance

Run a second PortfolioStrategy instance with BANKNIFTY params.

**Config** (add to STRATEGIES env var):
```json
{
  "name": "portfolio_strategy",
  "id": "banknifty_portfolio_1",
  "params": {
    "underlying": "BANKNIFTY",
    "quantity_lots": 1,
    "strangle_vix_max": 14.0,
    "premium_call_delta": 0.18,
    "premium_put_delta": -0.18
  }
}
```

**Key difference**: BANKNIFTY is more volatile, so:
- Tighter delta targets (0.18 vs 0.20)
- Lower strangle VIX max (14 vs 12 — BANKNIFTY VIX runs higher)
- Wider IC wings (6 strikes × 100 = 600 pts vs 5 × 50 = 250)

### 2.3 Cross-Instrument Risk Management

**New risk rule**: Combined NIFTY + BANKNIFTY day loss limit.
- Individual: -5,000 per underlying
- Combined: -8,000 total (not 2 × 5,000 — correlation)

**Files to modify**:
- `src/risk/limits.py` — add combined underlying loss limit
- `src/risk/manager.py` — aggregate P&L across underlyings

### 2.4 Performance Comparison Dashboard

Track NIFTY vs BANKNIFTY:
- Which produces better risk-adjusted returns?
- Correlation of daily P&L (target: < 0.5)
- Combined portfolio Sharpe vs individual

---

## Phase 3: Execution Edge (Weeks 13-20) — ALPHA FROM EXECUTION

### 3.1 Smart Order Entry

Replace market orders with intelligent limit orders.

**Strategy**:
1. Place limit order at mid-price (bid+ask)/2
2. Wait 3 seconds
3. If not filled, move to aggressive side by 1 tick
4. After 6 seconds, convert to market (IOC)
5. If IOC partially fills, place remaining as limit

**Expected improvement**: 30-50% slippage reduction on entry.

**Files to create**:
- `src/oms/smart_order.py` — SmartOrderRouter with configurable aggression levels
- Add `OrderType.IOC` to `src/core/types.py`

### 3.2 Exit Priority Optimizer

When multiple exit conditions trigger simultaneously, choose the best one.

**Current**: First exit condition wins (arbitrary order).
**Improved**: Score each exit by urgency × impact:
- Gamma stop (expiry) → urgency 10
- Stop loss → urgency 8
- Trailing stop → urgency 5
- Theta target → urgency 3
- Time exit → urgency 2

### 3.3 Spread Execution

For iron condor (4 legs), execute as 2 vertical spreads instead of 4 individual legs.

**Why**: Exchanges handle spreads atomically — no partial execution risk.
**How**: Kite API supports `"variety": "iceberg"` for basket orders.

### 3.4 Fill Prediction Model

After 30+ days of execution data, train a simple model:
- Input: time of day, VIX, bid-ask spread, strike distance from ATM
- Output: expected slippage (bps), expected fill time (ms)
- Use to: pre-reject entries where slippage would eat the edge

---

## Phase 4: Strategy Expansion (Weeks 17-30) — NEW EDGES

### 4.1 Overnight Theta Harvester

Hold NRML positions overnight when theta/risk ratio is favorable.

**Entry**: Same premium leg logic but with NRML product type.
**Exit**: Next morning at 9:20 if gap < 1%, else tight stop.
**Edge**: Overnight theta is 30-40% of total daily theta for weekly options.

**Risk management**:
- Max overnight position: 1 lot only
- Must be IC (defined risk) — never naked overnight
- Skip if event day tomorrow (budget, RBI, global)
- AI advisor veto on high-risk nights

### 4.2 Expiry Day Scalping

NIFTY Tuesday expiry has unique gamma dynamics.

**Strategy**: After 2 PM on expiry day:
- ATM options have extreme gamma (±5 delta per 50pt move)
- Sell far OTM strangles (10 delta) that expire worthless in 75 minutes
- Tight time stop: exit by 3:15 regardless

**Edge**: Theta decay accelerates exponentially in last 2 hours.
**Risk**: Gamma also exponential — must be very far OTM.

### 4.3 Event-Driven Overlay

On known events (budget, RBI policy, US Fed), adjust strategy:
- Pre-event: Widen strangles, increase IC wing width
- Post-event: If vol crush > 5%, aggressive premium selling
- AI advisor already identifies events — just need to act on it

### 4.4 Mean Reversion on VIX

When India VIX spikes > 20 intraday, it typically reverts.

**Strategy**:
- Buy ATM straddle when VIX > 20 AND VIX rose > 15% today
- Target: 50% of VIX reversion captured via vega
- Stop: VIX rises another 10%

**Why uncorrelated**: This strategy profits when premium selling stops out.

### 4.5 OI-Based Strike Selection

Use real OI data (now being collected) for smarter strike selection.

**Current**: Delta-based strike selection (fixed target delta).
**Improved**:
- Identify "pain" strikes where max OI sits
- Sell options at strikes with highest OI change (market makers defending)
- Avoid strikes with unusual OI buildup (smart money positioning against you)

**Status (Apr 2026)**: Partially implemented. `_find_oi_validated_strikes()` combines OI walls + VIX range + delta validation. Currently falls back to delta because OI walls are typically far OTM. Logging OI wall data for every trade for future ML analysis.

### 4.6 Regime-Based Buy/Sell Switching (Low VIX Buying Strategy)

**Problem**: When VIX < 14, premiums are too thin for profitable selling. Our system currently sells anyway or sits out — missing opportunities.

**Insight**: Low VIX means options are CHEAP. This is the buyer's market, not the seller's. Professional desks switch from selling to buying based on VIX regime.

**Regime-based strategy routing**:

| VIX | Range-bound | Trending | Choppy |
|-----|------------|----------|--------|
| < 14 | **Buy straddle** (pre-event) or sit out | **Buy debit spread** (cheap) | Sit out |
| 14-22 | **Sell** strangle/IC | **Buy** debit spread | Sell IC (half size) |
| > 22 | **Sell** IC only (defined risk) | **Buy** debit spread (half size) | Sit out |

**Implementation**:
- Add `LongStraddleStrategy` for low-VIX event plays (buy ATM CE + PE before known events)
- Modify portfolio strategy to route to buying when VIX < 14 instead of selling
- Use event calendar to identify IV crush opportunities (buy before event, sell after if IV drops > 5%)
- Position sizing: max 1-2% of capital per option buy (vs 20-25% for selling)

**Key buying rules (from Anant Ladha)**:
- Buy only when IV is LOW (below 30-day historical average)
- Target 3:1 reward:risk minimum
- Tight stop: exit if option loses 50% of purchase price
- Time stop: exit if target not hit within 2 days (theta erodes)

**Why this matters**: Covers the ~40% of trading days when VIX is below 14 and selling doesn't work. Instead of sitting out, we deploy capital differently.

**Prerequisites**: 30+ days of low-VIX chain data to validate buying edge. Currently VIX has been 25+ (high), so no low-VIX data yet.

**Effort**: 15-20 hours. Medium complexity — new strategy + regime routing.

---

## Phase 5: Real Backtesting (Weeks 20-30) — VALIDATION ON REAL DATA

### 5.1 Chain Replay Backtest

By week 20, you'll have 60+ days of recorded chain snapshots.

**Run**: All strategies through replay engine with real option prices.
**Compare**: Replay P&L vs synthetic backtest P&L.
**Expected**: Replay is 15-25% worse (real spreads, real IV behavior).

If replay Sharpe > 2.0 → strategies are validated.
If replay Sharpe < 1.5 → need parameter re-tuning.

### 5.2 Walk-Forward Optimization

Use existing `src/backtest/walk_forward.py`:
- Train on 40 days, test on 20 days, roll forward
- Optimize: delta targets, stop loss %, VIX thresholds
- Constraint: parameter changes < 20% from current (prevent overfitting)

### 5.3 Regime-Specific Backtesting

Segment results by market regime:
- Low VIX (< 14): Strangle performance
- Normal VIX (14-20): IC performance
- High VIX (20-30): IC + trend performance
- Trending days (>1% move): Trend leg performance

**Goal**: Understand which regime each strategy truly profits in. Disable strategies in regimes where they lose money.

---

## Phase 6: ML-Driven Improvement (Weeks 25-40) — DATA TO EDGE

### 6.1 Entry Quality Classifier

After 100+ trades, train on decision_snapshots CSV:
- Features: VIX, PCR, morning_range, DTE, hour, regime, score
- Target: trade outcome (win/loss, P&L bucket)
- Model: Gradient boosted trees (XGBoost)

**Use**: Adjust signal threshold dynamically. If model says 70% win probability at score=60, enter. If model says 40% at score=75, skip.

### 6.2 Optimal Exit Timing

- Features: held_minutes, Greeks trajectory, VIX change, unrealized P&L path
- Target: optimal exit point (maximize risk-adjusted P&L)
- Model: Survival analysis (when to exit) or RL (continuous action)

### 6.3 Regime Classifier

Replace rule-based RegimeDetector with ML:
- Features: VIX level + trend, morning range, OI changes, FII/DII flows
- Target: regime (4 classes) + expected daily range
- Model: Random forest, retrained weekly

### 6.4 AI Advisor Activation Decision

After 60+ days of shadow data:
- Compute trust scorecard: AI right on X% of disagreements
- If AI alpha > 0 consistently → activate confluence at 0.5 weight
- If AI alpha < 0 → keep in shadow, retune prompt

---

## Phase 7: Infrastructure Hardening (Ongoing)

### 7.1 High Availability
- Watchdog process that restarts on crash
- State persistence (positions, flags) to survive restarts
- Dual-server deployment (primary + standby)

### 7.2 Monitoring & Alerting
- Grafana dashboard: P&L curve, Greeks, risk limits, latency
- PagerDuty/Telegram alerts: reconciliation mismatch, circuit breaker, unhedged position
- Daily email digest with execution quality metrics

### 7.3 Automated Pre-Market Checks
- Cron job: `scripts/verify_system.py` at 9:00 AM
- Auto-authenticate Kite token
- Download fresh instruments
- Verify DB, option chain, Telegram all green
- Alert if any check fails — block auto-start

---

## Measurement Framework

### KPIs to Track Weekly

| KPI | Week 1-4 Target | Week 9-16 Target | Week 25+ Target |
|-----|-----------------|------------------|-----------------|
| **Live Sharpe** | > 0 (paper) | > 1.5 (real) | > 2.0 |
| **Max DD / Month** | < 15K | < 10K | < 8K |
| **Win Rate** | > 30% | > 40% | > 45% |
| **Slippage (bps)** | Measure | < 30 | < 15 |
| **Latency (p95 ms)** | Measure | < 500 | < 300 |
| **Days Without Intervention** | 5 consecutive | 15 consecutive | 30 consecutive |
| **Reconciliation Mismatches** | < 3/week | 0/week | 0/month |
| **Instruments** | 1 (NIFTY) | 2 (+ BANKNIFTY) | 2+ |
| **Strategy Families** | 1 (premium + trend) | 2 (+ overnight) | 3+ |
| **AI Advisor Alpha** | Measure (shadow) | Measure | > 0 (activate) |

### Monthly Review Checklist

1. P&L vs backtest expectation (within 30%?)
2. Slippage trend (improving or degrading?)
3. Filter effectiveness (which filters prevent losses vs block profits?)
4. Risk limit utilization (too tight? too loose?)
5. Strategy regime performance (which strategies work when?)
6. Execution quality trend (latency, fill rate, partial fills)
7. AI advisor shadow scorecard (trending positive?)
8. New data insights (patterns in decision_snapshots?)

---

## Effort Estimates

| Phase | Duration | Effort | Prerequisite |
|-------|----------|--------|--------------|
| **Phase 0**: Execution Intelligence | Weeks 1-4 | 15-20 hrs dev | None |
| **Phase 1**: Prove It Works | Weeks 1-8 | 2 hrs/day monitoring | Phase 0 |
| **Phase 2**: Multi-Instrument | Weeks 9-16 | 10-15 hrs dev | Phase 1 validated |
| **Phase 3**: Execution Edge | Weeks 13-20 | 15-20 hrs dev | 30+ days slippage data |
| **Phase 4**: Strategy Expansion | Weeks 17-30 | 20-30 hrs dev | Phases 1-2 profitable |
| **Phase 5**: Real Backtesting | Weeks 20-30 | 10-15 hrs | 60+ days chain data |
| **Phase 6**: ML-Driven | Weeks 25-40 | 20-30 hrs | 100+ trades logged |
| **Phase 7**: Infrastructure | Ongoing | 5 hrs/month | Any phase |

**Total to "Top 1% Retail"**: ~6-10 months of consistent execution + 100-150 hrs development.

---

## Phase 8: Profitability Improvements (From Live Trading Insights — Apr 2026)

Based on 4 days of live paper trading data collection, these improvements target the main profit killers.

### 8.1 Chop Regime Detection (HIGH PRIORITY)

**Problem**: 30-40% of trading days are choppy — market moves enough to trigger entries but reverses. Both premium and trend legs lose on these days.

**Solution**: Add CHOP regime to RegimeDetector:
- Detect: morning range > 0.5% BUT no sustained direction (reverses within 30 min)
- Indicators: price crosses VWAP 3+ times in first hour, range expanding but no trend
- Action: Sit out entirely, or use very tight stops (15% SL)

**Expected impact**: Avoid 1-2 losing days per week → +1-2% monthly

**Files to modify**:
- `src/strategy/regime.py` — add `CHOPPY` regime classification
- `src/strategy/implementations/portfolio_strategy.py` — sit out or reduce size in CHOP

### 8.2 Enable AI Advisor Confluence (MEDIUM PRIORITY)

**Evidence**: Advisor was correct on both tested days:
- Mar 30: Said EXTREME/skip_premium → system lost -4,235 ignoring it
- Apr 01: Said MEDIUM/iron_condor → system made +1,492 following it

**Activation criteria**: After 15+ shadow days with advisor accuracy > 65%
- Enable `advisor_confluence_enabled=true`
- Start at weight=0.5 (half influence)
- Monitor for 2 weeks, increase to 1.0 if alpha remains positive

**Expected impact**: Avoid 1-2 extreme days per month → +0.5-1% monthly

### 8.3 Intraday Re-Entry After Profit Target (MEDIUM PRIORITY)

**Problem**: After PT hit at 12:21 (Apr 01), market stayed range-bound for 3 more hours. Theta was available but no position was on.

**Solution**: Allow one re-entry after PT if:
- Score still >= 70
- Time before 13:00
- VIX hasn't spiked since exit
- At least 30 min after PT exit (let dust settle)

**Expected impact**: 1 extra profitable trade per week → +0.5% monthly

### 8.4 Dynamic Profit Target Based on VIX and DTE (MEDIUM PRIORITY)

**Problem**: Fixed 12% PT exits too early on high-VIX days (premium decays faster, could capture 20-30%) and too late on low-VIX days (premium barely moves).

**Solution**:
- VIX > 20: PT = 18% (fat premiums, faster decay)
- VIX 14-20: PT = 12% (current default)
- VIX < 14: PT = 8% (thin premiums, take what you can)
- DTE = 0-1: PT = 20% (expiry day decay is exponential)

**Expected impact**: +15-20% more captured per winning trade → +0.5% monthly

### 8.5 Slow Drift Detector for Trend Leg (MEDIUM PRIORITY)

**Problem**: Current breakout detector misses slow grinds (Mar 30: NIFTY dropped 0.9% over 6 hours but never had a sharp breakout moment).

**Solution**: Add drift detection alongside breakout:
- Track cumulative directional movement over 2-hour rolling window
- If cumulative > 0.7% in one direction with < 30% retracement → "drift" signal
- Enter debit spread on drift (lower conviction than breakout, smaller size)

**Expected impact**: Catch 2-3 trend days per month that breakout misses → +0.5% monthly

### 8.6 BANKNIFTY as Second Underlying (Phase 2 already covers this)

**Additional insight from live trading**: NIFTY and BANKNIFTY have ~0.6 correlation. Running portfolio strategy on both means:
- 2x opportunities (BANKNIFTY expiry on Wednesday, NIFTY on Tuesday)
- Partial diversification (one may be range-bound while other trends)
- Combined Sharpe improves ~30% over single underlying

### 8.7 Standalone IC Should NOT Adjust (FROM DATA)

**Evidence**: IC standalone (ic_1) lost -2,528 across 4 days. Every adjustment added charges and often adjusted into a worse position.

**Fix**: For standalone IC, disable adjustments — just hold to PT or SL. The defined risk means max loss is capped. Adjustments turn defined risk into undefined charges.

### Summary of Expected Cumulative Impact

| Improvement | Monthly Impact | Complexity | Data Needed |
|-------------|---------------|------------|-------------|
| Chop detection | +1-2% | Medium | 20+ days regime data |
| AI advisor enabled | +0.5-1% | Low | 15+ shadow days |
| Intraday re-entry | +0.5% | Low | 10+ PT exits analyzed |
| Dynamic PT | +0.5% | Low | 20+ trades |
| Slow drift detector | +0.5% | Medium | 30+ trend days |
| BANKNIFTY | +1-2% | Medium | Chain recording setup |
| IC no adjustments | +0.3% | Trivial | Already proven |

**Total potential improvement: +4-7% monthly on top of current ~3%**
**Realistic after implementation: 4-5% monthly sustained (50-60% annual)**

---

## The Hard Truth

The biggest gap to top 1% is NOT code — it's **live market hours under management**. No amount of engineering substitutes for:
- 200+ live trading days
- 500+ real trades with real slippage
- 3+ regime changes navigated (low vol → high vol → crash → recovery)
- Real drawdowns survived without panicking

The roadmap above builds the system. The market builds the trader. Start Phase 0 tomorrow — every day of data collection brings you closer.
