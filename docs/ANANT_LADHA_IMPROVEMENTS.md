# Anant Ladha Insights — Improvement Plan

## Source
- Channel: Invest Aaj For Kal (CFA, CA, CFP, LL.B.)
- Knowledge base: `docs/anant_ladha_option_kb.json`
- 17 videos, comprehensive F&O methodology for Indian markets

## Gap Analysis Summary

| Status | Count | Description |
|--------|-------|-------------|
| Implemented | 9 | Already in our system |
| Partial | 10 | Concept exists but values/logic differ |
| Missing | 13 | Not implemented at all |

---

## HIGH PRIORITY Improvements (Data-backed, clear edge)

### 1. OI Wall-Based Strike Selection
**Current:** Delta-based (sell at delta 0.15)
**Anant:** Sell at/above highest Call OI (resistance) and at/below highest Put OI (support)

**Why this matters:** OI walls represent institutional positioning. Selling outside these walls means institutions are protecting those levels — they have a vested interest in market staying within range. Delta-based selection ignores where the money is.

**Implementation:**
```python
# In portfolio_strategy.py, modify _enter_strangle / _enter_iron_condor
def _find_oi_based_strikes(self, chain):
    """Select strikes based on OI walls, not just delta."""
    max_ce_oi_strike = max(chain.strikes, key=lambda e: e.ce.oi if e.ce else 0)
    max_pe_oi_strike = max(chain.strikes, key=lambda e: e.pe.oi if e.pe else 0)

    # Sell at or just beyond OI wall
    ce_strike = max_ce_oi_strike.strike  # Resistance — sell CE here
    pe_strike = max_pe_oi_strike.strike  # Support — sell PE here

    # Cross-validate with delta (must still be 0.10-0.30 range)
    if ce_entry.ce.greeks.delta > 0.30:
        # OI wall too close to ATM — use delta instead
        pass

    return ce_strike, pe_strike
```

**Expected Impact:** Better strike placement = fewer stop-outs. OI walls have held ~70% of the time historically.

**Effort:** Medium (1 week). Need to integrate OI data into strike selection.

**Files to modify:**
- `src/strategy/implementations/portfolio_strategy.py` — `_enter_strangle`, `_enter_iron_condor`
- `src/strategy/scoring.py` — add OI wall proximity scoring

---

### 2. VIX-Based Expected Range Formula
**Current:** No range calculation for strike selection
**Anant:** `Expected weekly range = (Spot × VIX × √7) / (√365 × 100)`

**Why this matters:** This gives a mathematically sound boundary. If Nifty is at 23000 and VIX is 15, expected weekly range is ±415 points. Selling strikes beyond this range means ~68% probability of profit (1 SD).

**Implementation:**
```python
def expected_range(spot: float, vix: float, days: int = 7) -> float:
    """Expected price range using VIX (1 SD = 68% probability)."""
    import math
    return spot * vix / 100 * math.sqrt(days / 365)

# Use for strike selection:
# Sell CE at spot + expected_range (or beyond)
# Sell PE at spot - expected_range (or beyond)
```

**Expected Impact:** More principled strike selection. Combined with OI walls → sell at max(OI_wall, VIX_range_boundary).

**Effort:** Low (2-3 days)

**Files to modify:**
- `src/strategy/implementations/portfolio_strategy.py`
- `src/strategy/regime.py` — add range to RegimeSnapshot

---

### 3. Weekly Cycle Discipline (Enter Mon, Exit Wed)
**Current:** Enter any day, hold to profit target or exit time
**Anant:** Enter Monday morning, exit by Wednesday 2:30 PM. Avoid Thursday gamma risk.

**Why this matters:** NIFTY weekly expiry is Tuesday. Thursday is the highest gamma day. By exiting Wednesday, you avoid the last 1.5 days where gamma can destroy profits. Theta decay is fastest Mon-Wed.

**Implementation:**
```python
# In portfolio_strategy, add weekly timing:
day = now.weekday()  # 0=Mon, 1=Tue, ...

# Entry: prefer Monday-Tuesday
if day >= 3:  # Thursday-Friday
    logger.info("[SHADOW_BLOCK] Late week entry — reduced conviction")
    # In live mode: skip or half size

# Exit: mandatory close by Wednesday 2:30 PM if DTE <= 2
if day == 2 and now.time() >= time(14, 30) and dte <= 2:
    return self._exit_premium("Weekly time stop: Wed 2:30 PM")
```

**Expected Impact:** Avoids the highest-risk period (Thursday near expiry). May sacrifice some theta but saves from gamma blowups.

**Effort:** Low (1-2 days)

**Note:** NIFTY moved to Tuesday expiry (not Thursday). Adjust accordingly — enter Thursday/Friday, exit Monday 2:30 PM.

---

### 4. Change in OI Tracking
**Current:** We capture absolute OI but don't track changes
**Anant:** Track CHANGE in OI — fresh positions reveal where smart money is going

**Why this matters:**
- OI increasing + price rising = fresh longs (bullish)
- OI decreasing + price rising = short covering (weak bounce)
- OI increasing + price falling = fresh shorts (bearish)
- OI decreasing + price falling = long unwinding (bearish)

**Implementation:**
```python
# In chain_recorder or option_chain_builder:
# Store previous day's OI per strike
# Compute delta_oi = today_oi - yesterday_oi
# Add to decision_logger fields (already have oi_change_ce/pe)

# In scoring:
# If selling CE and CE OI is increasing = support for the sell (writers agree)
# If selling CE and CE OI is decreasing = warning (writers exiting)
```

**Expected Impact:** Better entry timing. Avoid selling when OI signals disagree with position.

**Effort:** Medium (3-4 days)

**Files to modify:**
- `src/market_data/option_chain.py` — store previous OI, compute change
- `src/strategy/scoring.py` — add OI change to scoring factors
- `src/strategy/decision_logger.py` — populate oi_change_ce/pe fields

---

### 5. Minimum Premium % Check
**Current:** No minimum premium threshold as % of index
**Anant:** Collect minimum 0.5% of index value. Below this, not worth the risk.

**Why this matters:** At Nifty 23000, minimum premium should be 115 (0.5%). If total strangle premium is only 60, the risk/reward is terrible — you're risking 5000+ to make 60.

**Implementation:**
```python
# In _enter_strangle / _enter_iron_condor:
spot = float(self.ctx.get_spot_price(self.params.underlying))
min_premium = spot * 0.005  # 0.5% of spot

if float(total_premium) < min_premium:
    logger.info(f"[{self.strategy_id}] BLOCKED: premium {total_premium} < min {min_premium:.0f} (0.5% of spot)")
    return None
```

**Expected Impact:** Avoids low-premium trades where charges eat the edge.

**Effort:** Low (1 day)

---

## MEDIUM PRIORITY Improvements

### 6. SL at 2x Premium (vs our 25-40%)
**Current:** SL at 25% (strangle) or 40% (IC) of premium
**Anant:** SL when premium DOUBLES (100% increase)

**Analysis:** Our tighter SL cuts losers faster but also exits more often. Anant's 2x gives more room for intraday noise. Need to test which is better on our data.

**Action:** Run backtest comparing 25% SL vs 100% SL (2x) on chain data. Decision after data.

---

### 7. IC Profit Target 50% (vs our 60%)
**Current:** IC PT at 60% of credit
**Anant:** Close at 50% of max profit

**Analysis:** Anant says "don't be greedy for last 30%." Our 60% is slightly greedier. The difference is small. Could test 50% vs 60%.

**Action:** Compare in next backtest cycle.

---

### 8. Event Calendar Integration
**Current:** AI advisor mentions events but no structured calendar
**Anant:** Never trade event days without hedges. IV crush events are opportunities.

**Implementation:** Maintain `data/economic_calendar.json` with:
- Budget day, RBI policy, quarterly results, elections
- Auto-flag these in scoring (reduce premium score by 20 on event days)
- Post-event: if IV drops >5% intraday, aggressive premium selling signal

**Effort:** Medium (3-4 days)

---

### 9. Capital % Based Position Sizing
**Current:** Fixed lot count
**Anant:** Max 20-25% of capital per position, keep 30% cash

**Implementation:**
```python
# In risk manager:
deployed_capital = sum(margin for each open position)
available_capital = total_capital - deployed_capital
max_new_position = total_capital * 0.25

if estimated_margin > max_new_position:
    reduce lots accordingly
```

**Effort:** Medium (2-3 days)

---

### 10. Proper Rolling Instead of Adjustment
**Current:** IC adjustments are crude — close threatened side and reopen
**Anant:** Roll the threatened side to next strike or next expiry

**Implementation:** Instead of closing + reopening, modify the strike:
- If CE side threatened: buy back short CE, sell CE at higher strike
- Same for PE side
- This preserves the unthreatened side and reduces charges

**Effort:** Medium (3-4 days)

---

## LOW PRIORITY (Consider Later)

### 11. Close IC 5 Days Before Expiry
**Anant says:** Avoid gamma risk by closing early
**Our take:** With DTE-based scoring and gamma-aware exits, we already handle this. But could add as a soft rule.

### 12. Cash Reserve Tracking
Track how much capital is reserved for adjustments. Alert when reserve < 30%.

### 13. BankNifty-Specific Rules
Anant notes BankNifty is more volatile. When we add BankNifty (Phase 2), use wider strikes and different delta targets (0.18 vs 0.15).

---

## What Anant Gets WRONG (or we disagree with)

### 1. "Enter Monday, Exit Wednesday" — Expiry Shifted
NIFTY expiry moved to Tuesday (not Thursday). His Monday→Wednesday cycle is outdated. Our system correctly handles Tuesday expiry.

### 2. Lot Sizes Changed
KB says Nifty=50, BankNifty=15. Current lot sizes are Nifty=75, BankNifty=30. We have correct values.

### 3. Strangle PT at 50% is Too Late
Anant says close strangle at 40-50% profit. Our 12-15% PT captures quick theta and re-enters. On our data, earlier PT has better risk-adjusted returns (multiple small wins > one big hold).

### 4. "Never Sell Below VIX 14"
Anant says avoid selling when VIX < 14. Our system sells with reduced size — thin premium but still net positive after charges on calm days. Could be worth testing.

---

## Implementation Priority Order

| # | Improvement | Impact | Effort | When |
|---|-------------|--------|--------|------|
| 1 | **OI wall strike selection** | High | 1 week | Week 1-2 |
| 2 | **VIX range formula** | High | 2-3 days | Week 1 |
| 3 | **Min premium 0.5% check** | Medium | 1 day | Week 1 |
| 4 | **Weekly cycle timing** | Medium | 1-2 days | Week 2 |
| 5 | **OI change tracking** | Medium | 3-4 days | Week 2-3 |
| 6 | **Event calendar** | Medium | 3-4 days | Week 3 |
| 7 | **Capital % sizing** | Medium | 2-3 days | Week 3 |
| 8 | **SL 2x vs 25% backtest** | Data | 1 day | After 30 trades |
| 9 | **IC rolling (not adjust)** | Medium | 3-4 days | Week 4 |
| 10 | **IC PT 50% vs 60% test** | Low | 1 day | After 30 trades |

**Total estimated effort: ~4 weeks**

---

## Key Insight from Anant

> "Option chain analysis is the REAL edge. Most retail traders use TA (charts).
> Professionals use option chain data — OI, PCR, IV — because it shows
> where the MONEY is positioned, not where the LINES are drawn."

This aligns with our system's direction. We collect OI data but don't use it for strike selection yet. **OI-based strike selection is the single biggest improvement** we can make from this knowledge base.
