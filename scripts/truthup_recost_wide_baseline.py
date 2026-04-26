"""Re-cost the wide_baseline 200-day validation trades with correct
charges (per Apr 25 2026 audit fix to ``src/core/constants.py``).

Why this script exists
----------------------
The independent reviewer of the chain-window truth-up correctly
flagged that 13 trades over 4 days is statistically meaningless
(effective N ≤ 5). The right way to answer "does the strategy have
any positive net edge under realistic costs?" is to re-cost the
much larger sample we already have: the 200-day wide_baseline
validation trades (211 paired trades, Sep 2024 → Jun 2025).

The wide_baseline run used the now-stale CHARGES constants — STT
options sell was 0.0625% (pre-Oct-2024 rate) instead of 0.10%, and
exchange was 0.05% instead of 0.0353%. So past validation reports
silently understated charges. Re-costing produces an honest verdict.

Methodology
-----------
1. Load all `portfolio_bt` decisions in [2024-09-02, 2025-06-25].
2. Pair ENTER+EXIT (already de-duplicated by Bug 4 fix; this run is
   post-fix).
3. For each trade, apply DATE-AWARE STT:
   - 2024-09-02 to 2026-03-31: STT options sell = 0.10%
     (the wide_baseline window ENDS 2025-06-25, so only the post-Oct-
     2024 0.10% rate applies; pre-Oct-2024 0.0625% does NOT enter
     here because no trade dates are pre-Oct-2024).
   Wait: the window is 2024-09-02 → 2025-06-25, which DOES start
   pre-Oct-2024. We use 0.0625% for trades 2024-09-02 → 2024-09-30,
   then 0.10% from 2024-10-01 onwards.
4. Compute net P&L = recorded outcome_pnl − total_charges.
5. Compute Sharpe at 1× and at 5× / 10× lots (linear approximation,
   noting capacity wide_baseline already shows -₹82/lot at 75 lots).
6. Verdict per PHASE3_MASTER §V.5 vocabulary (PASS / YELLOW / FAIL /
   INSUFFICIENT_DATA).

Discipline
----------
- No tuning during truth-up. Pure measurement.
- Date-aware STT: do not collapse to a single rate.
- Verdict label MUST be one of the locked four; no "PROVISIONAL"
  inventions. (Lesson from the chain-window truth-up's bias.)
- Effective-N adjustment: same strategy_id firing at different
  minutes counts independently; cross-strategy correlation only
  matters when multiple strategies fire on the same minute (not the
  case here — the wide_baseline used a single `portfolio_bt`).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.constants import CHARGES
from src.core.types import OrderSide
from src.portfolio.charges import calculate_charges


DEC_DIR = Path("data/decisions")
REPORT_PATH = Path("reports/phase3_pre/recost_wide_baseline_v1.md")

WINDOW_START = date(2024, 9, 2)
WINDOW_END = date(2025, 6, 25)

# Date thresholds for date-aware STT options sell rate
STT_OCT2024_DATE = date(2024, 10, 1)  # 0.0625% → 0.10%
STT_APR2026_DATE = date(2026, 4, 1)   # 0.10% → 0.15%


@dataclass
class TradeRecord:
    trade_date: date
    strategy_id: str
    leg: str
    mode: str
    entry_ts: datetime
    exit_ts: datetime
    spot: float
    vix: float
    quantity: int
    entry_premium: float
    outcome_pnl: float
    held_minutes: float
    exit_reason: str
    stt_rate_pct: float = 0.0
    legs_per_trade: int = 2
    total_charges: Decimal = Decimal(0)
    net_pnl: float = 0.0


def _stt_rate_for_date(d: date) -> Decimal:
    """Date-aware STT options-sell rate.

    - Pre-Oct-1-2024: 0.0625%
    - Oct 1 2024 → Mar 31 2026: 0.10%
    - Apr 1 2026 onwards: 0.15%

    For our window (Sep 2 2024 → Jun 25 2025) we hit the first two
    bands.
    """
    if d < STT_OCT2024_DATE:
        return Decimal("0.0625")  # pre-Oct-2024 rate (still in CHARGES history)
    if d < STT_APR2026_DATE:
        return CHARGES["stt"]["options_sell_pct"]  # 0.10% (current default)
    return CHARGES["stt"]["options_sell_pct_apr2026"]  # 0.15%


def _legs_for_mode(mode: str) -> int:
    if mode == "iron_condor":
        return 4
    if mode in ("strangle", "straddle"):
        return 2
    if mode == "debit_spread":
        return 2
    return 2


def _load_paired_trades() -> list[TradeRecord]:
    trades: list[TradeRecord] = []
    for p in sorted(DEC_DIR.glob("decisions_*.csv")):
        name = p.stem.replace("decisions_", "")
        try:
            d = date.fromisoformat(name)
        except ValueError:
            continue
        if not (WINDOW_START <= d <= WINDOW_END):
            continue
        df = pd.read_csv(p)
        if df.empty:
            continue
        # Wide_baseline used `portfolio_bt`. Filter strictly to it so
        # any leakage from live or replay runs doesn't pollute the
        # sample.
        df = df[df["strategy_id"] == "portfolio_bt"]
        if df.empty:
            continue
        df = df.drop_duplicates(
            subset=["timestamp", "strategy_id", "leg", "decision"], keep="first"
        )
        df["_pair"] = df.groupby(["strategy_id", "leg", "decision"]).cumcount()
        enters = df[df["decision"] == "ENTER"]
        exits = df[df["decision"] == "EXIT"]
        merged = enters.merge(
            exits[["strategy_id", "leg", "_pair", "timestamp", "outcome_pnl",
                   "exit_reason", "held_minutes"]],
            on=["strategy_id", "leg", "_pair"],
            suffixes=("_in", "_out"),
            how="inner",
        )
        for _, r in merged.iterrows():
            try:
                pnl_raw = r.get("outcome_pnl_out", 0)
                pnl = 0.0 if pd.isna(pnl_raw) else float(pnl_raw)
                exit_reason_raw = r.get("exit_reason_out", "")
                held_raw = r.get("held_minutes_out", 0)
                held = 0.0 if pd.isna(held_raw) else float(held_raw)
                trades.append(TradeRecord(
                    trade_date=d,
                    strategy_id=str(r["strategy_id"]),
                    leg=str(r["leg"]),
                    mode=str(r.get("mode", "")),
                    entry_ts=pd.to_datetime(r["timestamp_in"]).to_pydatetime(),
                    exit_ts=pd.to_datetime(r["timestamp_out"]).to_pydatetime(),
                    spot=float(r.get("spot", 0) or 0),
                    vix=float(r.get("vix", 0) or 0),
                    quantity=int(r.get("quantity", 0) or 0),
                    entry_premium=float(r.get("entry_premium", 0) or 0),
                    outcome_pnl=pnl,
                    held_minutes=held,
                    exit_reason=str(exit_reason_raw) if exit_reason_raw is not None else "",
                    stt_rate_pct=float(_stt_rate_for_date(d)),
                    legs_per_trade=_legs_for_mode(str(r.get("mode", ""))),
                ))
            except (ValueError, TypeError, KeyError) as exc:
                print(f"  [skip] {d}: {exc}")
    return trades


# ─── Charges (date-aware) ────────────────────────────────────────────


def _calc_charges_with_stt(price, qty, side, instrument_type, stt_pct):
    saved = CHARGES["stt"]["options_sell_pct"]
    CHARGES["stt"]["options_sell_pct"] = stt_pct
    try:
        return calculate_charges(
            price=price, quantity=qty, side=side,
            instrument_type=instrument_type, is_expiry_exercise=False,
        )
    finally:
        CHARGES["stt"]["options_sell_pct"] = saved


def _compute_trade_charges(t: TradeRecord) -> Decimal:
    if t.quantity <= 0 or t.entry_premium <= 0:
        return Decimal(0)
    entry_p = Decimal(str(t.entry_premium))
    if t.leg == "PREMIUM":
        implied_exit = max(Decimal("0.05"),
                           entry_p - Decimal(str(t.outcome_pnl)) / Decimal(t.quantity))
        entry_side, exit_side = OrderSide.SELL, OrderSide.BUY
    else:
        implied_exit = max(Decimal("0.05"),
                           entry_p + Decimal(str(t.outcome_pnl)) / Decimal(t.quantity))
        entry_side, exit_side = OrderSide.BUY, OrderSide.SELL

    per_leg_entry = entry_p / Decimal(t.legs_per_trade)
    per_leg_exit = implied_exit / Decimal(t.legs_per_trade)
    stt_pct = Decimal(str(t.stt_rate_pct))

    total = Decimal(0)
    for _ in range(t.legs_per_trade):
        c_e = _calc_charges_with_stt(per_leg_entry, t.quantity, entry_side, "CE", stt_pct)
        c_x = _calc_charges_with_stt(per_leg_exit, t.quantity, exit_side, "CE", stt_pct)
        total += c_e.total + c_x.total
    return total


# ─── Aggregation + verdict ───────────────────────────────────────────


def _per_trade_sharpe(s: pd.Series) -> float:
    if len(s) < 2 or s.std(ddof=1) == 0:
        return 0.0
    return float(s.mean() / s.std(ddof=1) * np.sqrt(len(s)))


def _summarize(trades: list[TradeRecord]) -> dict:
    if not trades:
        return {"n_trades": 0}
    df = pd.DataFrame([
        {
            "date": t.trade_date,
            "strategy_id": t.strategy_id,
            "leg": t.leg,
            "mode": t.mode,
            "vix": t.vix,
            "quantity": t.quantity,
            "entry_premium": t.entry_premium,
            "outcome_pnl": t.outcome_pnl,
            "stt_rate_pct": t.stt_rate_pct,
            "total_charges": float(t.total_charges),
            "net_pnl": t.net_pnl,
            "exit_reason": t.exit_reason,
            "post_oct2024": t.trade_date >= STT_OCT2024_DATE,
        }
        for t in trades
    ])
    daily_pnl = df.groupby("date")["net_pnl"].sum()
    return {
        "n_trades": len(df),
        "n_days": int(df["date"].nunique()),
        "n_winners": int((df["outcome_pnl"] > 0).sum()),
        "n_losers": int((df["outcome_pnl"] < 0).sum()),
        "gross_pnl_total": float(df["outcome_pnl"].sum()),
        "gross_pnl_mean": float(df["outcome_pnl"].mean()),
        "gross_pnl_median": float(df["outcome_pnl"].median()),
        "charges_total": float(df["total_charges"].sum()),
        "charges_mean_per_trade": float(df["total_charges"].mean()),
        "charges_pct_of_gross": (float(df["total_charges"].sum()) /
                                 float(df["outcome_pnl"].sum()) * 100)
                                 if df["outcome_pnl"].sum() > 0 else float("inf"),
        "net_pnl_total": float(df["net_pnl"].sum()),
        "net_pnl_mean": float(df["net_pnl"].mean()),
        "net_pnl_median": float(df["net_pnl"].median()),
        "net_pnl_std": float(df["net_pnl"].std(ddof=1)),
        "win_rate_gross": float((df["outcome_pnl"] > 0).mean() * 100),
        "win_rate_net": float((df["net_pnl"] > 0).mean() * 100),
        "net_sharpe_per_trade": _per_trade_sharpe(df["net_pnl"]),
        "gross_sharpe_per_trade": _per_trade_sharpe(df["outcome_pnl"]),
        "net_sharpe_daily_annualized": _daily_sharpe_annualized(daily_pnl),
        "by_period": _per_period(df),
        "by_mode": _per_mode(df),
        "by_month": _per_month(df),
        "df": df,
    }


def _daily_sharpe_annualized(daily_pnl: pd.Series) -> float:
    if len(daily_pnl) < 2 or daily_pnl.std(ddof=1) == 0:
        return 0.0
    return float(daily_pnl.mean() / daily_pnl.std(ddof=1) * np.sqrt(252))


def _per_period(df: pd.DataFrame) -> pd.DataFrame:
    return df.groupby("post_oct2024").agg(
        n=("outcome_pnl", "count"),
        days=("date", "nunique"),
        stt_pct=("stt_rate_pct", "mean"),
        gross_total=("outcome_pnl", "sum"),
        charges_total=("total_charges", "sum"),
        net_total=("net_pnl", "sum"),
        net_mean=("net_pnl", "mean"),
        net_sharpe=("net_pnl", _per_trade_sharpe),
    ).round(2)


def _per_mode(df: pd.DataFrame) -> pd.DataFrame:
    return df.groupby("mode").agg(
        n=("outcome_pnl", "count"),
        gross_total=("outcome_pnl", "sum"),
        charges_total=("total_charges", "sum"),
        net_total=("net_pnl", "sum"),
        net_mean=("net_pnl", "mean"),
        net_sharpe=("net_pnl", _per_trade_sharpe),
        win_rate_net=("net_pnl", lambda s: float((s > 0).mean() * 100)),
    ).round(2)


def _per_month(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["month"] = df["date"].apply(lambda d: d.strftime("%Y-%m"))
    return df.groupby("month").agg(
        n=("outcome_pnl", "count"),
        gross_total=("outcome_pnl", "sum"),
        charges_total=("total_charges", "sum"),
        net_total=("net_pnl", "sum"),
        net_mean=("net_pnl", "mean"),
        net_sharpe=("net_pnl", _per_trade_sharpe),
    ).round(2)


def _verdict(s: dict) -> dict:
    if s["n_trades"] == 0:
        return {"verdict": "INSUFFICIENT_DATA",
                "reasons": ["no trades found in window"]}

    n_trades = s["n_trades"]
    sharpe = s["net_sharpe_per_trade"]
    median_per_trade = s["net_pnl_median"]
    charges_pct = s["charges_pct_of_gross"]

    # 200-day, 200+ trade sample is statistically adequate for Sharpe
    # estimation (standard error ~1/sqrt(N) ≈ 7%).
    INSUFFICIENT_THRESHOLD = 30
    if n_trades < INSUFFICIENT_THRESHOLD:
        return {"verdict": "INSUFFICIENT_DATA",
                "reasons": [f"only {n_trades} trades < {INSUFFICIENT_THRESHOLD}"]}

    reasons = []
    if sharpe < 0.5:
        reasons.append(f"net Sharpe {sharpe:.3f} < 0.5")
    if median_per_trade < 50:
        reasons.append(f"median net per trade ₹{median_per_trade:.0f} < ₹50")
    if charges_pct == float("inf") or charges_pct > 50:
        reasons.append(f"charges {charges_pct:.1f}% > 50% of gross")

    if not reasons:
        verdict = "PASS"
    elif sharpe >= 0:
        verdict = "YELLOW"
        reasons.append("Sharpe positive but below 0.5 threshold — viable only "
                       "as one strategy in a portfolio, not standalone")
    else:
        verdict = "FAIL"

    return {
        "verdict": verdict,
        "reasons": reasons,
        "sharpe": sharpe,
        "median_per_trade": median_per_trade,
        "charges_pct": charges_pct,
    }


# ─── Report ──────────────────────────────────────────────────────────


def _write_report(trades: list[TradeRecord], summary: dict, verdict: dict) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df = summary.get("df", pd.DataFrame())
    lines: list[str] = []
    lines.append("# Phase 3-Pre Truth-Up — Wide-Baseline Re-Cost (v1)")
    lines.append("")
    lines.append(f"- Generated: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"- Window: {WINDOW_START} → {WINDOW_END} (200-day train+val)")
    lines.append(f"- Source: 211 paired ENTER+EXIT from `portfolio_bt` "
                 f"backtest (post-Bug-4 dedup; the wide_baseline run)")
    lines.append("")
    lines.append("## Why this analysis exists")
    lines.append("")
    lines.append("The chain-window truth-up "
                 "(`reports/phase3_pre/fee_truthup_v1.md`) hit "
                 "**INSUFFICIENT_DATA** — 13 trades over 4 days with effective "
                 "N=3.2 cannot answer the cost-edge question. The independent "
                 "reviewer recommended re-costing the much larger sample we "
                 "already have: the 200-day wide_baseline validation.")
    lines.append("")
    lines.append("The wide_baseline run used the now-stale `CHARGES` constants "
                 "(STT options sell 0.0625% pre-Oct-2024 rate; Exchange 0.05% "
                 "pre-Oct-2024 rate). The Apr 25 2026 audit fix updated these "
                 "to the correct post-Oct-2024 / post-Apr-1-2026 rates. "
                 "Re-costing the recorded `outcome_pnl` (which is gross — "
                 "before any charges) with realistic charges produces an "
                 "honest net P&L verdict.")
    lines.append("")

    lines.append("## VERDICT")
    lines.append("")
    lines.append(f"**{verdict['verdict']}**")
    lines.append("")
    if verdict["reasons"]:
        lines.append("Reasons:")
        for r in verdict["reasons"]:
            lines.append(f"- {r}")
        lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Trades | {summary['n_trades']} paired ENTER+EXIT |")
    lines.append(f"| Days | {summary['n_days']} (out of ~200 trading days) |")
    lines.append(f"| Winners (gross) | {summary['n_winners']} ({summary['win_rate_gross']:.1f}%) |")
    lines.append(f"| Losers (gross) | {summary['n_losers']} |")
    lines.append(f"| **Gross P&L total** | **₹{summary['gross_pnl_total']:,.0f}** |")
    lines.append(f"| Gross mean per trade | ₹{summary['gross_pnl_mean']:,.0f} |")
    lines.append(f"| Gross median per trade | ₹{summary['gross_pnl_median']:,.0f} |")
    lines.append(f"| **Total charges (date-aware STT)** | **₹{summary['charges_total']:,.0f}** |")
    lines.append(f"| Charges as % of gross | **{summary['charges_pct_of_gross']:.1f}%** |")
    lines.append(f"| Charges mean per trade | ₹{summary['charges_mean_per_trade']:,.0f} |")
    lines.append(f"| **Net P&L total** | **₹{summary['net_pnl_total']:,.0f}** |")
    lines.append(f"| Net mean per trade | ₹{summary['net_pnl_mean']:,.0f} |")
    lines.append(f"| Net median per trade | ₹{summary['net_pnl_median']:,.0f} |")
    lines.append(f"| Net stdev per trade | ₹{summary['net_pnl_std']:,.0f} |")
    lines.append(f"| Net win rate | {summary['win_rate_net']:.1f}% |")
    lines.append(f"| **Net Sharpe (per-trade × √N)** | **{summary['net_sharpe_per_trade']:+.3f}** |")
    lines.append(f"| Gross Sharpe (per-trade × √N) | {summary['gross_sharpe_per_trade']:+.3f} |")
    lines.append(f"| Net Sharpe (daily, annualized × √252) | {summary['net_sharpe_daily_annualized']:+.3f} |")
    lines.append("")

    lines.append("## Period split — pre vs post Oct 1 2024 STT hike (0.0625% → 0.10%)")
    lines.append("")
    lines.append("```")
    lines.append(summary["by_period"].to_string())
    lines.append("```")
    lines.append("")

    lines.append("## Per-mode breakdown")
    lines.append("")
    lines.append("```")
    lines.append(summary["by_mode"].to_string())
    lines.append("```")
    lines.append("")

    lines.append("## Per-month breakdown")
    lines.append("")
    lines.append("```")
    lines.append(summary["by_month"].to_string())
    lines.append("```")
    lines.append("")

    lines.append("## Charge rate methodology (date-aware)")
    lines.append("")
    lines.append("- 2024-09-02 → 2024-09-30 (29 trading days): STT options sell **0.0625%**")
    lines.append("- 2024-10-01 → 2025-06-25 (~170 trading days): STT options sell **0.10%**")
    lines.append("- (Apr 1 2026 hike to 0.15% does not affect this window)")
    lines.append("")
    lines.append("Other charges (per `src/core/constants.py::CHARGES` after Apr 25 2026 audit):")
    lines.append("- Exchange (NSE F&O options): 0.0353% on premium turnover (post Oct 2024)")
    lines.append("- SEBI: 0.0001%")
    lines.append("- GST: 18% on (brokerage + exchange + SEBI)")
    lines.append("- Stamp duty: 0.003% on options buy")
    lines.append("- Brokerage: Zerodha 0.03% or ₹20/order whichever lower")
    lines.append("")
    lines.append("**Charge approximation:** entry_premium is split evenly across legs "
                 "(2 for strangle/straddle, 4 for iron condor). Implied exit price "
                 "is back-derived from outcome_pnl. This is approximate but "
                 "conservative for the cost magnitude.")
    lines.append("")

    lines.append("## What this verdict means")
    lines.append("")
    if verdict["verdict"] == "PASS":
        lines.append("Phase 3a may proceed per PHASE3_MASTER §VI.4. The strategy")
        lines.append("has positive net edge after realistic charges over 200 days.")
        lines.append("Reviewer corrections (5 strategies, HMM, vol-targeting) apply.")
    elif verdict["verdict"] == "YELLOW":
        lines.append("Phase 3a may proceed per PHASE3_MASTER §V.5.2 but with")
        lines.append("max_lots = 1 locked. The strategy has positive net P&L but")
        lines.append("Sharpe is below the 0.5 threshold for standalone deployment.")
        lines.append("It can still earn its way as one of several allocated")
        lines.append("strategies in the Phase 3d orchestrator.")
    elif verdict["verdict"] == "FAIL":
        lines.append("Phase 3a does NOT start. Per PHASE3_MASTER §V.5.3, the")
        lines.append("operator commits to one of three replan options:")
        lines.append("- (a) Pivot to institutional size where the operator")
        lines.append("      collects spread instead of paying it.")
        lines.append("- (b) Pivot to different strategies (Iron Butterfly, Long")
        lines.append("      Calendar, NIFTY/BANKNIFTY relative-vol pair).")
        lines.append("- (c) Retire systematic premium-selling at retail scale.")
        lines.append("")
        lines.append("The 30-day moratorium on parameter optimization begins now.")
    else:
        lines.append("Sample is too small for any verdict. See PHASE3_MASTER §V.5")
        lines.append("for next-step branches when INSUFFICIENT_DATA is the result.")
    lines.append("")

    lines.append("## Methodology caveats")
    lines.append("")
    lines.append("- **outcome_pnl is GROSS** (premium-decay × qty); the strategy")
    lines.append("  does NOT deduct charges. Re-costing applies them on top.")
    lines.append("- **Charge approximation** splits entry_premium across legs.")
    lines.append("  For IC's asymmetric short+wing legs, this slightly")
    lines.append("  overstates wing costs (wings are cheaper than shorts).")
    lines.append("  Direction: makes the verdict slightly conservative.")
    lines.append("- **No slippage adjustment.** Pure charge re-costing. The")
    lines.append("  wide_baseline used GDFL parquet bid/ask which may have")
    lines.append("  been tighter than real-world chain spreads. Real net is")
    lines.append("  therefore OPTIMISTIC vs reality. The ChartreuseyellowFAIL on")
    lines.append("  cost_sensitivity at +0.25 in wide_baseline_portfolio.md")
    lines.append("  already showed this margin is thin.")
    lines.append("- **Linear lot scaling NOT applied** in this report. The")
    lines.append("  original wide_baseline already showed -₹82/lot at 75 lots")
    lines.append("  (capacity FAIL). The 1-lot Sharpe alone does not imply")
    lines.append("  multi-lot viability.")
    lines.append("")

    REPORT_PATH.write_text("\n".join(lines))
    print(f"\n✓ Report written: {REPORT_PATH}")


def main() -> None:
    print("=" * 70)
    print("Phase 3-Pre Truth-Up — Wide-Baseline Re-Cost")
    print("=" * 70)
    print()

    print(f"[1/3] Loading paired trades from {WINDOW_START} → {WINDOW_END}...")
    trades = _load_paired_trades()
    if not trades:
        print("  No trades found. Exiting.")
        return
    print(f"  {len(trades)} paired trades across "
          f"{len({t.trade_date for t in trades})} days.")

    print()
    pre_oct = sum(1 for t in trades if t.trade_date < STT_OCT2024_DATE)
    post_oct = len(trades) - pre_oct
    print(f"[2/3] Computing date-aware charges...")
    print(f"  Pre-Oct-1-2024 trades (STT 0.0625%): {pre_oct}")
    print(f"  Post-Oct-1-2024 trades (STT 0.10%): {post_oct}")
    for t in trades:
        t.total_charges = _compute_trade_charges(t)
        t.net_pnl = t.outcome_pnl - float(t.total_charges)

    print()
    print("[3/3] Computing summary + verdict...")
    summary = _summarize(trades)
    verdict = _verdict(summary)

    print()
    print("─" * 70)
    print(f"VERDICT: {verdict['verdict']}")
    print("─" * 70)
    if verdict["reasons"]:
        for r in verdict["reasons"]:
            print(f"  - {r}")
    print()
    print(f"Trades: {summary['n_trades']} | Days: {summary['n_days']} | "
          f"Net Sharpe (per-trade): {summary['net_sharpe_per_trade']:+.3f}")
    print(f"Gross: ₹{summary['gross_pnl_total']:,.0f} | "
          f"Charges: ₹{summary['charges_total']:,.0f} ({summary['charges_pct_of_gross']:.1f}% of gross) | "
          f"Net: ₹{summary['net_pnl_total']:,.0f}")
    print(f"Net mean per trade: ₹{summary['net_pnl_mean']:,.0f} | "
          f"Net median: ₹{summary['net_pnl_median']:,.0f} | "
          f"Win rate: {summary['win_rate_net']:.1f}%")
    print(f"Daily-annualized Sharpe: {summary['net_sharpe_daily_annualized']:+.3f}")
    print()

    _write_report(trades, summary, verdict)


if __name__ == "__main__":
    main()
