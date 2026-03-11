"""Run a short straddle backtest with synthetic NIFTY data.

Generates realistic intraday NIFTY price movements and option premiums
for 30 trading days, runs the short straddle strategy, and saves results
to a JSON file that the dashboard can display.

Usage:
    uv run python scripts/run_backtest.py
"""

import asyncio
import json
import math
import random
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.constants import LOT_SIZES
from src.core.models import (
    Greeks,
    OptionChain,
    OptionChainEntry,
    OptionData,
    PnL,
    Position,
    Signal,
    Tick,
)
from src.core.types import OptionType, OrderSide, SignalType
from src.options.pricing import bs_call_price, bs_put_price
from src.options.greeks import compute_greeks
from src.options.iv import compute_iv


# ─── Synthetic Market Data Generator ──────────────────────────────

class SyntheticMarket:
    """Generates realistic NIFTY intraday price movements and option premiums."""

    def __init__(self, start_date: date, num_days: int = 30, seed: int = 42):
        self.start_date = start_date
        self.num_days = num_days
        self.rng = np.random.default_rng(seed)

        # Starting NIFTY price
        self.spot = 22500.0
        self.base_iv = 0.15  # 15% annualized IV
        self.risk_free = 0.07
        self.lot_size = LOT_SIZES["NIFTY"]
        self.strike_interval = 50

        # Generate trading days (skip weekends)
        self.trading_days = []
        current = start_date
        while len(self.trading_days) < num_days:
            if current.weekday() < 5:  # Mon-Fri
                self.trading_days.append(current)
            current += timedelta(days=1)

    def generate_intraday(self, day: date) -> list[dict]:
        """Generate minute-by-minute NIFTY data for one trading day.

        Returns list of dicts with: time, spot, ce_ltp, pe_ltp, atm_strike, iv
        """
        ticks = []
        spot = self.spot

        # Daily drift (-0.5% to +0.5%) + mean reversion tendency
        daily_drift = self.rng.normal(0, 0.003)

        # Intraday pattern: higher volatility at open/close
        minutes = 375  # 9:15 to 15:30
        start_time = datetime.combine(day, time(9, 15))

        # ATM strike at market open
        atm_strike = round(spot / self.strike_interval) * self.strike_interval

        # Days to expiry (approximate: next Tuesday)
        days_to_expiry = (7 - day.weekday() + 1) % 7  # days until Tuesday
        if days_to_expiry == 0:
            days_to_expiry = 7
        T = days_to_expiry / 365.0

        for i in range(minutes):
            current_time = start_time + timedelta(minutes=i)

            # Volatility is higher at open and close
            minutes_from_open = i
            minutes_to_close = minutes - i
            intraday_vol_mult = 1.0 + 0.5 * (
                math.exp(-minutes_from_open / 30) + math.exp(-minutes_to_close / 30)
            )

            # Random walk with drift
            minute_return = (
                daily_drift / minutes
                + self.rng.normal(0, 0.0003) * intraday_vol_mult
            )
            spot *= (1 + minute_return)

            # IV smile/skew effect + intraday IV changes
            iv = self.base_iv + self.rng.normal(0, 0.005)
            iv = max(0.08, min(0.35, iv))

            # Time decay adjustment for remaining time today
            T_remaining = T - (i / minutes) / 365.0
            T_remaining = max(T_remaining, 1 / 365.0)  # at least 1 day

            # Calculate option premiums using BS model
            ce_price = bs_call_price(spot, float(atm_strike), T_remaining, self.risk_free, iv)
            pe_price = bs_put_price(spot, float(atm_strike), T_remaining, self.risk_free, iv)

            # Add realistic bid-ask spread noise
            ce_price = max(0.05, ce_price + self.rng.normal(0, 0.5))
            pe_price = max(0.05, pe_price + self.rng.normal(0, 0.5))

            ticks.append({
                "time": current_time,
                "spot": round(spot, 2),
                "atm_strike": atm_strike,
                "ce_ltp": round(ce_price, 2),
                "pe_ltp": round(pe_price, 2),
                "iv": round(iv, 4),
                "T": T_remaining,
            })

        # Update spot for next day
        self.spot = spot
        return ticks


# ─── Backtest Runner ─────────────────────────────────────────────

class BacktestRunner:
    """Runs a short straddle backtest using synthetic data."""

    def __init__(self, capital: float = 1_000_000):
        self.capital = capital
        self.lot_size = LOT_SIZES["NIFTY"]
        self.lots = 1
        self.quantity = self.lots * self.lot_size

        # Strategy params
        self.entry_time = time(9, 20)
        self.exit_time = time(15, 15)
        self.stop_loss_pct = 30.0  # exit if premium up 30%

        # Tracking
        self.trades = []
        self.daily_results = []
        self.equity_curve = []

    def run(self, market: SyntheticMarket) -> dict:
        """Run the backtest across all trading days."""
        equity = self.capital

        for day in market.trading_days:
            intraday = market.generate_intraday(day)
            result = self._run_day(day, intraday)

            equity += result["pnl"]
            result["equity"] = round(equity, 2)
            self.daily_results.append(result)
            self.equity_curve.append({
                "date": day.isoformat(),
                "equity": round(equity, 2),
                "pnl": round(result["pnl"], 2),
            })

        return self._compile_results(market)

    def _run_day(self, day: date, intraday: list[dict]) -> dict:
        """Simulate one trading day of short straddle."""
        entered = False
        entry_premium = 0.0
        entry_ce = 0.0
        entry_pe = 0.0
        exit_premium = 0.0
        exit_reason = ""
        atm_strike = 0
        entry_time_str = ""
        exit_time_str = ""
        max_premium = 0.0
        min_premium = float("inf")

        for tick in intraday:
            t = tick["time"].time()
            ce_ltp = tick["ce_ltp"]
            pe_ltp = tick["pe_ltp"]
            current_premium = ce_ltp + pe_ltp

            # Entry logic
            if not entered and t >= self.entry_time:
                entered = True
                entry_premium = current_premium
                entry_ce = ce_ltp
                entry_pe = pe_ltp
                atm_strike = tick["atm_strike"]
                entry_time_str = tick["time"].strftime("%H:%M")
                max_premium = entry_premium
                min_premium = entry_premium

                self.trades.append({
                    "date": day.isoformat(),
                    "time": entry_time_str,
                    "action": "SELL",
                    "strike": atm_strike,
                    "ce_price": round(entry_ce, 2),
                    "pe_price": round(entry_pe, 2),
                    "total_premium": round(entry_premium, 2),
                    "quantity": self.quantity,
                })
                continue

            if not entered:
                continue

            # Track premium extremes
            max_premium = max(max_premium, current_premium)
            min_premium = min(min_premium, current_premium)

            # Stop loss check
            if entry_premium > 0:
                premium_change_pct = (current_premium - entry_premium) / entry_premium * 100
                if premium_change_pct > self.stop_loss_pct:
                    exit_premium = current_premium
                    exit_reason = f"Stop loss: +{premium_change_pct:.1f}%"
                    exit_time_str = tick["time"].strftime("%H:%M")
                    break

            # Exit time check
            if t >= self.exit_time:
                exit_premium = current_premium
                exit_reason = "Exit time"
                exit_time_str = tick["time"].strftime("%H:%M")
                break

        # If still entered at end of day (shouldn't happen with exit_time logic)
        if entered and exit_premium == 0:
            exit_premium = intraday[-1]["ce_ltp"] + intraday[-1]["pe_ltp"]
            exit_reason = "EOD"
            exit_time_str = intraday[-1]["time"].strftime("%H:%M")

        # Calculate P&L: short straddle profits when premium decreases
        if entered:
            pnl_per_unit = entry_premium - exit_premium
            gross_pnl = pnl_per_unit * self.quantity

            # Estimate charges (brokerage + STT + others)
            # STT on sell side: 0.0625% of premium * qty
            stt = entry_premium * self.quantity * 0.000625
            brokerage = min(20, entry_premium * self.quantity * 0.0003) * 2  # both legs
            other_charges = gross_pnl * 0.001 if gross_pnl > 0 else 0
            total_charges = round(stt + brokerage + other_charges, 2)
            net_pnl = round(gross_pnl - total_charges, 2)

            self.trades.append({
                "date": day.isoformat(),
                "time": exit_time_str,
                "action": "BUY",
                "strike": atm_strike,
                "ce_price": round(exit_premium - entry_pe + (entry_pe - (exit_premium - exit_premium + entry_pe)), 2),
                "pe_price": round(exit_premium - entry_ce + (entry_ce - exit_premium + exit_premium - entry_ce), 2),
                "total_premium": round(exit_premium, 2),
                "quantity": self.quantity,
            })

            return {
                "date": day.isoformat(),
                "day_of_week": day.strftime("%A"),
                "atm_strike": atm_strike,
                "spot_open": intraday[0]["spot"],
                "spot_close": intraday[-1]["spot"],
                "entry_premium": round(entry_premium, 2),
                "exit_premium": round(exit_premium, 2),
                "max_premium": round(max_premium, 2),
                "min_premium": round(min_premium, 2),
                "entry_time": entry_time_str,
                "exit_time": exit_time_str,
                "exit_reason": exit_reason,
                "gross_pnl": round(gross_pnl, 2),
                "charges": total_charges,
                "pnl": net_pnl,
                "iv": intraday[len(intraday) // 2]["iv"],
            }

        return {
            "date": day.isoformat(),
            "day_of_week": day.strftime("%A"),
            "atm_strike": 0,
            "spot_open": intraday[0]["spot"],
            "spot_close": intraday[-1]["spot"],
            "entry_premium": 0, "exit_premium": 0,
            "max_premium": 0, "min_premium": 0,
            "entry_time": "", "exit_time": "",
            "exit_reason": "No entry",
            "gross_pnl": 0, "charges": 0, "pnl": 0, "iv": 0,
        }

    def _compile_results(self, market: SyntheticMarket) -> dict:
        """Compile all results into a summary dict."""
        pnls = [d["pnl"] for d in self.daily_results]
        pnl_array = np.array(pnls)

        total_pnl = float(pnl_array.sum())
        winning_days = int((pnl_array > 0).sum())
        losing_days = int((pnl_array < 0).sum())
        total_days = len(pnls)

        # Equity curve for metrics
        equity = np.array([self.capital] + [d["equity"] for d in self.daily_results])
        peak = np.maximum.accumulate(equity)
        drawdown = equity - peak
        max_drawdown = float(np.min(drawdown))

        # Sharpe ratio
        if pnl_array.std() > 0:
            sharpe = float(pnl_array.mean() / pnl_array.std() * math.sqrt(252))
        else:
            sharpe = 0.0

        # Sortino
        downside = pnl_array[pnl_array < 0]
        if len(downside) > 0 and downside.std() > 0:
            sortino = float(pnl_array.mean() / downside.std() * math.sqrt(252))
        else:
            sortino = 0.0

        # Win/loss stats
        wins = pnl_array[pnl_array > 0]
        losses = pnl_array[pnl_array < 0]
        avg_win = float(wins.mean()) if len(wins) > 0 else 0
        avg_loss = float(losses.mean()) if len(losses) > 0 else 0
        profit_factor = abs(float(wins.sum()) / float(losses.sum())) if losses.sum() != 0 else float("inf")

        total_charges = sum(d["charges"] for d in self.daily_results)

        return {
            "strategy": "Short Straddle",
            "underlying": "NIFTY",
            "period": f"{market.trading_days[0].isoformat()} to {market.trading_days[-1].isoformat()}",
            "num_days": total_days,
            "lots": self.lots,
            "lot_size": self.lot_size,
            "initial_capital": self.capital,
            "metrics": {
                "total_pnl": round(total_pnl, 2),
                "total_return_pct": round(total_pnl / self.capital * 100, 2),
                "total_charges": round(total_charges, 2),
                "net_pnl": round(total_pnl - total_charges, 2),
                "max_drawdown": round(max_drawdown, 2),
                "max_drawdown_pct": round(max_drawdown / self.capital * 100, 2),
                "sharpe_ratio": round(sharpe, 2),
                "sortino_ratio": round(sortino, 2),
                "winning_days": winning_days,
                "losing_days": losing_days,
                "win_rate": round(winning_days / total_days * 100, 1) if total_days > 0 else 0,
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
                "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else 999.99,
                "best_day": round(float(pnl_array.max()), 2),
                "worst_day": round(float(pnl_array.min()), 2),
                "avg_daily_pnl": round(float(pnl_array.mean()), 2),
            },
            "daily_results": self.daily_results,
            "equity_curve": self.equity_curve,
            "params": {
                "entry_time": self.entry_time.strftime("%H:%M"),
                "exit_time": self.exit_time.strftime("%H:%M"),
                "stop_loss_pct": self.stop_loss_pct,
                "quantity_lots": self.lots,
            },
        }


def main():
    print("=" * 60)
    print("FnO Trading System — Short Straddle Backtest")
    print("=" * 60)

    # Use a realistic start date
    start = date(2026, 2, 1)
    market = SyntheticMarket(start_date=start, num_days=30, seed=42)
    runner = BacktestRunner(capital=1_000_000)

    print(f"\nPeriod: {market.trading_days[0]} to {market.trading_days[-1]}")
    print(f"Strategy: Short Straddle on NIFTY")
    print(f"Lots: {runner.lots} ({runner.quantity} qty)")
    print(f"Entry: {runner.entry_time}, Exit: {runner.exit_time}")
    print(f"Stop Loss: {runner.stop_loss_pct}%\n")

    results = runner.run(market)

    # Print summary
    m = results["metrics"]
    print("─" * 40)
    print(f"  Total P&L:      Rs {m['total_pnl']:>12,.2f}")
    print(f"  Total Charges:  Rs {m['total_charges']:>12,.2f}")
    print(f"  Net P&L:        Rs {m['net_pnl']:>12,.2f}")
    print(f"  Return:         {m['total_return_pct']:>11.2f}%")
    print(f"  Max Drawdown:   Rs {m['max_drawdown']:>12,.2f}")
    print(f"  Sharpe Ratio:   {m['sharpe_ratio']:>11.2f}")
    print(f"  Sortino Ratio:  {m['sortino_ratio']:>11.2f}")
    print(f"  Win Rate:       {m['win_rate']:>11.1f}%")
    print(f"  Winning Days:   {m['winning_days']:>11d}")
    print(f"  Losing Days:    {m['losing_days']:>11d}")
    print(f"  Avg Win:        Rs {m['avg_win']:>12,.2f}")
    print(f"  Avg Loss:       Rs {m['avg_loss']:>12,.2f}")
    print(f"  Profit Factor:  {m['profit_factor']:>11.2f}")
    print(f"  Best Day:       Rs {m['best_day']:>12,.2f}")
    print(f"  Worst Day:      Rs {m['worst_day']:>12,.2f}")
    print("─" * 40)

    # Print daily breakdown
    print(f"\n{'Date':<12} {'Day':<10} {'Strike':>7} {'Entry':>8} {'Exit':>8} {'P&L':>10} {'Reason':<20}")
    print("─" * 80)
    for d in results["daily_results"]:
        pnl_str = f"Rs {d['pnl']:>8,.2f}"
        pnl_color = "" if d["pnl"] >= 0 else ""
        print(
            f"{d['date']:<12} {d['day_of_week']:<10} {d['atm_strike']:>7} "
            f"{d['entry_premium']:>8.2f} {d['exit_premium']:>8.2f} "
            f"{pnl_str:>10} {d['exit_reason']:<20}"
        )

    # Save results to JSON
    output_path = Path(__file__).parent.parent / "backtest_results.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to: {output_path}")


if __name__ == "__main__":
    main()
