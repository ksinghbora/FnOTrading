# FnO Trading System -- Complete Guide

> **Audience**: Technologists who are new to options trading.
> This document explains both the domain concepts and the engineering behind
> this automated Futures & Options algorithmic trading system for Indian
> markets (NSE).

---

## Table of Contents

1. [Trading Concepts](#1-trading-concepts)
2. [System Architecture](#2-system-architecture)
3. [Strategy Deep Dive](#3-strategy-deep-dive)
4. [Risk Management (6 Layers)](#4-risk-management-6-layers)
5. [AI Advisor](#5-ai-advisor)
6. [Data Collection & Backtesting](#6-data-collection--backtesting)
7. [Live Trading Results (Paper)](#7-live-trading-results-paper)
8. [System Operations](#8-system-operations)
9. [Configuration](#9-configuration)
10. [Future Roadmap](#10-future-roadmap)

---

## 1. Trading Concepts

This section explains options trading from scratch using analogies
a software engineer would find intuitive.

### 1.1 What Are Options (CE / PE)?

An option is a **contract** that gives the buyer the **right, but not the
obligation**, to buy or sell an asset at a fixed price on (or before) a
specific date.

Think of it like a **reservation**. You pay a small fee now (the premium)
to lock in a price for later. If the deal is favorable, you exercise. If
not, you let it expire and lose only the reservation fee.

There are two types:

| Type | Full Name | Right | Analogy |
|------|-----------|-------|---------|
| **CE** | Call Option | Buy the asset at fixed price | "I paid 100 to reserve the right to buy NIFTY at 23000" |
| **PE** | Put Option | Sell the asset at fixed price | "I paid 100 to reserve the right to sell NIFTY at 23000" |

**Why they exist**: Hedging. A portfolio manager holding stocks buys PEs
as insurance against a crash -- just like fire insurance for a building.
Speculators and algo traders participate to provide that insurance
(liquidity) and earn the premium.

In India, options are **European-style** on indices (NIFTY, BANKNIFTY):
they can only be exercised at expiry, not before. They settle in cash --
no physical delivery of an "index."

---

### 1.2 Strike Price, ATM / OTM / ITM

The **strike price** is the fixed price in the contract.

If NIFTY is at 23,000:

```
ITM (In The Money)   -- Option has intrinsic value right now
ATM (At The Money)   -- Strike = current price (approximately)
OTM (Out of The Money) -- Option has NO intrinsic value right now

For CE (Call):
  Strike 22800 → ITM  (you can buy at 22800 what's worth 23000)
  Strike 23000 → ATM
  Strike 23200 → OTM  (why buy at 23200 what's worth 23000?)

For PE (Put):
  Strike 23200 → ITM  (you can sell at 23200 what's worth 23000)
  Strike 23000 → ATM
  Strike 22800 → OTM  (why sell at 22800 what's worth 23000?)
```

**Software analogy**: Think of strike as a `threshold` in an if-statement.
ITM means the condition is already true; OTM means it is not yet true.

---

### 1.3 Premium, Theta Decay, and Why Premium Sellers Profit

**Premium** is the price of an option -- what you pay to buy it, or what
you receive when you sell it. Premium has two components:

```
Premium = Intrinsic Value + Time Value

Intrinsic = max(0, Spot - Strike)  for CE
            max(0, Strike - Spot)  for PE

Time Value = everything else (volatility expectation, time left)
```

**Theta decay** (time decay) is the relentless erosion of time value as
expiry approaches. It is mathematically guaranteed:

```
Day 7:  ████████████████████  Time value = 100
Day 5:  ████████████████       Time value = 80
Day 3:  ██████████             Time value = 55
Day 1:  ████                   Time value = 25
Expiry: (gone)                 Time value = 0
```

The decay is **not linear** -- it accelerates near expiry, like an ice
cube melting faster as it gets smaller.

**Why premium sellers profit**: When you SELL an option, you collect
premium upfront. If the market stays within a range, all that time value
decays to zero and you keep the premium. Think of it as being the
**insurance company** -- most days, the house does not burn down, and you
keep the premium.

The catch: on the days the house DOES burn down (market crashes), losses
can be very large. This is why risk management is critical.

---

### 1.4 India VIX -- The Fear Gauge

**VIX** (Volatility Index) measures how much the market EXPECTS to move
in the next 30 days, derived from option prices. India VIX is computed by
NSE from NIFTY option premiums.

```
VIX < 12   Very calm, premiums thin, theta small
VIX 12-16  Normal, sweet spot for premium selling
VIX 16-20  Elevated, use Iron Condor (wings protect)
VIX 20-25  High fear, reduce position size
VIX > 25   Extreme, sit out or use defined-risk only
```

**Why it matters**: VIX directly determines how much premium you collect.
Higher VIX = fatter premiums = more income BUT also more risk of big
moves.

**Software analogy**: VIX is like CPU temperature. Normal range is fine,
elevated means throttle down, and extreme means stop before something
breaks.

---

### 1.5 Strategies -- Strangle, Iron Condor, Straddle

These are **multi-leg option structures** -- combinations of CE and PE
positions that create specific risk/reward profiles.

#### Short Straddle (sell ATM CE + ATM PE)

You sell both a call and a put at the SAME strike (ATM). You profit if
the market stays near that strike.

```
       Profit
         ^
         |     /\
         |    /  \
         |   /    \
    ─────|──/──────\──────── Strike ─→
         | /   BE   \        (Breakeven points)
         |/          \
     Loss v
```

- **Max profit**: Total premium collected (when market expires exactly at strike)
- **Risk**: Unlimited on both sides
- **Best when**: VIX 11-15, very flat market
- **Used in this system**: Rarely -- needs extremely calm conditions

#### Short Strangle (sell OTM CE + OTM PE)

You sell a call ABOVE the market and a put BELOW the market. You profit
if the market stays between the two strikes.

```
       Profit
         ^
         |  ___/````\___
         | /    zone    \
         |/   of       \
    ─────/──profit──────\────→
        /|               |\
       / |  PE strike    | \  CE strike
      /  |               |  \
 Loss v
```

- **Max profit**: Total premium (when market stays between the two strikes)
- **Risk**: Unlimited beyond breakeven points
- **Best when**: VIX 12-16, range-bound market
- **Used in this system**: When VIX < 14 (calm market)

#### Iron Condor (strangle + protective wings)

An iron condor is a strangle with **insurance**. You sell a strangle AND
buy cheaper OTM options further out for protection.

```
       Profit
         ^
         |  ___/````\___
         | /            \
    ─────/───────────────\───→
        /|               |\
       / |               | \
      /__|_______________|__\___  (loss is CAPPED by wings)
         |               |
    Long PE  Short PE  Short CE  Long CE
    22600    22800     23200     23400
```

Four legs:
1. **Sell** PE at 22800 (short put)
2. **Buy** PE at 22600 (long put -- wing, caps downside loss)
3. **Sell** CE at 23200 (short call)
4. **Buy** CE at 23400 (long call -- wing, caps upside loss)

- **Max profit**: Net premium collected
- **Max loss**: CAPPED at wing width minus premium (e.g., 200 - 80 = 120)
- **Best when**: VIX 14-25, range-bound to mildly volatile
- **Used in this system**: When VIX >= 14 (default mode -- defined risk)

**Why IC is our primary strategy**: The wings limit max loss per trade.
Unlike a naked strangle, a single bad day cannot wipe out a week of gains.

---

### 1.6 Debit Spread (Bull Call / Bear Put)

A **debit spread** is a directional bet with defined risk. You BUY one
option and SELL another at a different strike, paying a net debit.

**Bull Call Spread** (bullish):
```
Buy CE at strike A (closer to ATM -- more expensive)
Sell CE at strike B (further OTM -- cheaper)
Net cost: debit = premium_A - premium_B

       Profit
         ^
         |          ____/````
         |         /
    ─────|────────/──────────→
         |       /|
         |______/ |
     Loss v       A         B
         (max loss = debit paid)
```

**Bear Put Spread** (bearish): Mirror image using PEs.

- **Max loss**: Debit paid (defined, known upfront)
- **Max profit**: Strike difference - debit (also known upfront)
- **Best when**: Strong directional move (trending market)
- **Used in this system**: Trend leg -- enters on confirmed breakout

---

### 1.7 PCR (Put Call Ratio), Open Interest, Max Pain

**Open Interest (OI)** is the total number of outstanding (unsettled)
contracts at a given strike. Think of it as "how many people are betting
at this level."

**Put Call Ratio (PCR)**:
```
PCR = Total Put OI / Total Call OI

PCR < 0.7   →  Too many call buyers (bearish signal)
PCR 0.8-1.2 →  Balanced (neutral, good for premium selling)
PCR > 1.5   →  Excessive hedging (fearful market)
```

**Max Pain** is the price at which the maximum number of options expire
worthless -- where option writers (sellers) lose the least money.

```
Markets tend to gravitate toward max pain near expiry because:
- Market makers are net short options
- They hedge by pushing the market toward max pain
- This is called "pinning" -- not a conspiracy, just incentive alignment
```

**In our system**: PCR and max pain are entry filters. If PCR is extreme
(< 0.7 or > 1.5), the system skips entry. If spot is too far from max
pain (> 3%), entry is blocked.

---

### 1.8 The Greeks -- Delta, Gamma, Theta, Vega

The Greeks measure how an option's price changes in response to market
variables. Think of them as **partial derivatives** of the option pricing
function.

| Greek | Measures | Analogy | Range |
|-------|----------|---------|-------|
| **Delta** | Price sensitivity to spot move | Velocity (first derivative of position) | CE: 0 to 1, PE: -1 to 0 |
| **Gamma** | How fast delta changes | Acceleration (second derivative) | Always positive, peaks at ATM |
| **Theta** | Time decay per day | Depreciation (rent you earn as seller) | Negative for buyers, positive for sellers |
| **Vega** | Sensitivity to volatility | How much VIX changes affect your P&L | Always positive for long options |

Practical examples for a sold 23200 CE when NIFTY is at 23000:
```
Delta = -0.15  → If NIFTY goes up 100 pts, you lose 100 * 0.15 * 75 = 1,125
Gamma = 0.002  → Delta will increase by 0.002 for each 1pt move
Theta = +8.50  → You earn 8.50 * 75 = 637 per day from time decay
Vega  = -12    → If VIX drops 1 point, you gain 12 * 75 = 900
```

**Why gamma matters on expiry day**: Gamma explodes near ATM on expiry.
A 50-point move can flip your delta from 0.1 to 0.5 in minutes. Our
system has gamma-aware exits that tighten stops on expiry day.

---

### 1.9 Lot Size, Margin, NRML vs MIS

**Lot size** is the minimum quantity you must trade. You cannot buy "1
option" -- you buy 1 lot.

| Index | Lot Size | Strike Interval | 1 Lot Notional |
|-------|----------|-----------------|----------------|
| NIFTY | 75 | 50 pts | ~17.25 lakh |
| BANKNIFTY | 30 | 100 pts | ~15 lakh |
| FINNIFTY | 40 | 50 pts | ~9.5 lakh |

**Margin**: When you SELL an option, the exchange holds a deposit
(margin) because your loss is potentially unlimited. For NIFTY options,
margin is typically 80,000 - 1,50,000 per lot depending on VIX.

**Product types**:
| Product | Full Name | Carry Overnight? | Margin |
|---------|-----------|-------------------|--------|
| **NRML** | Normal | Yes | Full span + exposure margin |
| **MIS** | Margin Intraday Square-off | No (auto-squared at 3:15 PM) | ~60% of NRML |

Our system uses **NRML** for flexibility (no forced square-off).

---

### 1.10 Weekly Expiry (NIFTY Tuesday)

After SEBI's November 2024 regulation, each index is allowed only **one
weekly expiry**:

| Index | Weekly Expiry Day |
|-------|-------------------|
| NIFTY | **Tuesday** |
| BANKNIFTY | Wednesday |
| FINNIFTY | Thursday |

This matters because:
- Options lose value fastest in the last 2 days before expiry
- NIFTY options sold on Monday morning have maximum theta decay on
  Monday-Tuesday
- Expiry day has extreme gamma -- small moves cause big P&L swings
- Our system penalizes expiry-day entries (score -10 to -20)

---

### 1.11 Black-Scholes Model (Simplified)

The **Black-Scholes model** is the standard formula for pricing European
options. It takes 5 inputs and produces a theoretical option price:

```
Inputs:
  S = Spot price          (current NIFTY level)
  K = Strike price        (the contract's fixed price)
  T = Time to expiry      (in years, e.g., 5/365 = 0.0137)
  r = Risk-free rate      (e.g., 6.5% in India)
  sigma = Volatility      (annualized, from VIX or computed)

Output:
  C = Call price           P = Put price
```

**Software analogy**: BS is like a physics simulation -- it models
option prices assuming the market follows a random walk (geometric
Brownian motion) with constant volatility. The real market violates both
assumptions, which is why:

1. **IV (Implied Volatility)** varies by strike -- this is called the
   "volatility smile" or "skew"
2. **Real prices diverge** from BS by 40-60% -- our backtests using BS
   overestimate P&L by ~60%
3. **Our chain recorder** captures REAL option prices to avoid BS
   assumptions

The Greeks listed above are all partial derivatives of the BS formula.

---

## 2. System Architecture

### 2.1 High-Level Architecture (Event-Driven)

```
┌─────────────────────────────────────────────────────────────────────┐
│                     FnO Trading System                              │
│                                                                     │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐        │
│  │  Kite    │   │  Chain   │   │ Regime   │   │   AI     │        │
│  │ WebSocket├──►│ Builder  ├──►│ Detector ├──►│ Advisor  │        │
│  │  Ticker  │   │ + Greeks │   │ (2D Vol  │   │ (Claude  │        │
│  └──────────┘   │ + PCR    │   │  ×Action)│   │  shadow) │        │
│       │         └────┬─────┘   └────┬─────┘   └────┬─────┘        │
│       │              │              │               │              │
│       ▼              ▼              ▼               ▼              │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │                     EVENT BUS                             │      │
│  │    (asyncio queues + Redis pub/sub)                       │      │
│  │    Events: TICK, CANDLE, SIGNAL, ORDER_*, RISK_*          │      │
│  └──────┬──────────────────────┬────────────────────┬───────┘      │
│         │                      │                    │              │
│         ▼                      ▼                    ▼              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐          │
│  │  STRATEGIES  │    │     OMS      │    │    RISK      │          │
│  │              │    │              │    │   MANAGER    │          │
│  │ Portfolio    │    │ Validator    │    │              │          │
│  │  (Premium+  │    │ Deduplicator │    │ Limits       │          │
│  │   Trend)    │    │ Executor     │    │ CircuitBreak │          │
│  │ Iron Condor │    │ Tracker      │    │ KillSwitch   │          │
│  │ Strangle    │    │              │    │ GreeksRisk   │          │
│  │ Straddle    │    │              │    │              │          │
│  │ TrendSpread │    │              │    │              │          │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘          │
│         │                   │                   │                  │
│         ▼                   ▼                   ▼                  │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │                   BROKER LAYER                            │      │
│  │    Paper Broker (simulated fills)                         │      │
│  │    OR Zerodha Kite API (real orders)                      │      │
│  └──────────────────────────────────────────────────────────┘      │
│                                                                     │
│  ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌───────────┐       │
│  │TimescaleDB│  │   Redis   │  │  Chain    │  │  FastAPI  │       │
│  │ (trades,  │  │ (cache,   │  │ Recorder  │  │ Dashboard │       │
│  │  candles) │  │  pub/sub) │  │ (CSV/60s) │  │ (port     │       │
│  └───────────┘  └───────────┘  └───────────┘  │  8000)    │       │
│                                                └───────────┘       │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 Data Flow: Tick to Trade

```
1. TICK ARRIVES
   Kite WebSocket → TickFeedManager → EventBus (TICK event)
                                            │
2. CHAIN BUILDING                           │
   OptionChainBuilder ◄────────────────────┘
   ├── Updates LTP, volume, OI per strike
   ├── Computes IV (implied volatility) via Newton-Raphson
   ├── Computes Greeks (delta, gamma, theta, vega) via BS
   ├── Computes PCR, max pain
   └── Publishes enriched chain to Redis cache

3. CANDLE AGGREGATION
   OHLCAggregator ◄── TICK events
   ├── Builds 1-min, 5-min candles from raw ticks
   └── Publishes CANDLE_CLOSED events

4. REGIME DETECTION
   RegimeDetector
   ├── Reads VIX → VolRegime (LOW / NORMAL / HIGH / EXTREME)
   ├── Reads candles → ActionRegime (RANGE_BOUND / CHOPPY / TRENDING)
   └── 2D lookup → strategy recommendation + lot multiplier

5. STRATEGY EVALUATION (on every tick / every 5-min candle)
   PortfolioStrategy.on_tick()
   ├── Score premium conditions (VIX, range, move, PCR, DTE)
   ├── Score trend conditions (breakout strength, OI confirm)
   ├── Apply signal threshold (score >= 60 to enter)
   ├── Apply AI advisor confluence (if enabled)
   └── Return Signal (or None)

6. SIGNAL → ORDER
   Signal → OrderManager.process_signal()
   ├── OrderValidator: checks field validity
   ├── OrderDeduplicator: prevents duplicate entries
   ├── RiskManager.validate_order(): 6-layer check
   └── OrderExecutor.execute(): sends to broker

7. FILL → PORTFOLIO
   Broker fill → OrderTracker → PortfolioManager
   └── Updates positions, P&L, margin used

8. MONITORING
   Chain Recorder → CSV every 60 seconds
   Decision Logger → CSV row per ENTER/EXIT/SKIP
   Telegram → alerts on entry, exit, errors
   FastAPI Dashboard → real-time web UI on port 8000
```

### 2.3 Component Overview

| Component | File | Purpose |
|-----------|------|---------|
| **EventBus** | `src/core/events.py` | Central pub/sub -- asyncio queues + Redis. All components communicate through events, never direct calls. |
| **TickFeedManager** | `src/market_data/feed.py` | Receives raw ticks from Kite WebSocket, normalizes, publishes TICK events. |
| **OptionChainBuilder** | `src/market_data/option_chain.py` | Builds live option chain from ticks. Computes IV, Greeks, PCR, max pain. |
| **OHLCAggregator** | `src/market_data/aggregator.py` | Aggregates ticks into OHLC candles (1-min, 5-min). |
| **RegimeDetector** | `src/strategy/regime.py` | 2D regime model (Vol x Action). Recommends strategy + lot size. |
| **StrategyRunner** | `src/strategy/runner.py` | Manages strategy lifecycle. Auto-loads from STRATEGIES env var. Routes ticks/candles to strategies. |
| **BaseStrategy** | `src/strategy/base.py` | Abstract base class. Defines on_tick/on_candle/on_start/on_stop contract. |
| **PortfolioStrategy** | `src/strategy/implementations/portfolio_strategy.py` | Primary strategy -- two independent legs (premium + trend). |
| **OrderManager** | `src/oms/manager.py` | Orchestrates validation, dedup, risk check, execution. |
| **OrderExecutor** | `src/oms/executor.py` | Sends orders to broker with rate limiting and latency tracking. |
| **RiskManager** | `src/risk/manager.py` | Central risk gate -- every order passes through. |
| **CircuitBreaker** | `src/risk/circuit_breaker.py` | Auto-halts on day loss, rapid loss, or disconnect. |
| **KillSwitch** | `src/risk/kill_switch.py` | Emergency stop -- cancels all orders, closes all positions. |
| **PaperBrokerClient** | `src/broker/paper/client.py` | Simulated broker for paper trading. |
| **ZerodhaClient** | `src/broker/zerodha/client.py` | Real broker integration via Kite Connect API. |
| **ChainRecorder** | `src/market_data/chain_recorder.py` | Saves full option chain to CSV every 60 seconds. Gold standard data. |
| **DecisionLogger** | `src/strategy/decision_logger.py` | Logs 27+ fields per trading decision for future ML training. |
| **Settings** | `src/config.py` | Pydantic-based config from .env file. |

### 2.4 Key Directory Structure

```
FnOTrading/
├── src/
│   ├── main.py                  # Application entry point
│   ├── config.py                # Settings from .env
│   ├── core/
│   │   ├── events.py            # EventBus + EventType enum
│   │   ├── models.py            # Tick, OHLC, Signal, Order, OptionChain
│   │   ├── types.py             # Enums (OrderSide, OptionType, etc.)
│   │   ├── constants.py         # Lot sizes, tokens, VIX thresholds
│   │   ├── clock.py             # MarketClock (holidays, market hours)
│   │   └── structured_logger.py # JSON structured logging
│   ├── strategy/
│   │   ├── base.py              # BaseStrategy ABC
│   │   ├── params.py            # Pydantic param models per strategy
│   │   ├── regime.py            # 2D regime detector
│   │   ├── scoring.py           # Signal scoring configs
│   │   ├── indicators.py        # EMA, breakout detection
│   │   ├── runner.py            # Strategy lifecycle manager
│   │   ├── registry.py          # @register_strategy decorator
│   │   ├── decision_logger.py   # ML data collection
│   │   └── implementations/
│   │       ├── portfolio_strategy.py   # Primary: IC + trend
│   │       ├── short_strangle.py
│   │       ├── short_straddle.py
│   │       ├── iron_condor.py
│   │       └── trend_debit_spread.py
│   ├── market_data/
│   │   ├── feed.py              # Tick normalization
│   │   ├── option_chain.py      # Chain builder + Greeks
│   │   ├── aggregator.py        # OHLC candle builder
│   │   ├── chain_recorder.py    # CSV snapshot recorder
│   │   └── simulator.py         # Synthetic data for testing
│   ├── oms/
│   │   ├── manager.py           # Order orchestration
│   │   ├── executor.py          # Broker order submission
│   │   ├── validator.py         # Order field validation
│   │   ├── dedup.py             # Duplicate prevention
│   │   └── tracker.py           # Fill tracking + TTF
│   ├── risk/
│   │   ├── manager.py           # Central risk gate
│   │   ├── limits.py            # Position / loss limits
│   │   ├── circuit_breaker.py   # Auto-halt logic
│   │   ├── kill_switch.py       # Emergency flatten
│   │   └── greeks_risk.py       # Portfolio Greeks monitor
│   ├── broker/
│   │   ├── base.py              # BrokerClient ABC
│   │   ├── paper/client.py      # Simulated broker
│   │   └── zerodha/
│   │       ├── client.py        # Kite Connect API
│   │       ├── ticker.py        # Kite WebSocket
│   │       └── instruments.py   # Instrument master
│   ├── portfolio/
│   │   ├── manager.py           # Position tracking
│   │   └── reconciliation.py    # Broker vs internal match
│   ├── advisor/
│   │   ├── models.py            # DayBias, Advisory schemas
│   │   ├── analyzer.py          # Claude API analysis
│   │   ├── confluence.py        # Score adjustment logic
│   │   ├── context.py           # News, FII/DII, calendar
│   │   ├── collector.py         # Log parsers
│   │   ├── shadow.py            # Counterfactual scoring
│   │   ├── store.py             # JSON + DB persistence
│   │   └── formatter.py         # Telegram formatting
│   ├── api/
│   │   ├── app.py               # FastAPI application
│   │   ├── routes/              # API endpoints
│   │   └── static/index.html    # Web dashboard
│   ├── backtest/
│   │   ├── engine.py            # BS-based backtest engine
│   │   ├── historical_engine.py # Real spot+VIX CSV replay
│   │   ├── replay_engine.py     # Chain snapshot replay
│   │   ├── walk_forward.py      # Walk-forward optimization
│   │   └── metrics.py           # Sharpe, drawdown, etc.
│   └── db/
│       ├── session.py           # SQLAlchemy async engine
│       └── models/              # TimescaleDB table models
├── scripts/
│   ├── auto_auth.py             # Kite token auto-authentication
│   ├── morning_advisor.py       # Pre-market AI analysis
│   ├── nightly_audit.py         # Post-market audit
│   ├── verify_system.py         # Pre-market health check
│   ├── download_spot_data.py    # Historical data download
│   └── multi_seed_backtest.py   # Multi-seed Monte Carlo
├── data/
│   ├── chain_snapshots/         # Real option chain CSVs (60s)
│   ├── breeze_chain/            # Breeze API historical data
│   ├── decisions/               # Decision logger CSVs
│   ├── day_bias.json            # AI advisor morning output
│   └── *.csv                    # Spot + VIX historical data
├── docs/
│   ├── SYSTEM_GUIDE.md          # This file
│   └── ROADMAP_TOP1PCT.md       # Development roadmap
└── .env                         # Configuration (secrets)
```

---

## 3. Strategy Deep Dive

### 3.1 Portfolio Strategy (Primary)

The Portfolio Strategy is the main trading engine. It runs **two
independent legs** simultaneously -- each with its own entry logic, exit
rules, and P&L tracking.

#### Why Two Legs?

Premium selling (strangle/IC) profits when the market stays flat.
Trend following (debit spread) profits when the market moves big.
On any given day, one of them wins. Together, they hedge each other
naturally:

```
Market behavior    Premium leg    Trend leg    Combined
─────────────────────────────────────────────────────────
Range-bound day    +Profit        No entry     +Profit
Mild trend day     Small loss     Small profit ~Breakeven
Strong trend day   -Loss (capped) +Big profit  +Profit
Choppy/whipsaw     Small profit   -Small loss  ~Breakeven
```

#### Daily Lifecycle

```
09:15 ─── Market opens, collect ticks ──────────────────────────
09:15-09:30  Morning data collection (morning range, first candles)
09:30 ─── Phase 1: Premium evaluation begins ──────────────────
          │ Score premium conditions (VIX, range, move, PCR, DTE)
          │ Need score >= 75 in Phase 1 (high conviction only)
          │ If score passes: enter IC or strangle
          │
10:00 ─── Phase 2: Trend evaluation begins (independent) ──────
          │ Score >= 60 sufficient
          │ Check momentum breakout above morning high / below morning low
          │ If breakout >= 0.5% AND OI confirms: enter debit spread
          │
10:00-15:00  Monitor both legs independently
          │ ├── Premium: check SL, trail stop, profit target, theta/gamma
          │ └── Trend: check SL, trail stop, profit target
          │
15:15 ─── Time stop: close any remaining positions ─────────────
15:30 ─── Market close ────────────────────────────────────────
```

#### Signal Scoring System (0-100)

Every potential trade is scored before entry. The score is a sum of
independent factors:

**Premium Scoring (for strangle / IC)**:

| Factor | Max Points | Best Condition | Worst Condition |
|--------|-----------|----------------|-----------------|
| VIX sweet spot | 25 | VIX 12-16 (strangle) or 14-20 (IC) | VIX > 25 (naked) or > 28 (IC) = 0 |
| Morning range | 25 | Range <= 0.3% (very tight) | Range > 0.8% = 0 |
| Move from open | 25 | Move <= 0.15% (flat) | Move > 0.5% = 0 |
| PCR | 15 | PCR 0.8-1.2 (neutral) | PCR extreme = -10 |
| Days to Expiry | 10 | DTE >= 3 | Expiry day = -10 |

Example: VIX=14.5 (25) + range=0.2% (25) + move=0.1% (25) + PCR=1.0
(15) + DTE=5 (10) = **100** (perfect score, enter immediately)

Example: VIX=22 (8) + range=0.6% (5) + move=0.4% (5) + PCR=0.5 (-10) +
expiry (−10) = **-2** (clamped to 0, do not enter)

**Trend Scoring (for debit spread)**:

| Factor | Max Points | Best Condition | Worst Condition |
|--------|-----------|----------------|-----------------|
| Breakout strength | 30 | Breakout >= 0.5% | No breakout = 0 |
| OI confirmation | 25 | High-OI level breached | No confirmation = 0 |
| Trend duration | 25 | Sustained >= 30 min | < 15 min = 0 |
| VIX adequate | 20 | VIX >= 14 | VIX < 11 = 0 |

**Entry thresholds**:
- Phase 1 (9:30-10:00): score >= 75 (premium only, high conviction)
- Phase 2 (10:00+): score >= 60 (both premium and trend)

#### How IC vs Strangle Is Chosen

```python
if vix < params.strangle_vix_max:  # default 14.0
    mode = "strangle"   # Calm market: naked selling, no wings needed
else:
    mode = "iron_condor" # Higher vol: add protective wings
```

The IC vs strangle decision is driven by VIX alone:
- VIX < 14: Strangle (naked) -- premiums are thin, wings would eat too
  much of the already-small income
- VIX >= 14: Iron Condor -- premiums are fat enough that wings cost is
  worth the protection

#### Exit Rules

**Premium leg exits** (checked every tick):

| Exit Type | Condition | Purpose |
|-----------|-----------|---------|
| Stop Loss | Premium rises to 125% of entry (strangle) or 140% (IC) | Cap losses |
| Trailing Stop | Premium bounces 10% from peak decay (strangle) or 20% (IC) | Lock profits |
| Profit Target | Premium decays 12% (strangle) or 60% (IC) | Capture theta |
| Gamma Stop | Gamma exposure > 60 (or > 120 on expiry day) | Avoid expiry blowup |
| Theta/Gamma Ratio | Theta/gamma < threshold | Diminishing returns |
| Time Stop | After 15:15 | EOD flatten |
| Portfolio Max Loss | Combined day loss > max_loss | Protect capital |

**Trend leg exits**:

| Exit Type | Condition | Purpose |
|-----------|-----------|---------|
| Stop Loss | Spread value drops 20% from entry debit | Cap losses |
| Trailing Stop | Spread value drops 15% from peak | Lock profits |
| Profit Target | Spread reaches 55% of max theoretical value | Capture move |
| Time Stop | After 15:15 | EOD flatten |

Note: IC exit thresholds are wider than strangle because 4 legs create
more noise in the net premium. This was a hard-won lesson from live
trading (see Section 7).

---

### 3.2 Regime Detection (2D Model)

The regime detector classifies the market into a 2D grid of Volatility
(from VIX) and Price Action (from candles):

```
                     RANGE_BOUND      CHOPPY        TRENDING
                   ┌─────────────┬─────────────┬─────────────┐
    LOW (VIX<14)   │  Strangle   │  Strangle   │ Debit Spread│
                   │  1.5x lots  │  0.5x lots  │  1.0x lots  │
                   ├─────────────┼─────────────┼─────────────┤
  NORMAL (14-18)   │  Strangle   │ Iron Condor │ Debit Spread│
                   │  1.0x lots  │  0.5x lots  │  1.0x lots  │
                   ├─────────────┼─────────────┼─────────────┤
    HIGH (18-22)   │ Iron Condor │  SIT OUT    │ Debit Spread│
                   │  0.5x lots  │  0.0x lots  │  0.5x lots  │
                   ├─────────────┼─────────────┼─────────────┤
  EXTREME (>22)    │ Iron Condor │  SIT OUT    │ Debit Spread│
                   │  0.25x lots │  0.0x lots  │  0.5x lots  │
                   └─────────────┴─────────────┴─────────────┘
```

**Vol dimension** (from India VIX):
```python
class VolRegime(str, Enum):
    LOW = "low"         # VIX < 14
    NORMAL = "normal"   # VIX 14-18
    HIGH = "high"       # VIX 18-22
    EXTREME = "extreme" # VIX > 22
```

**Action dimension** (from 5-min candles): Three independent scorers
(range-bound, choppy, trending) each produce a score from 0 to 1. The
winner is the action regime. If two scorers both exceed 0.5, the market
is "conflicted" (transitioning) and position size is cut to 0.25x.

**Confidence** is the gap between the top and second scorer. High
confidence = clear regime. Low confidence = ambiguous, halve size.

---

### 3.3 Trend Debit Spread

A standalone strategy that buys debit spreads on morning range breakouts.
This also runs as the "trend leg" inside the Portfolio Strategy.

#### Breakout Detection

```
Morning range (9:15-9:30): 3 five-minute candles
  Morning High = max(candle.high for candles[0:3])
  Morning Low  = min(candle.low  for candles[0:3])

After 10:00 AM:
  If current price > Morning High + (confirmation_pct × Morning High):
      direction = "UP", strength = (price - Morning High) / Morning High
  If current price < Morning Low - (confirmation_pct × Morning Low):
      direction = "DOWN", strength = (Morning Low - price) / Morning Low
```

**Confirmation**: breakout_confirmation_pct = 0.7% (must move 0.7% beyond
the range, not just touch it). This was tightened from 0.5% after live
trading showed too many false breakouts.

**OI confirmation** (optional but recommended): The breakout level must
coincide with or breach a high-OI strike. High-OI strikes act as
support/resistance because many contracts are sitting there.

#### Entry and Exit

```
Entry:
  Buy ATM+1 CE (bullish) or ATM-1 PE (bearish)
  Sell ATM+1+width CE (bullish) or ATM-1-width PE (bearish)
  Spread width: 3 strikes (150 points on NIFTY)
  Max 1 trade per day (avoids whipsaw re-entries)

Exit:
  Stop loss:      35% of entry debit lost
  Profit target:  50% of max spread value reached
  Trailing stop:  15% drop from peak value
  Time stop:      3:00 PM (exit before close)
```

#### Key Parameters (from `TrendDebitSpreadParams`)

```python
entry_time = time(10, 0)              # Wait for morning range
breakout_confirmation_pct = 0.7       # Strong breakout required
spread_width_strikes = 3              # 150pts on NIFTY
stop_loss_pct = 35.0                  # Cut losers fast
profit_target_pct = 50.0              # Take profit at 50% of max
trailing_stop_pct = 15.0              # Tight trail
max_trades_per_day = 1                # No whipsaw re-entries
oi_confirm = True                     # OI level confirmation
```

---

### 3.4 Standalone Strategies (IC, Strangle, Straddle)

These run independently alongside the Portfolio Strategy, primarily for
data collection and comparison.

**Short Strangle** (`src/strategy/implementations/short_strangle.py`):
- Sells OTM CE + OTM PE at 0.15 delta
- Stop loss: 30%, trail: 15%, profit target: 15%
- VIX max: 18 (naked = conservative)
- Includes hedge legs 5 strikes OTM

**Iron Condor** (`src/strategy/implementations/iron_condor.py`):
- 4 legs: sell 0.15 delta + buy 5-strike wings
- VIX max: 25 (wings protect)
- Stop loss: 40%, profit target: 25%
- Adjustment threshold: 60% (roll when one side is tested)

**Short Straddle** (`src/strategy/implementations/short_straddle.py`):
- Sells ATM CE + ATM PE
- Most sensitive to moves (ATM = highest gamma)
- Needs VIX 11-15 and very tight range
- Stop loss: 30%, trail: 15%, profit target: 10%

All standalone strategies now include **signal scoring** using the configs
in `src/strategy/scoring.py`. Each strategy type has its own scoring
profile (different VIX bands, range preferences, DTE sensitivity).

---

## 4. Risk Management (6 Layers)

Every order passes through 6 independent defense layers before reaching
the broker. Think of it as defense-in-depth, like a castle with multiple
walls.

```
Signal from Strategy
        │
        ▼
┌─── Layer 1: STRATEGY-LEVEL ────────────────────────────────────┐
│  Signal scoring (score >= threshold to enter)                   │
│  VIX filter (skip if VIX > max)                                │
│  PCR filter (skip if PCR outside 0.7-1.5)                      │
│  Max pain filter (skip if spot > 3% from max pain)             │
│  Regime detection (sit out on choppy/extreme)                  │
│  Trend filter (skip if market moved > 0.5% from open)          │
└────────────────────────────────────────────────────────────────┘
        │ Signal passes
        ▼
┌─── Layer 2: OMS (Order Management System) ─────────────────────┐
│  OrderValidator: symbol exists, qty > 0, price > 0             │
│  OrderDeduplicator: no duplicate orders in last 30 seconds     │
│  Rate Limiter: max 5 orders/second                             │
└────────────────────────────────────────────────────────────────┘
        │ Order validated
        ▼
┌─── Layer 3: RISK MANAGER ──────────────────────────────────────┐
│  Day loss check: total day P&L < -15,000 → block              │
│  Strategy loss check: strategy P&L < -5,000 → block           │
│  Total lots check: open lots < 50 → block                     │
│  Open orders check: pending orders < 20 → block               │
│  Risk-reducing exception: exits always allowed                 │
└────────────────────────────────────────────────────────────────┘
        │ Risk approved
        ▼
┌─── Layer 4: CIRCUIT BREAKER ───────────────────────────────────┐
│  State: CLOSED (normal) / OPEN (halted) / HALF_OPEN (exits)   │
│  Triggers:                                                      │
│    - Day loss > threshold                                      │
│    - Rapid loss (5,000 in 5 minutes)                           │
│    - Network disconnect > 120 seconds                          │
│  HALF_OPEN: only risk-reducing orders pass                     │
│  OPEN: all orders blocked                                      │
└────────────────────────────────────────────────────────────────┘
        │ Circuit breaker clear
        ▼
┌─── Layer 5: KILL SWITCH ───────────────────────────────────────┐
│  Manual or automatic emergency stop                             │
│  When activated:                                                │
│    1. Cancel ALL open orders                                   │
│    2. Close ALL positions at market                            │
│    3. Halt ALL strategies                                      │
│  Accessible via: API endpoint, dashboard button, Telegram      │
└────────────────────────────────────────────────────────────────┘
        │ Kill switch not active
        ▼
┌─── Layer 6: BROKER ────────────────────────────────────────────┐
│  Zerodha / Exchange margin requirements                        │
│  Exchange circuit limits (index-level halts)                   │
│  Margin shortfall → order rejected                             │
│  RMS (Risk Management System) by broker                        │
└────────────────────────────────────────────────────────────────┘
        │ Order placed successfully
        ▼
    Exchange (NSE)
```

### Key Risk Parameters

```python
# From src/config.py (Settings)
max_day_loss     = 15,000   # Stop trading if day loss exceeds this
max_strategy_loss = 5,000   # Stop a specific strategy
max_total_lots   = 50       # Total open lots across all strategies
max_open_orders  = 20       # Pending orders limit
max_orders_per_second = 5   # Rate limit to broker API

# From strategy params
vix_entry_max    = 18.0     # Naked strategies (25 for IC)
vix_reduce_above = 15.0     # Halve position size above this VIX
pcr_oi_min       = 0.7      # Min PCR to enter
pcr_oi_max       = 1.5      # Max PCR to enter
max_pain_proximity = 3.0%   # Max distance from max pain

# Circuit breaker
rapid_loss_amount  = 5,000  # Trigger on 5K loss in 5 minutes
disconnect_timeout = 120s   # Trigger on 2-min disconnect
```

### Gamma-Aware Exits

Options near expiry have extreme gamma (small spot moves cause large P&L
swings). The system monitors **gamma exposure**:

```
gamma_exposure = abs(net_gamma) * lots * spot * 0.01

If gamma_exposure > 60:
    Tighten stop loss by 50%

If gamma_exposure > 60 AND expiry day:
    Tighten stop loss by 75% (gamma_expiry_multiplier = 2.0)
```

This prevents the classic retail mistake of holding short options into
expiry and getting destroyed by gamma.

---

## 5. AI Advisor

### 5.1 How It Works

The AI Advisor uses Claude (Anthropic's LLM) to generate a daily market
bias before market open. It is designed as a **confluence** system -- the
AI signal adds to or subtracts from the rule-based signal, never replaces
it.

```
┌───── Pre-Market (9:00 AM) ─────────────────────────────────────┐
│                                                                  │
│  scripts/morning_advisor.py                                     │
│  ├── Collect: yesterday's P&L, news (Google RSS), FII/DII      │
│  │           flows, economic calendar, VIX trend                │
│  ├── Send to Claude API with safety-bounded system prompt       │
│  ├── Claude returns DayBias:                                    │
│  │     {                                                        │
│  │       "bias": "neutral",        # bearish/neutral/bullish    │
│  │       "confidence": 0.65,       # 0-1                       │
│  │       "vix_forecast": "stable", # declining/stable/rising    │
│  │       "adjustment": -5,         # score adjustment ±15 max  │
│  │       "reasoning": "..."        # explanation                │
│  │     }                                                        │
│  └── Save to data/day_bias.json                                │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
         │
         ▼
┌───── During Trading ───────────────────────────────────────────┐
│                                                                  │
│  Portfolio Strategy loads DayBias at on_start()                 │
│                                                                  │
│  At entry scoring:                                               │
│    rule_score = 72                                              │
│    ai_adjustment = -5   (advisor says slightly bearish)         │
│    confidence = 0.65    (< 0.7 threshold)                      │
│                                                                  │
│    if confidence < 0.7:                                         │
│        final_score = rule_score  (AI IGNORED — low confidence)  │
│    else:                                                        │
│        final_score = rule_score + (adj × confidence × weight)   │
│        # bounded to ±15 max adjustment                          │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
         │
         ▼
┌───── Post-Market (4:00 PM) ────────────────────────────────────┐
│                                                                  │
│  scripts/nightly_audit.py                                       │
│  ├── Parse today's [CONFLUENCE] logs                            │
│  ├── Build scorecard:                                           │
│  │     AI agreed with rules: X times                            │
│  │     AI disagreed: Y times                                    │
│  │     AI was right when disagreeing: Z%                        │
│  │     AI alpha: +/- N points (would have helped/hurt)          │
│  └── Save audit to data/audit.json                              │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### 5.2 Shadow Mode (Current Default)

The advisor runs in **shadow mode** by default
(`advisor_confluence_enabled = false`). In this mode:

- AI bias is generated and logged
- Score adjustments are calculated and logged
- But the adjustment is **never applied** to the actual score
- This creates a parallel track record to evaluate AI accuracy

### 5.3 Trust Ladder

The system follows a graduated trust model:

| Phase | Duration | AI Weight | Condition |
|-------|----------|-----------|-----------|
| Shadow | Weeks 1-4 | 0 (log only) | Default |
| Half Weight | Weeks 5-8 | 0.5 | AI alpha > 0 for 20+ days |
| Full Weight | Weeks 9+ | 1.0 | AI alpha > 0, high-confidence calls correct > 60% |
| Codify | Future | Replace with rules | If AI identifies stable patterns |

### 5.4 Configuration

```ini
# .env
ANTHROPIC_API_KEY=sk-ant-...
ADVISOR_MODEL=claude-sonnet-4-20250514
ADVISOR_CONFLUENCE_ENABLED=false
ADVISOR_CONFLUENCE_WEIGHT=1.0
```

### 5.5 Key Design Decisions

1. **Confidence gating**: Low-confidence AI opinions are ignored (< 0.7).
   This prevents the AI from adding noise on uncertain days.

2. **Bounded adjustment**: Maximum ±15 points out of a 100-point score.
   The AI can nudge a borderline decision, not override a strong signal.

3. **Counterfactual scoring**: The shadow system tracks "what would have
   happened" if AI was active, building evidence before trusting it.

4. **No direct position sizing**: The AI only adjusts the entry score,
   not position size, stop loss, or exit timing. One knob = one risk.

---

## 6. Data Collection & Backtesting

### 6.1 Data Sources (by reliability)

```
┌───────────────────────────────────────────────────────────────────┐
│                                                                    │
│  GOLD STANDARD: Our Chain Recorder (~95% realistic)               │
│  ├── Source: Live Kite WebSocket + optional Breeze enrichment     │
│  ├── What: LTP, bid, ask, IV, greeks, OI for ALL strikes         │
│  ├── Frequency: Every 60 seconds                                 │
│  ├── Location: data/chain_snapshots/chain_YYYY-MM-DD.csv         │
│  ├── Available: March 25, 2026+ (growing daily)                  │
│  └── Use for: Exact P&L validation, parameter tuning             │
│                                                                    │
│  GOOD ENOUGH: Breeze API Historical (~50% P&L accuracy)          │
│  ├── Source: ICICI Breeze Connect API (historical endpoint)       │
│  ├── What: 1-min OHLC + OI per strike, computed greeks           │
│  ├── Location: data/breeze_chain/                                │
│  ├── Available: 61 days (Dec 2025 - Mar 2026)                   │
│  ├── Diverges 70-176% from real chain data on same days          │
│  └── Use for: Directional conclusions ONLY                       │
│                                                                    │
│  ROUGH ESTIMATE: BS Historical Engine (~40% P&L accuracy)        │
│  ├── Source: Synthetic option prices from Black-Scholes model     │
│  ├── Input: Real spot + VIX CSVs, IV skew model, IV dynamics    │
│  ├── Available: 123 trading days (Sep 2025 - Mar 2026)          │
│  ├── Overestimates P&L by ~60%                                   │
│  └── Use for: Rough regime pattern analysis only                 │
│                                                                    │
└───────────────────────────────────────────────────────────────────┘
```

**Critical insight**: When all 3 sources were compared on overlapping
days, direction agreement between Breeze and our chain recorder was about
50% (coin flip). Magnitude differences were 70-176%. **Portfolio is the
ONLY strategy profitable across all 3 data sources**.

**Rule**: Never make parameter changes based solely on Breeze or BS
numbers. Wait for 30+ days of chain recorder data before tuning.

### 6.2 Chain Recorder

The chain recorder (`src/market_data/chain_recorder.py`) captures the
full option chain every 60 seconds during market hours:

```
CSV format:
time, underlying, expiry, strike, option_type,
ltp, iv, delta, gamma, theta, vega, oi, volume,
bid_price, ask_price

Example row:
09:30:15, NIFTY, 2026-04-01, 23000, CE,
165.50, 14.2, 0.52, 0.0023, -8.50, 12.1, 542300, 18500,
164.00, 167.00
```

Optional Breeze enrichment adds bid/ask and richer OI data when ICICI
Breeze API credentials are configured.

### 6.3 Replay Engine

The replay engine (`src/backtest/replay_engine.py`) replays recorded
chain snapshots through the strategy as if they were live ticks. This is
the highest-fidelity backtesting available -- the strategy sees real
option prices that real traders saw.

### 6.4 Historical BS Engine

The historical engine (`src/backtest/historical_engine.py`) replays real
spot and VIX data but synthesizes option prices using Black-Scholes with
an IV dynamics model:

```
IV dynamics model:
  - Intraday crush: IV declines 10-15% from open to close
  - Move-dependent expansion: big spot moves increase IV
  - Expiry acceleration: IV crush faster near expiry
  - IV skew: OTM puts have higher IV than OTM calls
```

This model was the biggest single improvement to backtest realism, making
all 3 premium-selling strategies profitable in simulation.

### 6.5 Decision Logger

Every trading decision (ENTER, EXIT, SKIP) is logged with 27+ features
for future ML training:

```
Columns:
  timestamp, strategy_id, leg, decision, mode,
  spot, vix, pcr_oi, max_pain, max_pain_dist_pct, iv_skew_ratio,
  move_from_open_pct, morning_range_pct,
  dte, hour, minute, day_of_week, is_expiry,
  rule_score, ai_adj, final_score, threshold,
  regime,
  entry_premium, quantity, exit_reason,
  outcome_pnl, held_minutes
```

File: `data/decisions/decisions_YYYY-MM-DD.csv`

**Purpose**: When 100+ decisions are collected, train an entry quality
classifier (XGBoost) to dynamically adjust score thresholds.

### 6.6 Backtesting Results Summary

| Engine | Data Source | Days | Portfolio P&L | Notes |
|--------|------------|------|---------------|-------|
| BS Historical | Synthetic prices from real spot+VIX | 90 | +88,287 | ~60% too optimistic |
| Simulated Normal | Monte Carlo (10 seeds x 30 days) | 300 | +9,262 avg | 10/10 profitable |
| Simulated Fat-Tail | Student-t(df=5) tails | 300 | +6,761 avg | 10/10 profitable |
| Breeze Replay | Breeze 1-min OHLC | 61 | Positive (directional) | ~50% P&L accuracy |
| Chain Replay | Real chain snapshots | 3+ | TBD (collecting data) | Gold standard |

---

## 7. Live Trading Results (Paper)

### 7.1 Paper Trading Summary (5 Days, March 2026)

| Strategy | Win Rate | Total P&L | Avg P&L/Trade | Status |
|----------|----------|-----------|---------------|--------|
| Iron Condor | 5/5 (100%) | +32.49 | +6.50 | Best performer |
| Straddle | 3/5 (60%) | -28.34 | -5.67 | Inconsistent |
| Strangle | 2/5 (40%) | -10.72 | -2.14 | Struggling |
| Delta Neutral | 1/4 (25%) | -184.91 | -46.23 | **Disabled** |

Note: P&L in paper mode uses 1-lot sizing with paper broker fills; actual
rupee amounts depend on premium levels and lot sizing. Iron condor's
consistent profitability validated the "defined risk" thesis.

### 7.2 Key Observations

1. **Iron condor is the most consistent** across all market conditions --
   confirmed in both backtest and live paper trading.

2. **Delta neutral is unprofitable** -- disabled after 4 days. The
   hedging costs (futures commissions + slippage on frequent rebalancing)
   ate the theta decay.

3. **Portfolio strategy emerged** as the solution: combine the best
   performer (IC/strangle) with a trend hedge (debit spread) instead of
   running 4 separate strategies.

### 7.3 Lessons Learned from Live Trading

These are the 10 most important lessons, each discovered through an actual
bug or loss during live paper trading:

**1. Fill Price vs Estimated Price Mismatch**
Strategy used chain builder LTP (103.20) for stop/target calculations,
but paper broker filled at a different price (173.56). All SL/PT/trail
calculations used the wrong base. Left approximately 5,000 in profit on
the table.
**Fix**: Fill reconciliation -- always read actual fill prices, not
pre-trade estimates.

**2. WebSocket Goes Silent After Initial Batch**
Kite WebSocket sent 84 ticks on connect, then silence for the entire
day. Strategy entered on stale data, then flew blind with no exits.
**Fix**: Heartbeat detection + auto-reconnect + fallback HTTP LTP
seeding.

**3. Trading on Holidays**
System started on Ram Navami (holiday), connected to Kite, got stale
prices, entered positions. No ticks flowed. Ghost positions all day.
**Fix**: Holiday guard in main.py -- skips strategy loading on
holidays/weekends. Double-check via `is_market_open()` before orders.

**4. Missing tradingsymbol in WebSocket Ticks**
WebSocket ticks only contain `instrument_token`, not `tradingsymbol`.
Lot size lookup on empty string returned 1 instead of 75. Risk manager
thought 75 quantity = 75 lots (should be 1 lot).
**Fix**: Symbol map in OptionChainBuilder -- stores tradingsymbol from
instrument registration, uses as fallback.

**5. IC Wing Strikes Missing from Chain**
Chain registered +/-20 strikes (+/-1000 points). IC needs short + 5-strike
wing. Wing strikes at the edge were often missing, blocking IC entry.
Forced fallback to naked strangle exactly when IC was needed most.
**Fix**: Widened to +/-30 strikes (+/-1500 points). 124 tokens subscribed.

**6. Kite Token Expires Daily**
Access tokens expire at midnight. Next morning: 403 Forbidden on
WebSocket. Auto-auth code existed but could not read credentials.
**Fix**: Explicit .env loading in auto-auth. Scheduled re-auth at
8:55 AM + 5-minute health check.

**7. BS Backtest Overfitting**
105-parameter sweep on BS backtest found "optimal" params that disabled
trail stops. These params were the best in-sample but performed WORSE on
out-of-sample data and on real chain data.
**Fix**: Always do OOS validation. Chose conservative midpoints, not
"optimal" peaks. BS overestimates by ~60%.

**8. Log Spam from Tick-Level Evaluation**
Trend scoring logged on every tick when `now.minute % 5 == 0`. With
~100 ticks/sec, that generated ~6000 log lines per minute-mark.
**Fix**: One-shot guard per 5-minute window. Use `if cur_min !=
self._last_X_min` pattern.

**9. Afternoon Stop Tightening Kills IC**
Added stop tightening after 1 PM (30%) and 2 PM (20%). IC's 4-leg net
fluctuates more than naked positions. P&L dropped from +9,560 to +4,085.
**Fix**: Removed. IC premiums naturally fluctuate +/-20% intraday.

**10. IC Trailing Stop Too Tight**
Applied strangle's trailing stop (15% bounce after 10% decay) to IC.
IC's 4-leg net fluctuates more, triggering stops on normal noise.
**Fix**: Separate IC trailing: 25% decay activation, 20% bounce (vs
strangle's 10% decay, 15% bounce).

**Golden rule**: IC and strangle need separate exit parameters. Four
legs create more noise in net premium -- wider thresholds are required.

---

## 8. System Operations

### 8.1 Starting the System

```bash
# Using uv (recommended)
uv run python -m src.main

# Or using Make
make run
```

The startup sequence:
1. Load Settings from `.env`
2. Connect to TimescaleDB + Redis
3. Initialize EventBus
4. Create broker (Paper or Zerodha based on `PAPER_TRADING` setting)
5. Build MarketClock, check if today is a trading day
6. If holiday/weekend: log warning and skip strategy loading
7. Initialize: TickFeedManager, OHLCAggregator, OptionChainBuilder
8. Initialize: RiskLimits, CircuitBreaker, KillSwitch, RiskManager
9. Auto-authenticate Kite token (if credentials present)
10. Register option chain instruments (NIFTY +/-30 strikes)
11. Start KiteTicker WebSocket (subscribe to ~124 tokens)
12. Start ChainRecorder (60-second CSV snapshots)
13. Load strategies from `STRATEGIES` env var
14. Start FastAPI server (port 8000)
15. Begin processing ticks

### 8.2 Auto-Authentication Flow

Kite Connect tokens expire daily at midnight. The system handles this
automatically:

```
┌─── Startup ──────────────────────────────────────┐
│  auto_auth.py runs                                │
│  ├── Load credentials from .env                   │
│  │   (KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET) │
│  ├── POST /api/login (user + password)            │
│  ├── Generate TOTP from secret (pyotp)            │
│  ├── POST /api/twofa (request_id + totp)          │
│  ├── Extract access_token from response           │
│  ├── Save to .kite_access_token file              │
│  ├── Update .env with new token                   │
│  └── Send Telegram notification (success/failure) │
└──────────────────────────────────────────────────┘

Scheduled re-auth:
  - 8:55 AM daily (before market open)
  - Every 5 minutes: health check (can we call Kite API?)
  - On WebSocket disconnect: attempt re-auth before reconnect
```

### 8.3 Holiday Guard

The system checks NSE holidays before starting strategies:

```python
# In src/core/clock.py
NSE_HOLIDAYS_2026 = {
    date(2026, 1, 26),  # Republic Day
    date(2026, 3, 14),  # Holi
    date(2026, 3, 26),  # Ram Navami  ← learned the hard way
    # ... full list from NSE circular
}

def is_market_open(self) -> bool:
    today = date.today()
    if today.weekday() >= 5:        # Saturday/Sunday
        return False
    if today in NSE_HOLIDAYS_2026:  # NSE holiday
        return False
    now = datetime.now().time()
    return time(9, 15) <= now <= time(15, 30)
```

**Lesson**: Always maintain the holiday list from the official NSE
circular. The system learned this after accidentally trading on Ram
Navami 2026.

### 8.4 Ticker Reconnection

WebSocket connections to Kite are fragile. The system handles
disconnections with:

1. **Heartbeat**: Checks tick count every 60 seconds. If zero ticks
   received, triggers reconnect.
2. **Auto-reconnect**: `_force_reconnect()` tears down and rebuilds the
   WebSocket connection.
3. **LTP seeding**: On startup and reconnect, fetches current prices via
   Kite HTTP API so the paper broker can fill orders even before
   WebSocket starts streaming.
4. **Connection events**: Publishes `CONNECTION_LOST` and
   `CONNECTION_RESTORED` events for circuit breaker monitoring.

### 8.5 Monitoring

**Web Dashboard** (`http://localhost:8000`):
- Real-time P&L by strategy
- Open positions and orders
- Risk limit utilization
- Circuit breaker state
- Option chain viewer
- Kill switch button

**Telegram Notifications**:
- Trade entry/exit alerts
- Daily P&L summary
- Risk limit warnings
- Authentication success/failure
- Kill switch activation

**Structured Logs**:
Tagged log lines for parsing and analysis:
```
[ENTRY_QUALITY]  strategy=portfolio leg=PREMIUM score=78 mode=iron_condor
[FILTER]         strategy=portfolio filter=vix value=23.5 threshold=25 result=pass
[ATTRIBUTION]    strategy=portfolio leg=PREMIUM pnl=+1200 reason=profit_target
[MONITOR]        strategy=portfolio prem_pnl=+800 trend_pnl=-200 combined=+600
[DAY_SUMMARY]    strategy=portfolio total_pnl=+1500 prem_trades=1 trend_trades=1
[SLIPPAGE]       order_id=123 expected=165.5 actual=167.0 slippage_bps=90
[RECONCILE]      status=ok broker_positions=2 internal_positions=2
```

### 8.6 Pre-Market Checklist

Run before every market open:

```bash
uv run python scripts/verify_system.py
```

This script verifies:
- Database connection
- Redis connection
- Kite API authentication (token valid)
- Option chain instrument data loaded
- Telegram bot can send messages
- VIX data available
- No stale positions from yesterday
- Holiday check

---

## 9. Configuration

### 9.1 The .env File

```ini
# ─── Zerodha Kite Connect ─────────────────────────────
KITE_API_KEY=your_api_key
KITE_API_SECRET=your_api_secret
KITE_ACCESS_TOKEN=auto_populated_daily

# ─── Kite Auto-Auth (for daily token refresh) ─────────
KITE_USER_ID=AB1234
KITE_PASSWORD=your_password
KITE_TOTP_SECRET=your_totp_base32_secret

# ─── Database ─────────────────────────────────────────
DATABASE_URL=postgresql+asyncpg://trader:tradersecret@localhost:5432/fnotrading

# ─── Redis ────────────────────────────────────────────
REDIS_URL=redis://localhost:6379/0

# ─── Telegram ─────────────────────────────────────────
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=-1001234567890

# ─── Risk Limits ──────────────────────────────────────
MAX_DAY_LOSS=15000
MAX_STRATEGY_LOSS=5000
MAX_TOTAL_LOTS=50
MAX_OPEN_ORDERS=20

# ─── Application ──────────────────────────────────────
LOG_LEVEL=INFO
ENVIRONMENT=development
PAPER_TRADING=true
API_HOST=0.0.0.0
API_PORT=8000

# ─── AI Advisor ───────────────────────────────────────
ANTHROPIC_API_KEY=sk-ant-...
ADVISOR_MODEL=claude-sonnet-4-20250514
ADVISOR_CONFLUENCE_ENABLED=false
ADVISOR_CONFLUENCE_WEIGHT=1.0

# ─── Breeze API (optional, for chain enrichment) ──────
BREEZE_API_KEY=
BREEZE_API_SECRET=
BREEZE_SESSION_TOKEN=

# ─── Strategies (JSON array) ──────────────────────────
STRATEGIES=[{"name": "portfolio_strategy", "id": "nifty_portfolio_1", "params": {"underlying": "NIFTY", "quantity_lots": 1}}]
```

### 9.2 Strategy Parameters Explained

All strategy parameters are defined in `src/strategy/params.py` as
Pydantic models. Key parameters for the Portfolio Strategy:

```python
class PortfolioParams(BaseStrategyParams):
    # ─── Signal gating ─────────────────────────────────
    signal_threshold = 60       # Minimum score to enter (Phase 2)
    phase1_threshold = 75       # Phase 1 (9:30-10:00) — high conviction only
    entry_time = time(9, 30)    # Wait for morning range to form
    exit_time = time(15, 15)    # EOD flatten

    # ─── Mode selection ────────────────────────────────
    strangle_vix_max = 14.0     # Below this VIX → strangle; above → IC

    # ─── Premium mode (strangle) ───────────────────────
    premium_stop_loss_pct = 25.0      # Cut losses at 25% of collected premium
    premium_trail_stop_pct = 10.0     # Trail by 10% from peak decay
    premium_profit_target_pct = 12.0  # Take profit at 12% decay

    # ─── Iron condor mode ──────────────────────────────
    ic_wing_width_strikes = 5         # 250 pts protection on NIFTY
    ic_stop_loss_pct = 40.0           # Wider SL (4 legs = more noise)
    ic_profit_target_pct = 60.0       # Let IC decay more (defined risk)

    # ─── Gamma-aware exit ──────────────────────────────
    gamma_exit_threshold = 60.0       # Tighten stop above this
    gamma_expiry_multiplier = 2.0     # 2x sensitivity on expiry day

    # ─── Trend mode (debit spread) ─────────────────────
    trend_stop_loss_pct = 20.0        # Cut trend losers fast
    trend_profit_target_pct = 55.0    # Let winners run
    trend_trailing_stop_pct = 15.0    # Tight trail on trends
    breakout_confirmation_pct = 0.5   # 0.5% beyond morning range
```

**Base parameters** (inherited by all strategies):

```python
class BaseStrategyParams:
    underlying = "NIFTY"
    quantity_lots = 1
    entry_time = time(9, 20)
    exit_time = time(15, 15)
    max_loss = 5000              # Per-strategy max day loss
    product = "NRML"             # NRML (carry overnight) vs MIS
    use_weekly_expiry = True

    # VIX filter
    vix_entry_max = 18.0         # Skip entry above this VIX
    vix_reduce_above = 15.0      # Halve lots above this VIX

    # PCR filter
    pcr_filter_enabled = True
    pcr_oi_min = 0.7
    pcr_oi_max = 1.5

    # Max pain filter
    max_pain_filter_enabled = True
    max_pain_proximity_pct = 3.0
```

### 9.3 How to Add a New Strategy

1. **Create the strategy file**:

```python
# src/strategy/implementations/my_strategy.py

from src.strategy.base import BaseStrategy
from src.strategy.params import BaseStrategyParams
from src.strategy.registry import register_strategy

class MyParams(BaseStrategyParams):
    my_param: float = 0.5

@register_strategy("my_strategy", MyParams)
class MyStrategy(BaseStrategy):
    params: MyParams

    def __init__(self, strategy_id: str, params: MyParams):
        super().__init__(strategy_id, params)

    def get_subscriptions(self) -> Subscription:
        return Subscription(instrument_tokens=[], timeframes=[])

    async def on_start(self) -> None:
        self._expiry = self.ctx.next_expiry(self.params.underlying)

    async def on_tick(self, tick: Tick) -> Signal | None:
        # Your strategy logic here
        # Return a Signal to place orders, or None
        return None

    async def on_stop(self) -> None:
        pass
```

2. **Add to STRATEGIES env var**:

```ini
STRATEGIES=[{"name": "my_strategy", "id": "my_strat_1", "params": {"my_param": 0.7}}]
```

3. **The system auto-loads it**: The `@register_strategy` decorator
   registers the strategy in a global registry. The StrategyRunner reads
   the STRATEGIES JSON at startup and instantiates each strategy with its
   params.

**Strategy context** (available via `self.ctx`):
- `self.ctx.get_chain("NIFTY")` -- live option chain
- `self.ctx.get_vix()` -- current VIX value
- `self.ctx.get_candles("NIFTY", Timeframe.M5)` -- 5-min candles
- `self.ctx.next_expiry("NIFTY")` -- next expiry date
- `self.ctx.get_spot("NIFTY")` -- current spot price

---

## 10. Future Roadmap

The full roadmap is in `docs/ROADMAP_TOP1PCT.md`. Here is a summary of
each phase:

### Phase 0: Execution Intelligence (Weeks 1-4)

Foundation phase -- instrument the system to measure everything:
- **Latency pipeline**: Measure tick-to-decision, decision-to-submit,
  submit-to-fill (target: < 500ms total)
- **Slippage tracker**: Record expected vs actual fill prices with
  market context (bid-ask spread, VIX, time of day)
- **Risk proximity monitor**: Log how close to risk limits every
  5 minutes (warn at 70% usage)
- **Decision enrichment**: Add 14 more columns to decision logger
  (total 50+) for richer ML features
- **Structured log persistence**: Route all tagged logs to daily JSONL
  files (90-day retention)

### Phase 1: Prove It Works (Weeks 1-8)

Run the system live and validate without adding features:
- **Paper trading** (weeks 1-4): Run every market day with full logging.
  Target: 60%+ premium entry rate, positive P&L, no manual intervention.
- **1-lot real money** (weeks 5-8): Switch to real execution after 3
  consecutive profitable weeks in paper mode. Target: Sharpe > 1.5.
- **First review** (week 8): Analyze filter effectiveness, slippage
  distribution, live vs backtest comparison, AI advisor scorecard.

### Phase 2: Multi-Instrument (Weeks 9-16)

Diversification across indices:
- Add **BANKNIFTY** live chain building (100-point strikes, 30 lot size)
- Run second PortfolioStrategy instance with BANKNIFTY params
- Cross-instrument risk limits (combined day loss < 8,000)
- Target: correlation < 0.5 between NIFTY and BANKNIFTY daily P&L

### Phase 3: Execution Edge (Weeks 13-20)

Alpha from better execution:
- **Smart order entry**: Limit at mid-price, wait 3s, move aggressive,
  then IOC. Target: 30-50% slippage reduction.
- **Spread execution**: Execute IC as 2 vertical spreads instead of 4
  individual legs (atomic fills, no partial execution risk).
- **Fill prediction model**: After 30+ days of execution data, predict
  slippage and reject entries where cost eats the edge.

### Phase 4: Strategy Expansion (Weeks 17-30)

New uncorrelated strategies:
- **Overnight theta harvester**: Hold IC overnight (NRML), exit at
  9:20 AM next day. Captures 30-40% of daily theta.
- **Expiry day scalper**: Sell far OTM strangles after 2 PM on expiry
  day. Extreme theta decay in last 2 hours.
- **Event-driven overlay**: Widen strangles pre-event (RBI, budget),
  aggressive selling post-event on vol crush.
- **VIX mean reversion**: Buy straddle when VIX spikes > 20 and rose
  > 15% today. Profits when premium selling stops out.

### Phase 5: Real Backtesting (Weeks 20-30)

Validate on real data (by then, 60+ days of chain snapshots):
- **Chain replay backtest**: All strategies through real option prices.
  Expected: 15-25% worse than synthetic. Validate: Sharpe > 2.0.
- **Walk-forward optimization**: Train 40 days, test 20, roll forward.
  Constrain parameter changes < 20% to prevent overfitting.
- **Regime-specific analysis**: Segment results by VIX regime and day
  type. Disable strategies in regimes where they lose money.

### Phase 6: ML-Driven Improvement (Weeks 25-40)

Data-driven edge refinement:
- **Entry quality classifier** (XGBoost): 100+ trades in, predict win
  probability per signal. Dynamically adjust thresholds.
- **Optimal exit timing**: Survival analysis or RL on exit decision.
  Train on: held_minutes, Greeks trajectory, unrealized P&L path.
- **Regime classifier**: Replace rule-based RegimeDetector with ML
  (random forest, retrained weekly).
- **AI advisor activation**: If shadow scorecard shows AI alpha > 0 for
  60+ days, activate confluence at 0.5 weight.

### Phase 7: Infrastructure Hardening (Ongoing)

Production reliability:
- Watchdog process for crash recovery
- State persistence across restarts
- Grafana monitoring dashboard
- Automated pre-market checks (cron 9:00 AM)
- PagerDuty alerts for reconciliation mismatches

### Target KPIs

| KPI | Early (Wk 1-4) | Mid (Wk 9-16) | Mature (Wk 25+) |
|-----|-----------------|----------------|------------------|
| Live Sharpe | > 0 (paper) | > 1.5 (real) | > 2.0 |
| Max DD / Month | < 15K | < 10K | < 8K |
| Win Rate | > 30% | > 40% | > 45% |
| Slippage (bps) | Measure | < 30 | < 15 |
| Latency (p95 ms) | Measure | < 500 | < 300 |
| Uptime (days without intervention) | 5 | 15 | 30 |
| Instruments | 1 (NIFTY) | 2 (+BANKNIFTY) | 2+ |
| Strategy Families | 1 | 2 | 3+ |

---

## Appendix A: Glossary

| Term | Meaning |
|------|---------|
| **ATM** | At The Money -- option strike closest to current spot price |
| **BS** | Black-Scholes -- standard option pricing model |
| **CE** | Call Option (right to buy) |
| **DTE** | Days To Expiry |
| **IC** | Iron Condor (4-leg defined-risk structure) |
| **IV** | Implied Volatility (market's expectation of future movement) |
| **LTP** | Last Traded Price |
| **MIS** | Margin Intraday Square-off (intraday-only product) |
| **NRML** | Normal (carry overnight product) |
| **NSE** | National Stock Exchange of India |
| **OI** | Open Interest (outstanding contracts) |
| **OMS** | Order Management System |
| **OTM** | Out of The Money (no intrinsic value) |
| **PCR** | Put Call Ratio (put OI / call OI) |
| **PE** | Put Option (right to sell) |
| **SEBI** | Securities and Exchange Board of India |
| **SL** | Stop Loss |
| **VIX** | Volatility Index (fear gauge) |

## Appendix B: Indian Market Hours

```
Pre-open:    09:00 - 09:08  (order collection)
Open:        09:08 - 09:12  (price discovery)
Normal:      09:15 - 15:30  (continuous trading)
Close:       15:30 - 15:40  (closing auction)

Our system:
  Starts evaluating:  09:30  (after morning range forms)
  Stops entering:     15:00  (trend) / 15:15 (premium)
  Flat by:            15:15  (all positions closed)
```

## Appendix C: Weekly Expiry Calendar

```
Monday:     No weekly expiry
Tuesday:    NIFTY weekly expiry
Wednesday:  BANKNIFTY weekly expiry
Thursday:   FINNIFTY weekly expiry
Friday:     No weekly expiry

Monthly expiry: Last Thursday of the month (all indices)
```

## Appendix D: Quick Reference -- Strategy Selection Logic

```
Is VIX available?
├── No → Use defaults (strangle mode, 1x lots)
└── Yes
    ├── VIX > 25 (IC max) → Sit out (extreme volatility)
    ├── VIX > 18 (naked max) → IC only (wings protect)
    ├── VIX 14-18 → IC (VIX above strangle_vix_max of 14)
    └── VIX < 14 → Strangle (calm, VIX below strangle_vix_max)

Is there a breakout?
├── No → Premium leg only
└── Yes
    ├── Strength >= 0.5%?
    │   ├── No → Skip trend
    │   └── Yes
    │       ├── OI confirmed? → Enter debit spread
    │       └── Not confirmed → Log only (unless oi_confirm=False)
    └── After 10:00 AM? → Must be true for trend evaluation

Score check:
├── Phase 1 (9:30-10:00): score >= 75 → enter premium
├── Phase 2 (10:00+): score >= 60 → enter premium or trend
└── Below threshold → SKIP (log decision for ML)
```

---

*Document generated for the FnO Trading System, branch FnO-v3.*
*Last updated: 2026-03-28.*
