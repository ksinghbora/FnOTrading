"""Regime-stratified metric reporter for backtest validation.

Slices a decisions frame by market regime (VIX level, expiry proximity,
event day, trend vs range) and reports daily-P&L Sharpe, win rate, and
max drawdown per bucket. The acceptance gate is Sharpe >= -0.5 whenever
a bucket carries >20 trades — a bucket that loses decisively is a veto
even if the aggregate backtest prints Sharpe > 0.

A single trade can belong to multiple buckets simultaneously (high VIX
AND expiry week AND trending). We compute stats per bucket independently;
overlap is expected and reported.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

REGIMES = (
    "high_vix",
    "mid_vix",
    "low_vix",
    "expiry_week",
    "event_day",
    "trending",
    "range_bound",
)


@dataclass
class RegimeStats:
    """Per-regime backtest scorecard.

    ``passed`` is False iff Sharpe < -0.5 AND num_trades > 20. That is,
    small samples get the benefit of the doubt (too few trades to reject),
    and modest losses don't trigger — only a decisive losing bucket.
    """

    regime: str
    num_trades: int
    total_pnl: float
    sharpe: float  # annualized from daily P&L
    win_rate: float
    max_dd: float
    passed: bool


def load_event_dates(
    event_days_csv: Path = Path("data/event_days.csv"),
) -> dict[date, str]:
    """Parse the event calendar. Returns {date: event_type}.

    Only rows tagged HARD_BLOCK or SOFT_CAUTION are returned — "#"-prefixed
    comment lines and section separators are skipped. Both severities count
    as event days for regime stratification (the caller can re-filter on
    event_type if a finer split is needed).
    """
    event_dates: dict[date, str] = {}
    if not event_days_csv.exists():
        return event_dates

    with event_days_csv.open("r") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            date_str, event_type, severity = parts[0], parts[1], parts[2]
            if severity not in ("HARD_BLOCK", "SOFT_CAUTION"):
                # Header row ("date,event_type,severity") also lands here — skip.
                continue
            try:
                d = datetime.fromisoformat(date_str).date()
            except ValueError:
                continue
            event_dates[d] = event_type
    return event_dates


def _coerce_date(value: object) -> date | None:
    """Coerce a date/timestamp/str value into a ``date`` instance."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime().date()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            try:
                return pd.to_datetime(value).date()
            except (ValueError, TypeError):
                return None
    return None


def _ensure_date_column(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with a ``date`` column coerced to ``datetime.date``.

    Accepts:
      - pre-existing ``date`` column (date/datetime/string)
      - ``timestamp`` column (ISO string or pd.Timestamp) — derive date.
    """
    out = df.copy()
    if "date" in out.columns:
        out["date"] = out["date"].map(_coerce_date)
        return out
    if "timestamp" in out.columns:
        out["date"] = pd.to_datetime(out["timestamp"], errors="coerce").dt.date
        return out
    # No temporal column at all — synthesise a NaT column so downstream
    # Sharpe computation yields 0 cleanly instead of raising.
    out["date"] = None
    return out


def bucket_row(row: pd.Series, event_dates: dict[date, str]) -> list[str]:
    """Classify a single row into zero or more regime buckets.

    A row can map to several labels (e.g. ``high_vix`` AND ``expiry_week``
    AND ``trending``). Missing inputs are tolerated — the corresponding
    label is simply skipped.
    """
    labels: list[str] = []

    vix = row.get("vix")
    if vix is not None and not (isinstance(vix, float) and math.isnan(vix)):
        v = float(vix)
        if v > 15:
            labels.append("high_vix")
        elif v >= 13:
            labels.append("mid_vix")
        else:
            labels.append("low_vix")

    is_expiry_raw = row.get("is_expiry")
    is_expiry = bool(is_expiry_raw) if is_expiry_raw is not None else False
    dte = row.get("dte")
    dte_val: float | None = None
    if dte is not None and not (isinstance(dte, float) and math.isnan(dte)):
        dte_val = float(dte)
    if is_expiry or (dte_val is not None and dte_val <= 2):
        labels.append("expiry_week")

    row_date = _coerce_date(row.get("date"))
    if row_date is not None and row_date in event_dates:
        labels.append("event_day")

    move = row.get("move_from_open_pct")
    if move is not None and not (isinstance(move, float) and math.isnan(move)):
        m = abs(float(move))
        if m > 1.0:
            labels.append("trending")
        if m <= 0.5:
            labels.append("range_bound")

    return labels


def _stats_for_bucket(sub: pd.DataFrame, regime: str) -> RegimeStats:
    """Compute the scorecard for a subset (rows already filtered to bucket)."""
    if sub.empty:
        return RegimeStats(
            regime=regime,
            num_trades=0,
            total_pnl=0.0,
            sharpe=0.0,
            win_rate=0.0,
            max_dd=0.0,
            passed=True,
        )

    pnl = pd.to_numeric(sub.get("outcome_pnl"), errors="coerce").fillna(0.0)
    num_trades = int((pnl != 0).sum())
    total_pnl = float(pnl.sum())

    wins = int((pnl > 0).sum())
    losses = int((pnl < 0).sum())
    decisive = wins + losses
    win_rate = (wins / decisive * 100.0) if decisive > 0 else 0.0

    # Daily P&L series — chronological. Null dates bucket into one group,
    # which is harmless for Sharpe but would distort draw-down; drop them
    # explicitly so draw-down only runs over known dates.
    sub_dated = sub.assign(
        outcome_pnl=pnl.values,
        date=sub["date"],
    )
    sub_dated = sub_dated[sub_dated["date"].notna()]
    daily = (
        sub_dated.groupby("date", sort=True)["outcome_pnl"].sum()
        if not sub_dated.empty
        else pd.Series(dtype=float)
    )

    if len(daily) < 2:
        sharpe = 0.0
    else:
        std = float(daily.std(ddof=1))
        if std == 0.0 or math.isnan(std):
            sharpe = 0.0
        else:
            sharpe = float(daily.mean() / std * math.sqrt(252))

    if daily.empty:
        max_dd = 0.0
    else:
        cum = daily.cumsum().to_numpy()
        peak = np.maximum.accumulate(cum)
        dd = cum - peak
        max_dd = float(dd.min())

    passed = not (sharpe < -0.5 and num_trades > 20)

    return RegimeStats(
        regime=regime,
        num_trades=num_trades,
        total_pnl=round(total_pnl, 2),
        sharpe=round(sharpe, 4),
        win_rate=round(win_rate, 2),
        max_dd=round(max_dd, 2),
        passed=passed,
    )


def stratify(
    decisions: pd.DataFrame,
    event_dates: dict[date, str] | None = None,
) -> dict[str, RegimeStats]:
    """Slice ``decisions`` by regime and return a stat card per bucket.

    Trades are bucketed by their **entry-time** features. When a frame
    carries ENTER/EXIT decision rows (real decisions CSV), we pair them so
    each trade contributes exactly once with ENTRY-row vix/move/dte and
    EXIT-row outcome_pnl. When the frame is a synthetic test fixture with
    only outcome_pnl rows (no ``decision`` column), we fall through to the
    legacy per-row labelling — same row holds features and pnl.

    Why entry-time matters: the harness exists to flag regimes where a
    runtime gate could intervene. EXIT-time labelling conflates "trade
    lost in regime X" with "regime X label appeared because the trade
    lost" (e.g., a +0.7% intraday move grows to +1.2% by exit, flipping
    the trade from ``range_bound`` at entry to ``trending`` at exit).
    Apr 25 2026 verified the bug end-to-end: harness reported high_vix
    Sharpe -2.32, entry-time view +3.64; harness reported trending
    n=1017, entry-time n=4. Gating at entry can never fire on labels
    that only crystallise at exit.
    """
    if event_dates is None:
        event_dates = load_event_dates()

    if decisions.empty:
        return {r: _stats_for_bucket(decisions, r) for r in REGIMES}

    df = _ensure_date_column(decisions)

    # Pair ENTER+EXIT rows when the schema indicates a real decisions log.
    # ``decision`` values from BaseStrategy._record_decision are 'ENTER'
    # and 'EXIT' (case-insensitive guard). The pairing key is
    # (strategy_id, leg, cumcount per decision) — the same defensive zip
    # used by scripts/diagnose_trend_leg.py that has been audited against
    # the real CSVs. If pairing fails (no ENTER rows, missing columns)
    # we fall back to the legacy single-row mode.
    if "decision" in df.columns:
        df = _pair_enter_exit(df)

    # Per-row bucket labels (list column). Keep in memory — the typical
    # decisions frame is <1M rows so iterrows-free apply is fine.
    labels = df.apply(lambda row: bucket_row(row, event_dates), axis=1)

    out: dict[str, RegimeStats] = {}
    for regime in REGIMES:
        mask = labels.apply(lambda ls, r=regime: r in ls)
        sub = df[mask]
        out[regime] = _stats_for_bucket(sub, regime)
    return out


def _pair_enter_exit(df: pd.DataFrame) -> pd.DataFrame:
    """Pair ENTER and EXIT rows; return one row per trade with entry-time
    features + exit outcome_pnl. Falls through unchanged if pairing isn't
    possible (e.g., no ENTER rows, or missing pairing key columns).
    """
    decision = df["decision"].astype(str).str.upper()
    enter_mask = decision == "ENTER"
    exit_mask = decision == "EXIT"
    if not enter_mask.any() or not exit_mask.any():
        return df

    required = {"strategy_id", "leg"}
    if not required.issubset(df.columns):
        return df

    work = df.copy()
    # Stable per-row index within (strategy_id, leg, decision) groups so
    # the i-th ENTER pairs with the i-th EXIT. CSV write order = trade
    # order in the harness, so cumcount is the natural pairing key.
    work["_pair_idx"] = work.groupby(
        ["strategy_id", "leg", decision], sort=False
    ).cumcount()

    enters = work[enter_mask].copy()
    exits_pnl = (
        work[exit_mask][["strategy_id", "leg", "_pair_idx", "outcome_pnl"]]
        .rename(columns={"outcome_pnl": "_exit_pnl"})
    )
    merged = enters.merge(
        exits_pnl,
        on=["strategy_id", "leg", "_pair_idx"],
        how="inner",
    )
    if merged.empty:
        # Pairing produced no hits — defensively return the original frame
        # so the caller still sees something instead of an empty stats card.
        return df

    merged["outcome_pnl"] = merged["_exit_pnl"]
    merged = merged.drop(columns=["_pair_idx", "_exit_pnl"])
    return merged
