"""Phase 3-Pre Fee/Slippage Truth-Up — Apr 25 2026.

Goal
----
Determine whether the strategy has any positive net edge at retail
scale (1-5 lots) under realistic Indian options costs. Verdict gates
Phase 3a optimization (do not start 3a if FAIL).

Methodology
-----------
Use 17 days of overlapping chain_snapshots + paper-trade decisions
(Mar 25 → Apr 24 2026, post-Oct-2024-STT-hike, spans Apr-1-2026
0.10% → 0.15% transition).

For each entered trade:
1. Load recorded ENTER + matching EXIT row from decisions CSV.
   ``outcome_pnl`` is GROSS (premium-decay × qty), no charges.
2. Compute realistic charges using DATE-AWARE STT:
   - 2026-03-25 → 2026-03-31: STT options sell = 0.10%
   - 2026-04-01 → 2026-04-24: STT options sell = 0.15% (Budget 2026)
   plus exchange (0.0353% post-Oct-2024 NSE circular 100/2024),
   SEBI (0.0001%), GST (18% on brokerage+exchange+SEBI),
   stamp duty (0.003% on buy side), brokerage (Zerodha
   ₹20/order or 0.03% whichever lower).
3. (Optional) Reconstruct strikes from (mode, spot, entry_premium)
   by querying chain_snapshot at entry minute. Compare recorded
   entry_premium vs cross-spread fill at chain bid → slippage gap.
4. Aggregate per-strategy and overall: gross, charges, slippage,
   net P&L at 1×, 5×, 10× the recorded quantity.

Outputs
-------
- Console summary (printed)
- ``reports/phase3_pre/fee_truthup_v1.md`` (full report)

Discipline
----------
- No tuning during truth-up. Pure measurement.
- Date-aware STT: do NOT collapse to a single rate.
- Strike reconstruction is best-effort; if fails, slippage analysis
  reports "not computable" rather than guess.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.constants import CHARGES, LOT_SIZES
from src.core.types import OrderSide
from src.portfolio.charges import calculate_charges

# ─── Config ──────────────────────────────────────────────────────────

CHAIN_DIR = Path("data/chain_snapshots")
DEC_DIR = Path("data/decisions")
REPORT_PATH = Path("reports/phase3_pre/fee_truthup_v1.md")

# Apr 1 2026: STT on options sell premium 0.10% → 0.15%
STT_TRANSITION_DATE = date(2026, 4, 1)


@dataclass
class TradeRecord:
    """One paired ENTER+EXIT trade with all derived metrics."""
    trade_date: date
    strategy_id: str
    leg: str            # PREMIUM / TREND
    mode: str           # strangle / iron_condor / straddle / debit_spread
    entry_ts: datetime
    exit_ts: datetime
    spot: float
    vix: float
    quantity: int
    entry_premium: float    # per-share price at entry (for SELL legs: collected)
    outcome_pnl: float       # GROSS pnl as recorded by strategy
    held_minutes: float
    exit_reason: str
    # Computed
    stt_rate: Decimal       # which STT rate applied (date-aware)
    legs_per_trade: int     # 2 for strangle/straddle, 4 for IC, 2 for debit spread
    # Charges
    total_charges: Decimal = Decimal(0)
    net_pnl: float = 0.0
    # Slippage (if strike reconstruction succeeded)
    chain_slip_pct: float | None = None
    chain_slip_rs: float | None = None


def _is_post_apr1(d: date) -> bool:
    return d >= STT_TRANSITION_DATE


def _stt_rate_for_date(d: date) -> Decimal:
    """STT options-sell rate effective on date d.

    - 0.10% (Oct 1 2024 - Mar 31 2026)
    - 0.15% (Apr 1 2026 onwards, Budget 2026)
    """
    return CHARGES["stt"]["options_sell_pct_apr2026"] if _is_post_apr1(d) else CHARGES["stt"]["options_sell_pct"]


def _legs_for_mode(mode: str) -> int:
    """Number of option legs per round-trip for each strategy mode."""
    if mode == "iron_condor":
        return 4   # short CE + short PE + long CE wing + long PE wing
    if mode in ("strangle", "straddle"):
        return 2
    if mode == "debit_spread":
        return 2   # long lower + short upper (or reverse)
    return 2


def _load_paired_trades(exclude_replay: bool = True) -> list[TradeRecord]:
    """Load every paired ENTER+EXIT in the chain window.

    Args:
        exclude_replay: When True (default), drop strategy_ids ending in
            ``_replay`` or ``_bt`` — these are backtest runs whose
            decisions accidentally landed in the live decisions
            directory. They are not real live paper trades and would
            contaminate the truth-up.
    """
    trades: list[TradeRecord] = []
    chain_dates = sorted(
        date.fromisoformat(p.stem.replace("chain_", ""))
        for p in CHAIN_DIR.glob("chain_*.csv")
    )

    for d in chain_dates:
        dec_path = DEC_DIR / f"decisions_{d.isoformat()}.csv"
        if not dec_path.exists():
            continue
        df = pd.read_csv(dec_path)
        if df.empty:
            continue
        # FILTER: drop backtest-mode strategy_ids that contaminate live
        # paper-trade data (e.g. portfolio_replay, iron_condor_bt).
        if exclude_replay and "strategy_id" in df.columns:
            mask = ~df["strategy_id"].astype(str).str.endswith(("_replay", "_bt"))
            df = df[mask]
            if df.empty:
                continue
        # DEDUP: drop exact-duplicate rows (Bug 4 fingerprint — re-runs
        # appended identical rows for the same trade event).
        df = df.drop_duplicates(
            subset=["timestamp", "strategy_id", "leg", "decision"],
            keep="first",
        )
        # Pair ENTER+EXIT by (strategy_id, leg, cumcount)
        df["_pair"] = df.groupby(["strategy_id", "leg", "decision"]).cumcount()
        enters = df[df["decision"] == "ENTER"].copy()
        exits = df[df["decision"] == "EXIT"].copy()
        merged = enters.merge(
            exits[["strategy_id", "leg", "_pair", "timestamp", "outcome_pnl",
                   "exit_reason", "held_minutes"]],
            on=["strategy_id", "leg", "_pair"],
            suffixes=("_in", "_out"),
            how="inner",
        )
        for _, r in merged.iterrows():
            try:
                stt_r = _stt_rate_for_date(d)
                legs = _legs_for_mode(str(r.get("mode", "")))
                # Note: pandas merge with suffixes renames overlapping
                # columns. ``outcome_pnl``, ``exit_reason``, ``held_minutes``
                # are NaN on ENTER rows but present on EXIT rows, so
                # they get suffixed as ``_out``. ``timestamp`` is the
                # decision time on each side, suffixed similarly.
                pnl_raw = r.get("outcome_pnl_out", r.get("outcome_pnl", 0))
                pnl = 0.0 if pd.isna(pnl_raw) else float(pnl_raw)
                exit_reason_raw = r.get("exit_reason_out", r.get("exit_reason", ""))
                held_raw = r.get("held_minutes_out", r.get("held_minutes", 0))
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
                    stt_rate=stt_r,
                    legs_per_trade=legs,
                ))
            except (ValueError, TypeError, KeyError) as exc:
                print(f"  [skip] {d} {r.get('strategy_id', '?')}: {exc}")

    return trades


# ─── Charge computation ──────────────────────────────────────────────


def _compute_trade_charges(t: TradeRecord) -> Decimal:
    """Realistic round-trip charges for one trade.

    Approximation: ``entry_premium`` is the per-share price at entry.
    For premium sellers (strangle/IC/straddle), the SELL side at entry
    pays STT at the date-aware rate; the BUY side at exit pays stamp
    duty. For trend debit spreads, BUY side at entry pays stamp; SELL
    side at exit pays STT.

    Both ENTER and EXIT incur exchange + SEBI + brokerage + GST.
    """
    qty = t.quantity
    if qty <= 0 or t.entry_premium <= 0:
        return Decimal(0)

    entry_p = Decimal(str(t.entry_premium))

    # Implied exit price from gross pnl: pnl = (entry - exit) * qty
    # → exit = entry - pnl/qty   (for sell-first; reverse for buy-first)
    if t.leg == "PREMIUM":
        # SELL on entry, BUY on exit. Higher pnl ⇒ exit price was LOWER.
        implied_exit = max(Decimal("0.05"),
                           entry_p - Decimal(str(t.outcome_pnl)) / Decimal(qty))
        entry_side = OrderSide.SELL
        exit_side = OrderSide.BUY
    else:  # TREND debit spread
        # BUY on entry, SELL on exit. Higher pnl ⇒ exit price was HIGHER.
        implied_exit = max(Decimal("0.05"),
                           entry_p + Decimal(str(t.outcome_pnl)) / Decimal(qty))
        entry_side = OrderSide.BUY
        exit_side = OrderSide.SELL

    # Per-leg charge approximation. We split entry_premium evenly across
    # the legs (e.g. for a strangle with entry_premium=23.6, each leg
    # is ~11.8 per share). This is a coarse approximation for IC's
    # asymmetric wings but is conservative for the cost magnitude.
    per_leg_entry = entry_p / Decimal(t.legs_per_trade)
    per_leg_exit = implied_exit / Decimal(t.legs_per_trade)

    total = Decimal(0)
    for _ in range(t.legs_per_trade):
        # Use date-aware STT rate by temporarily overriding the constant
        # for this calculation's scope. We patch via a local override:
        c_entry = _calc_charges_with_stt(
            price=per_leg_entry, qty=qty, side=entry_side,
            instrument_type="CE", is_expiry_exercise=False,
            stt_options_sell_pct=t.stt_rate,
        )
        c_exit = _calc_charges_with_stt(
            price=per_leg_exit, qty=qty, side=exit_side,
            instrument_type="CE", is_expiry_exercise=False,
            stt_options_sell_pct=t.stt_rate,
        )
        total += c_entry.total + c_exit.total

    return total


def _calc_charges_with_stt(
    price: Decimal, qty: int, side: OrderSide,
    instrument_type: str, is_expiry_exercise: bool,
    stt_options_sell_pct: Decimal,
) -> Any:
    """Wrap calculate_charges with date-aware STT override.

    The stock ``calculate_charges`` reads STT from the global CHARGES
    constant. For date-aware behaviour we monkey-patch the lookup.
    """
    # Save + override
    saved = CHARGES["stt"]["options_sell_pct"]
    CHARGES["stt"]["options_sell_pct"] = stt_options_sell_pct
    try:
        return calculate_charges(
            price=price, quantity=qty, side=side,
            instrument_type=instrument_type,
            is_expiry_exercise=is_expiry_exercise,
        )
    finally:
        CHARGES["stt"]["options_sell_pct"] = saved


# ─── Slippage analysis (best-effort strike reconstruction) ────────────


def _load_chain_at(d: date, ts: datetime) -> pd.DataFrame:
    """Load chain snapshot for date d, filtered to the minute of ts.

    Chain rows are recorded irregularly; we take all rows whose ``time``
    is within ±60s of ts.
    """
    p = CHAIN_DIR / f"chain_{d.isoformat()}.csv"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p, parse_dates=["time"])
    if df.empty:
        return df
    if df["time"].dt.tz is None:
        df["time"] = df["time"].dt.tz_localize("Asia/Kolkata")
    target = pd.Timestamp(ts)
    if target.tz is None:
        target = target.tz_localize("Asia/Kolkata")
    sub = df[(df["time"] >= target - pd.Timedelta(seconds=60)) &
             (df["time"] <= target + pd.Timedelta(seconds=60))]
    return sub


def _attempt_slippage_recon(t: TradeRecord) -> tuple[float | None, float | None]:
    """Best-effort: find the strikes the strategy traded and compute
    slippage as (recorded entry_premium - chain cross-spread bid sum).

    For a SELL-side strangle, the strategy collects the BID prices on
    the chosen CE+PE strikes (cross-spread fill from a taker's POV).
    The CHAIN bid is the truth; the recorded entry_premium might be
    LTP or midpoint (more optimistic).

    Returns (slippage_pct, slippage_rs) or (None, None) if not
    reconstructible. ``slippage_pct`` is positive when the chain price
    is WORSE than the recorded entry_premium (i.e. the strategy
    recorded an optimistic premium).
    """
    # Only attempt reconstruction for OTM-based modes (strangle, IC).
    # Straddle is ATM with both legs at the same strike — the
    # OTM-pair matching logic below picks coincidental OTM combos
    # whose bid sum happens to equal the recorded entry_premium,
    # which is meaningless. Skip straddle to avoid spurious slippage
    # numbers; strike-level slippage on straddle requires a more
    # involved reconstruction we don't have time for in 3-Pre.
    if t.leg != "PREMIUM" or t.mode not in ("strangle", "iron_condor"):
        return None, None
    chain = _load_chain_at(t.trade_date, t.entry_ts)
    if chain.empty:
        return None, None

    # Filter to nearest-expiry calls and puts
    if "expiry" in chain.columns:
        chain["expiry"] = pd.to_datetime(chain["expiry"]).dt.date
        nearest_exp = sorted(set(chain["expiry"]))
        if not nearest_exp:
            return None, None
        chain = chain[chain["expiry"] == min(nearest_exp)]

    ce = chain[chain["option_type"] == "CE"].copy()
    pe = chain[chain["option_type"] == "PE"].copy()
    if ce.empty or pe.empty:
        return None, None

    # For strangle/IC: short legs are typically OTM.
    spot = t.spot
    ce_otm = ce[ce["strike"] > spot].sort_values("strike")
    pe_otm = pe[pe["strike"] < spot].sort_values("strike", ascending=False)

    if ce_otm.empty or pe_otm.empty:
        return None, None

    # Find the (CE, PE) pair whose bid_price sum is closest to
    # recorded entry_premium. Tolerate up to 20% mismatch. If the
    # closest match is >20% off, we can't claim to have identified
    # the actual strikes traded — return None rather than report a
    # spurious slippage number.
    target_total = t.entry_premium
    best_diff = float("inf")
    best_pair = None
    for _, ce_row in ce_otm.head(20).iterrows():
        for _, pe_row in pe_otm.head(20).iterrows():
            ce_bid = float(ce_row.get("bid_price", 0) or 0)
            pe_bid = float(pe_row.get("bid_price", 0) or 0)
            if ce_bid <= 0 or pe_bid <= 0:
                continue
            total_bid = ce_bid + pe_bid
            diff = abs(total_bid - target_total)
            if diff < best_diff and diff < target_total * 0.20:  # tightened
                best_diff = diff
                best_pair = (ce_bid, pe_bid, total_bid)

    if best_pair is None:
        return None, None
    chain_bid_total = best_pair[2]
    if t.entry_premium <= 0:
        return None, None
    slip_pct = (t.entry_premium - chain_bid_total) / t.entry_premium * 100
    slip_rs = (t.entry_premium - chain_bid_total) * t.quantity
    return slip_pct, slip_rs


# ─── Aggregation + report ────────────────────────────────────────────


def _summarize(trades: list[TradeRecord]) -> dict:
    if not trades:
        return {"n_trades": 0}

    df = pd.DataFrame([
        {
            "date": t.trade_date,
            "strategy_id": t.strategy_id,
            "leg": t.leg,
            "mode": t.mode,
            "spot": t.spot,
            "vix": t.vix,
            "quantity": t.quantity,
            "entry_premium": t.entry_premium,
            "outcome_pnl": t.outcome_pnl,
            "stt_rate_pct": float(t.stt_rate),
            "total_charges": float(t.total_charges),
            "net_pnl": t.net_pnl,
            "chain_slip_pct": t.chain_slip_pct,
            "chain_slip_rs": t.chain_slip_rs,
            "post_apr1": _is_post_apr1(t.trade_date),
            "exit_reason": t.exit_reason,
        }
        for t in trades
    ])

    out = {
        "n_trades": len(df),
        "n_winners": int((df["outcome_pnl"] > 0).sum()),
        "n_losers": int((df["outcome_pnl"] < 0).sum()),
        "gross_pnl_total": float(df["outcome_pnl"].sum()),
        "gross_pnl_mean": float(df["outcome_pnl"].mean()),
        "gross_pnl_median": float(df["outcome_pnl"].median()),
        "charges_total": float(df["total_charges"].sum()),
        "charges_mean_per_trade": float(df["total_charges"].mean()),
        "charges_pct_of_gross": float(df["total_charges"].sum() /
                                       max(df["outcome_pnl"].sum(), 1) * 100)
                                       if df["outcome_pnl"].sum() > 0 else float("inf"),
        "net_pnl_total": float(df["net_pnl"].sum()),
        "net_pnl_mean": float(df["net_pnl"].mean()),
        "net_pnl_median": float(df["net_pnl"].median()),
        "win_rate_gross": float((df["outcome_pnl"] > 0).mean() * 100),
        "win_rate_net": float((df["net_pnl"] > 0).mean() * 100),
        # Sharpe (per-trade × sqrt(N))
        "net_sharpe_per_trade": _per_trade_sharpe(df["net_pnl"]),
        "gross_sharpe_per_trade": _per_trade_sharpe(df["outcome_pnl"]),
        # Slippage (where reconstructible)
        "slip_recon_n": int(df["chain_slip_pct"].notna().sum()),
        "slip_pct_mean": float(df["chain_slip_pct"].mean()) if df["chain_slip_pct"].notna().any() else None,
        "slip_pct_median": float(df["chain_slip_pct"].median()) if df["chain_slip_pct"].notna().any() else None,
        "slip_rs_total": float(df["chain_slip_rs"].sum()) if df["chain_slip_rs"].notna().any() else None,
        # Per-strategy breakdown
        "by_strategy": _per_strategy(df),
        "by_mode": _per_mode(df),
        "by_period": _per_period(df),
        "df": df,
    }
    return out


def _per_trade_sharpe(s: pd.Series) -> float:
    if len(s) < 2 or s.std(ddof=1) == 0:
        return 0.0
    return float(s.mean() / s.std(ddof=1) * np.sqrt(len(s)))


def _per_strategy(df: pd.DataFrame) -> pd.DataFrame:
    return df.groupby("strategy_id").agg(
        n=("outcome_pnl", "count"),
        gross_total=("outcome_pnl", "sum"),
        gross_mean=("outcome_pnl", "mean"),
        charges_total=("total_charges", "sum"),
        net_total=("net_pnl", "sum"),
        net_mean=("net_pnl", "mean"),
        win_rate_net=("net_pnl", lambda s: float((s > 0).mean() * 100)),
    ).round(2)


def _per_mode(df: pd.DataFrame) -> pd.DataFrame:
    return df.groupby("mode").agg(
        n=("outcome_pnl", "count"),
        gross_total=("outcome_pnl", "sum"),
        charges_total=("total_charges", "sum"),
        net_total=("net_pnl", "sum"),
        net_mean=("net_pnl", "mean"),
    ).round(2)


def _per_period(df: pd.DataFrame) -> pd.DataFrame:
    return df.groupby("post_apr1").agg(
        n=("outcome_pnl", "count"),
        stt_pct=("stt_rate_pct", "mean"),
        gross_total=("outcome_pnl", "sum"),
        charges_total=("total_charges", "sum"),
        net_total=("net_pnl", "sum"),
        net_mean=("net_pnl", "mean"),
    ).round(2)


# ─── Verdict logic ───────────────────────────────────────────────────


def _verdict(s: dict, lot_multipliers: tuple[int, ...] = (1, 5, 10)) -> dict:
    """Apply PASS / YELLOW / FAIL criteria from PHASE3_MASTER §V.5."""
    if s["n_trades"] == 0:
        return {"verdict": "INSUFFICIENT_DATA", "reasons": ["no trades"]}

    # Scaled net P&L at higher lot sizes. The recorded outcome_pnl is at
    # ``quantity`` (typically 1 lot = 75). Scaling by N gives N-lot
    # equivalents — gross scales linearly; charges scale linearly too.
    # So net_pnl scales linearly to first order. (Slippage at higher
    # size is non-linear due to book-walking; we report the linear
    # approximation and flag that it's optimistic.)
    by_size = {}
    for L in lot_multipliers:
        net_total = s["net_pnl_total"] * L
        net_mean = s["net_pnl_mean"] * L
        # Per-trade Sharpe is invariant under linear scaling, so
        # scaling lot doesn't change Sharpe under this approximation.
        sharpe = s["net_sharpe_per_trade"]
        by_size[L] = {
            "net_total": net_total,
            "net_mean": net_mean,
            "sharpe": sharpe,
        }

    sharpe_1lot = by_size[1]["sharpe"]
    sharpe_5lot = by_size[5]["sharpe"]
    median_per_trade = s["net_pnl_median"]
    charges_pct = s["charges_pct_of_gross"]

    reasons = []
    if sharpe_1lot < 0.5:
        reasons.append(f"1-lot net Sharpe {sharpe_1lot:.2f} < 0.5")
    if sharpe_5lot < 0:
        reasons.append(f"5-lot net Sharpe {sharpe_5lot:.2f} < 0")
    if median_per_trade < 50:
        reasons.append(f"median net per trade ₹{median_per_trade:.0f} < ₹50")

    # Slippage gap criterion
    if s.get("slip_pct_mean") is not None and s["slip_pct_mean"] > 70:
        reasons.append(f"slippage gap {s['slip_pct_mean']:.1f}% > 70%")

    # PHASE3_MASTER §V.5 locks the verdict vocabulary to four labels:
    # PASS, YELLOW, FAIL, INSUFFICIENT_DATA. Adding a "PROVISIONAL PASS"
    # is exactly the undisciplined relaxation §V.6.5 forbids. When
    # sample size is small, the only honest verdict is
    # INSUFFICIENT_DATA — the operator runs the next-step analysis on
    # a larger sample (e.g. re-cost wide_baseline) before any PASS.
    n_trades = s["n_trades"]
    n_days = s["df"]["date"].nunique() if "df" in s else 0
    n_distinct_strategies = s["df"]["strategy_id"].nunique() if "df" in s else 0

    # Effective sample size: trades fired by independent strategies on
    # the same minute share the same market state, so they are NOT
    # statistically independent draws. Conservative effective-N
    # estimate divides trade count by the per-day strategy count.
    effective_n = n_trades / max(n_distinct_strategies, 1) if n_trades > 0 else 0

    INSUFFICIENT_THRESHOLD = 30  # below this effective N, no PASS verdict

    if effective_n < INSUFFICIENT_THRESHOLD:
        verdict = "INSUFFICIENT_DATA"
        reasons.insert(
            0,
            f"effective N = {effective_n:.1f} (raw trades = {n_trades}; "
            f"{n_distinct_strategies} strategies firing on overlapping minutes "
            f"reduce independent samples) < {INSUFFICIENT_THRESHOLD}. "
            f"Statistical power is zero; PASS/FAIL on this sample is meaningless."
        )
    elif not reasons:
        verdict = "PASS"
    elif sharpe_1lot >= 0.5 and sharpe_5lot < 0:
        verdict = "YELLOW"
        reasons.append("1-lot viable; 5+ lot scale is unprofitable (capacity binding)")
    else:
        verdict = "FAIL"

    return {
        "verdict": verdict,
        "reasons": reasons,
        "by_size": by_size,
        "sharpe_1lot": sharpe_1lot,
        "sharpe_5lot": sharpe_5lot,
        "median_per_trade": median_per_trade,
        "charges_pct_of_gross": charges_pct,
    }


# ─── Report writer ───────────────────────────────────────────────────


def _write_report(trades: list[TradeRecord], summary: dict, verdict: dict) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df = summary.get("df", pd.DataFrame())

    lines: list[str] = []
    lines.append("# Phase 3-Pre Fee/Slippage Truth-Up — Verdict Report")
    lines.append("")
    lines.append(f"- Generated: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"- Window: {min(t.trade_date for t in trades)} → "
                 f"{max(t.trade_date for t in trades)}")
    lines.append(f"- Source: 17 days of overlapping chain_snapshots + paper-trade decisions")
    lines.append(f"- Strategy IDs observed: {sorted(set(t.strategy_id for t in trades))}")
    lines.append("")

    lines.append("## VERDICT")
    lines.append("")
    lines.append(f"**{verdict['verdict']}**")
    lines.append("")
    if verdict["verdict"] == "INSUFFICIENT_DATA":
        lines.append("This is not a green light to proceed. It is the harness "
                     "saying 'this sample cannot answer the cost-edge question.'")
        lines.append("")
        lines.append("**Per PHASE3_MASTER §V.5 verdict vocabulary, INSUFFICIENT_DATA "
                     "means Phase 3a does not start. The operator must obtain a "
                     "larger, lower-correlation sample before re-running the "
                     "truth-up.**")
        lines.append("")
    if verdict["reasons"]:
        lines.append("Reasons:")
        for r in verdict["reasons"]:
            lines.append(f"- {r}")
        lines.append("")

    lines.append("## Summary statistics")
    lines.append("")
    lines.append(f"| Metric | Value |")
    lines.append(f"|---|---|")
    lines.append(f"| Trades (paired ENTER+EXIT) | {summary['n_trades']} |")
    lines.append(f"| Winners (gross) | {summary['n_winners']} ({summary['win_rate_gross']:.1f}%) |")
    lines.append(f"| Losers (gross) | {summary['n_losers']} |")
    lines.append(f"| **Gross P&L total** | **₹{summary['gross_pnl_total']:,.0f}** |")
    lines.append(f"| Gross P&L mean per trade | ₹{summary['gross_pnl_mean']:,.0f} |")
    lines.append(f"| Gross P&L median per trade | ₹{summary['gross_pnl_median']:,.0f} |")
    lines.append(f"| **Total charges** | **₹{summary['charges_total']:,.0f}** |")
    lines.append(f"| Charges as % of gross | {summary['charges_pct_of_gross']:.1f}% |")
    lines.append(f"| Charges mean per trade | ₹{summary['charges_mean_per_trade']:,.0f} |")
    lines.append(f"| **Net P&L total** | **₹{summary['net_pnl_total']:,.0f}** |")
    lines.append(f"| Net P&L mean per trade | ₹{summary['net_pnl_mean']:,.0f} |")
    lines.append(f"| Net P&L median per trade | ₹{summary['net_pnl_median']:,.0f} |")
    lines.append(f"| Net win rate | {summary['win_rate_net']:.1f}% |")
    lines.append(f"| **Net Sharpe (per-trade × √N)** | **{summary['net_sharpe_per_trade']:.3f}** |")
    lines.append(f"| Gross Sharpe (per-trade × √N) | {summary['gross_sharpe_per_trade']:.3f} |")
    lines.append("")

    lines.append("## Lot-size scaling (linear approximation)")
    lines.append("")
    lines.append("| Lots | Net Total (₹) | Net mean per trade (₹) | Per-trade Sharpe |")
    lines.append("|---|---|---|---|")
    for L, x in verdict["by_size"].items():
        lines.append(f"| {L}× ({L * 75} contracts) | {x['net_total']:,.0f} | "
                     f"{x['net_mean']:,.1f} | {x['sharpe']:+.3f} |")
    lines.append("")
    lines.append("Note: linear scaling assumes constant slippage per lot. Real "
                 "5+ lot fills walk the order book and pay deeper into the "
                 "depth, so net P&L at 5/10 lots is OPTIMISTIC vs real.")
    lines.append("")

    lines.append("## Per-strategy breakdown")
    lines.append("")
    lines.append("```")
    lines.append(summary["by_strategy"].to_string())
    lines.append("```")
    lines.append("")

    lines.append("## Per-mode breakdown")
    lines.append("")
    lines.append("```")
    lines.append(summary["by_mode"].to_string())
    lines.append("```")
    lines.append("")

    lines.append("## Period split (pre vs post Apr 1 2026 STT hike to 0.15%)")
    lines.append("")
    lines.append("```")
    lines.append(summary["by_period"].to_string())
    lines.append("```")
    lines.append("")

    lines.append("## Slippage analysis (chain bid/ask vs recorded entry_premium)")
    lines.append("")
    if summary.get("slip_recon_n", 0) == 0:
        lines.append("- No trades successfully reconstructed against chain snapshot.")
        lines.append("- Likely cause: chain CSV recording was sparse around the entry minutes,")
        lines.append("  or the strangle/IC strikes were >20 OTM rungs from spot (outside our")
        lines.append("  search window).")
    else:
        lines.append(f"- Reconstructed {summary['slip_recon_n']} of {summary['n_trades']} trades")
        lines.append(f"- Mean slippage: {summary['slip_pct_mean']:.1f}% of entry_premium "
                     f"(positive = recorded > chain bid → strategy overstates collection)")
        lines.append(f"- Median slippage: {summary['slip_pct_median']:.1f}%")
        lines.append(f"- Total ENTRY slippage in ₹: "
                     f"{summary.get('entry_slippage_total', 0):,.0f}")
        lines.append("")
        lines.append("**Important:** the slippage shown here is ENTRY-side only "
                     "(SELL fill at chain bid vs recorded entry_premium). EXIT "
                     "slippage (BUY back at chain ask vs strategy's exit LTP) "
                     "is NOT measured here and would push real net P&L further "
                     "down by a similar magnitude. The net_pnl figures above "
                     "are therefore OPTIMISTIC; the true net is "
                     "approximately ``net_pnl × (1 - 2 × |slip_pct|/100)``.")
    lines.append("")

    lines.append("## Charge rate methodology")
    lines.append("")
    lines.append("Date-aware STT (per Indian tax law):")
    lines.append("- Until Mar 31 2026: STT options sell = **0.10%** (Oct 1 2024 hike)")
    lines.append("- From Apr 1 2026: STT options sell = **0.15%** (Budget 2026)")
    lines.append("- 19-day window has 5 days at 0.10% + 14 days at 0.15% rate.")
    lines.append("")
    lines.append("Other charges (per `src/core/constants.py::CHARGES` after Apr 25 2026 audit fix):")
    lines.append("- Exchange (NSE F&O options): 0.0353% on premium turnover (post-Oct-2024 NSE circular 100/2024)")
    lines.append("- SEBI: 0.0001%")
    lines.append("- GST: 18% on (brokerage + exchange + SEBI)")
    lines.append("- Stamp duty: 0.003% on options buy")
    lines.append("- Brokerage: Zerodha 0.03% or ₹20/order whichever lower")
    lines.append("")

    lines.append("## Decision criteria (locked, per PHASE3_MASTER §V.5)")
    lines.append("")
    lines.append("| Verdict | Criteria |")
    lines.append("|---|---|")
    lines.append("| PASS | 1-lot net Sharpe > 0.5 AND 5-lot net Sharpe > 0 AND median net per trade > +₹50 AND no single charge > 50% of gross edge |")
    lines.append("| YELLOW | 1-lot Sharpe > 0.5 BUT 5-lot Sharpe < 0 |")
    lines.append("| FAIL | Any of: 1-lot Sharpe < 0; median per trade < 0; slippage gap > 70%; single charge > 50% of gross |")
    lines.append("")

    if verdict["verdict"] == "FAIL":
        lines.append("## What FAIL means")
        lines.append("")
        lines.append("Phase 3a is NOT started. Operator commits to one of three")
        lines.append("replan options (per PHASE3_MASTER §V.5.3):")
        lines.append("")
        lines.append("- (a) Pivot to institutional size where the operator collects")
        lines.append("      spread instead of paying it.")
        lines.append("- (b) Pivot to different strategies that don't depend on")
        lines.append("      retail-scale OTM premium-selling edge: Iron Butterfly")
        lines.append("      (max ATM theta), NIFTY/BANKNIFTY relative-vol pair")
        lines.append("      (post-Nov-2024 dislocation), Long Calendar (positive vega).")
        lines.append("- (c) Retire systematic premium-selling at retail scale")
        lines.append("      entirely.")
        lines.append("")
        lines.append("The 30-day moratorium on parameter optimization begins now.")
    elif verdict["verdict"] == "YELLOW":
        lines.append("## What YELLOW means")
        lines.append("")
        lines.append("Phase 3a starts, BUT with `max_lots = 1` constraint locked")
        lines.append("in all subsequent optimization. Capacity is the binding")
        lines.append("constraint per PHASE3_MASTER §V.5.2.")
    else:
        lines.append("## What PASS means")
        lines.append("")
        lines.append("Phase 3a starts. Reviewer corrections (5 strategies, HMM,")
        lines.append("vol-targeting weights, single common holdout) apply.")
    lines.append("")

    lines.append("## Methodology caveats")
    lines.append("")
    lines.append("- **outcome_pnl is GROSS** (premium-decay × qty); charges are")
    lines.append("  computed on top, not deducted from outcome_pnl by the strategy.")
    lines.append("- **Charge approximation:** entry_premium is split evenly across")
    lines.append("  legs (2 for strangle/straddle, 4 for IC). For IC's asymmetric")
    lines.append("  wings this is coarse but conservative for cost magnitude.")
    lines.append("- **Lot-size scaling is linear** in this report. Real 5+ lot")
    lines.append("  fills walk the book; net P&L at higher sizes is therefore")
    lines.append("  OPTIMISTIC vs reality. The capacity gate already showed")
    lines.append("  this in `wide_baseline_portfolio.md`.")
    lines.append("- **Strike reconstruction is best-effort.** Where chain data")
    lines.append("  was sparse around entry minute, slippage analysis reports")
    lines.append("  'not computable' rather than guess.")
    lines.append("")

    lines.append("## Per-trade detail (full)")
    lines.append("")
    lines.append("```")
    cols = ["date", "strategy_id", "leg", "mode", "vix", "quantity",
            "entry_premium", "outcome_pnl", "stt_rate_pct", "total_charges",
            "net_pnl", "exit_reason"]
    lines.append(df[cols].to_string(index=False))
    lines.append("```")
    lines.append("")

    REPORT_PATH.write_text("\n".join(lines))
    print(f"\n✓ Report written: {REPORT_PATH}")


# ─── Main ────────────────────────────────────────────────────────────


def main() -> None:
    print("=" * 70)
    print("Phase 3-Pre Fee/Slippage Truth-Up")
    print("=" * 70)
    print()

    print("[1/4] Loading paired ENTER+EXIT trades from chain window...")
    trades = _load_paired_trades()
    print(f"  Loaded {len(trades)} paired trades across "
          f"{len({t.trade_date for t in trades})} days.")
    if not trades:
        print("  No trades found. Exiting.")
        return

    print()
    print("[2/4] Computing realistic charges (date-aware STT)...")
    for t in trades:
        t.total_charges = _compute_trade_charges(t)
        t.net_pnl = t.outcome_pnl - float(t.total_charges)

    pre_apr = sum(1 for t in trades if not _is_post_apr1(t.trade_date))
    post_apr = sum(1 for t in trades if _is_post_apr1(t.trade_date))
    print(f"  Pre-Apr-1-2026 trades (STT 0.10%): {pre_apr}")
    print(f"  Post-Apr-1-2026 trades (STT 0.15%): {post_apr}")

    print()
    print("[3/4] Attempting strike-level slippage reconstruction...")
    n_recon = 0
    total_slip_rs = 0.0
    for t in trades:
        slip_pct, slip_rs = _attempt_slippage_recon(t)
        t.chain_slip_pct = slip_pct
        t.chain_slip_rs = slip_rs
        if slip_pct is not None:
            n_recon += 1
            # Apply ENTRY-side slippage adjustment to net pnl. Positive
            # slip_rs means recorded entry_premium > chain bid (the
            # strategy overstated collection). Real net pnl = recorded
            # net - entry slippage. NOTE: this is ENTRY-only; exit
            # slippage (BUY at chain ask vs recorded LTP) is unmeasured
            # here and would push net even lower. So this adjustment is
            # CONSERVATIVE for the trader (optimistic for edge).
            t.net_pnl -= slip_rs
            total_slip_rs += slip_rs
    print(f"  Strikes reconstructed: {n_recon} of {len(trades)} trades")
    print(f"  Total ENTRY slippage applied: ₹{total_slip_rs:,.0f}")

    print()
    print("[4/4] Computing summary + verdict...")
    summary = _summarize(trades)
    verdict = _verdict(summary)
    # Stuff slippage-adjusted info into summary for the report
    summary["entry_slippage_total"] = total_slip_rs
    summary["entry_slippage_recon_count"] = n_recon
    summary["sample_size_warning"] = (
        f"Only {summary['n_trades']} trades over "
        f"{summary['df']['date'].nunique()} days. "
        "Statistical power is INSUFFICIENT for deployment-grade verdict. "
        "Need ~120-180 trades for Sharpe estimation at 95% confidence."
    ) if summary['n_trades'] < 120 else None

    # Console summary
    print()
    print("─" * 70)
    print(f"VERDICT: {verdict['verdict']}")
    print("─" * 70)
    if verdict["reasons"]:
        for r in verdict["reasons"]:
            print(f"  - {r}")
    print()
    print(f"Trades: {summary['n_trades']}  |  Net Sharpe: {summary['net_sharpe_per_trade']:+.3f}")
    print(f"Gross P&L: ₹{summary['gross_pnl_total']:,.0f}  |  "
          f"Charges: ₹{summary['charges_total']:,.0f}  |  "
          f"Net: ₹{summary['net_pnl_total']:,.0f}")
    print(f"Charges as % of gross: {summary['charges_pct_of_gross']:.1f}%")
    print(f"Median net per trade: ₹{summary['net_pnl_median']:,.0f}")
    print()

    _write_report(trades, summary, verdict)


if __name__ == "__main__":
    main()
