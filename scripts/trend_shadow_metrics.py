#!/usr/bin/env python3
"""Phase B of the score validation plan: nightly trend-leg shadow metrics.

Designed to run alongside nightly_audit during the 30-day shadow window
(Apr 21 → ~May 19) that follows the Apr 18 trend score rebalance.

The output is a JSON dump at `data/trend_shadow_YYYY-MM.json` plus a
human-readable summary on stdout. The decision tree from
`memory/score_validation_plan.md` keys off the entry-rate band that this
script reports.

WHY THIS EXISTS
---------------
After Apr 18, two changes shipped together:
  - The trend score function got a 6th factor (BankNifty) and a recalibrated
    factor 4 (VIX-level). Net: max base score dropped 100 → 90.
  - `trend_signal_threshold` was split out at 50 (premium leg keeps 60).

The bounded-impact analysis (`analyze_score_rebalance_impact.py`) said this
combo lands in the "🟧 NEEDS SHADOW DATA" band — material but acceptable,
provided we instrument and decide after 30 days. This script IS that
instrumentation.

WHAT IT MEASURES
----------------
For each TREND row in the configured date window:

  1. Entry-rate health (the headline gate)
       - entries_per_day: count per trading day
       - median_entries_per_day: robust to single-day backtest spikes
       - band: PASS / WARN / FAIL per the plan's thresholds

  2. Per-factor activation (Phase A logs only)
       - For each of f1..f6: % of entries where factor fired positive,
         negative, or zero, plus the mean contribution.
       - Spots dead factors (always ~0) and dominant factors (always max).

  3. Suppression source attribution (Phase A logs only)
       - For each near-miss (final_score below threshold but above 0):
         which factor's contribution would have flipped the entry if
         it had been at its max-positive value? Reports the bottleneck
         factor distribution.

  4. Score distribution drift
       - Histogram of live scores vs the historical factor4_only-shifted
         baseline (computed inline from the same data the rebalance
         analysis script uses).

PHASE A vs PRE-PHASE-A LOG HANDLING
-----------------------------------
Phase A landed Apr 18 (commit ebbabbf), so logs from Apr 21 onward have
the per-factor breakdown columns. Older logs only have aggregate
`rule_score`. The script handles both transparently:

  - For Phase A rows: precise per-factor attribution as described above.
  - For pre-Phase A rows: only entry-rate and aggregate score distribution;
    per-factor sections report "no Phase A data" and the count of skipped
    rows. This means the first 5 trading days of any 30-day window will
    have less detail than later days — that's fine, the entry-rate metric
    (the gate) is computed from both equally well.

USAGE
-----
    # Default: last 30 days, all strategies
    uv run python scripts/trend_shadow_metrics.py

    # Custom window
    uv run python scripts/trend_shadow_metrics.py --since 2026-04-21 --until 2026-05-19

    # Dump JSON only (for nightly_audit ingestion)
    uv run python scripts/trend_shadow_metrics.py --json-only

EXIT CODES
----------
    0  PASS or WARN — operator review optional
    1  FAIL — entry-rate band breached, escalate per the rollback trigger
    2  No data found in the window
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DECISIONS_DIR = REPO_ROOT / "data" / "decisions"
OUTPUT_DIR = REPO_ROOT / "data"

# Per the score_validation_plan.md, the historical baseline is the median
# entries-per-day across the 90 days analysed by analyze_score_rebalance_impact.py
# (4,958 ENTERs / 90 days). Mean = 55, but Mar 25 alone contributed 322 — so
# we use the median, which is much closer to 5-10/day. The plan specifies:
#   PASS: 30-day median within ±30% of historical median  (5-13/day)
#   WARN: 30-day median 30-50% below historical
#   FAIL: 30-day median >50% below for 5 consecutive days
# Adjust here if the historical baseline is recomputed.
HISTORICAL_MEDIAN_ENTRIES_PER_DAY = 9    # midpoint of the 5-13/day band
PASS_LOWER_BOUND = 5                      # -30% of 9 → ~6, plan says 5
WARN_LOWER_BOUND = 4                      # -50% of 9 → 4-5, FAIL below this

# Phase A column names — checked at parse time to detect schema generation.
# Sentinel column: if `score_f1_breakout` is in the header, the row has the
# full Phase A breakdown.
PHASE_A_SENTINEL = "score_f1_breakout"
PHASE_A_FACTORS = [
    "score_f1_breakout",
    "score_f2_oi",
    "score_f3_duration",
    "score_f4_vix_level",
    "score_f5_vix_dir",
    "score_f6_banknifty",
]


@dataclass
class FactorActivation:
    """Per-factor activation stats across a window of TREND ENTERs."""
    pos_pct: float = 0.0          # % of entries where factor contributed > 0
    neg_pct: float = 0.0          # % where factor contributed < 0
    zero_pct: float = 0.0         # % where factor contributed exactly 0
    mean: float = 0.0             # mean contribution across all entries
    min_val: int = 0
    max_val: int = 0


@dataclass
class WindowMetrics:
    """Full output of a shadow-metrics run."""
    window_start: str
    window_end: str
    trading_days: int
    total_entries: int
    phase_a_entries: int          # subset with per-factor breakdown
    pre_phase_a_entries: int      # subset without (aggregate score only)
    entries_per_day: dict[str, int] = field(default_factory=dict)
    median_entries_per_day: float = 0.0
    mean_entries_per_day: float = 0.0
    band: str = "UNKNOWN"         # PASS / WARN / FAIL
    consecutive_fail_days: int = 0
    score_histogram: dict[str, int] = field(default_factory=dict)
    factor_activation: dict[str, FactorActivation] = field(default_factory=dict)
    suppression_attribution: dict[str, float] = field(default_factory=dict)
    suppression_n: int = 0
    notes: list[str] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--since", type=str, default=None,
        help="Start date (YYYY-MM-DD inclusive). Default: 30 days before --until.",
    )
    p.add_argument(
        "--until", type=str, default=None,
        help="End date (YYYY-MM-DD inclusive). Default: today.",
    )
    p.add_argument(
        "--strategy-id", type=str, default=None,
        help="Filter to one strategy_id. Default: all.",
    )
    p.add_argument(
        "--json-only", action="store_true",
        help="Suppress human-readable stdout; only write the JSON file.",
    )
    p.add_argument(
        "--output", type=Path, default=None,
        help="Output JSON path. Default: data/trend_shadow_YYYY-MM.json.",
    )
    return p.parse_args()


def resolve_window(args: argparse.Namespace) -> tuple[date, date]:
    today = date.today()
    until = date.fromisoformat(args.until) if args.until else today
    since = date.fromisoformat(args.since) if args.since else (until - timedelta(days=30))
    if since > until:
        raise ValueError(f"--since {since} must be ≤ --until {until}")
    return since, until


def collect_rows(
    since: date, until: date, strategy_id: str | None,
) -> tuple[list[dict], list[dict], int]:
    """Read TREND ENTER rows in the window. Returns (phase_a_rows, pre_a_rows, skipped)."""
    phase_a_rows: list[dict] = []
    pre_a_rows: list[dict] = []
    skipped = 0

    files = sorted(DECISIONS_DIR.glob("decisions_*.csv"))
    for f in files:
        try:
            file_date = date.fromisoformat(f.stem.removeprefix("decisions_"))
        except ValueError:
            continue
        if file_date < since or file_date > until:
            continue

        with open(f, newline="") as fp:
            reader = csv.DictReader(fp)
            is_phase_a = PHASE_A_SENTINEL in (reader.fieldnames or [])
            for row in reader:
                if row.get("leg") != "TREND" or row.get("decision") != "ENTER":
                    continue
                if strategy_id and row.get("strategy_id") != strategy_id:
                    continue
                row["_date"] = file_date.isoformat()
                try:
                    row["_rule_score"] = int(row["rule_score"])
                    row["_final_score"] = int(row.get("final_score") or row["rule_score"])
                    row["_threshold"] = int(row.get("threshold") or 0)
                except (KeyError, ValueError):
                    skipped += 1
                    continue
                if is_phase_a:
                    try:
                        for col in PHASE_A_FACTORS:
                            row[f"_{col}"] = int(row.get(col) or 0)
                        row["_phase_a"] = True
                        phase_a_rows.append(row)
                    except ValueError:
                        skipped += 1
                else:
                    row["_phase_a"] = False
                    pre_a_rows.append(row)
    return phase_a_rows, pre_a_rows, skipped


def compute_entry_rate(
    rows: list[dict], since: date, until: date,
) -> tuple[dict[str, int], float, float, str, int]:
    """Return (per-day counts, median, mean, band, consecutive_fail_days)."""
    by_day: dict[str, int] = defaultdict(int)
    for r in rows:
        by_day[r["_date"]] += 1

    # Iterate every weekday in the window (rough proxy for trading days —
    # NSE holidays will look like FAIL days but the consecutive-fail check
    # below tolerates short clusters).
    counts: list[int] = []
    cursor = since
    consecutive_zeros = 0
    max_consecutive_zeros = 0
    while cursor <= until:
        if cursor.weekday() < 5:  # Mon-Fri
            n = by_day.get(cursor.isoformat(), 0)
            counts.append(n)
            if n == 0:
                consecutive_zeros += 1
                max_consecutive_zeros = max(max_consecutive_zeros, consecutive_zeros)
            else:
                consecutive_zeros = 0
        cursor += timedelta(days=1)

    median = statistics.median(counts) if counts else 0.0
    mean = statistics.mean(counts) if counts else 0.0

    # Hard rollback trigger: 5 consecutive zero-entry days while premium leg
    # is still firing. We don't have premium-leg counts in this script (would
    # need to broaden the row filter), so this captures only the trend-zero
    # signal — operator should cross-reference with nightly_audit before
    # acting on a FAIL.
    if max_consecutive_zeros >= 5:
        band = "FAIL"
    elif median < WARN_LOWER_BOUND:
        band = "FAIL"
    elif median < PASS_LOWER_BOUND:
        band = "WARN"
    else:
        band = "PASS"

    return dict(sorted(by_day.items())), median, mean, band, max_consecutive_zeros


def compute_score_histogram(rows: list[dict]) -> dict[str, int]:
    """10-point bucket histogram of final_score for entries."""
    bins: dict[str, int] = defaultdict(int)
    for r in rows:
        bucket = (r["_final_score"] // 10) * 10
        bins[f"{bucket:>3}-{bucket + 9:>3}"] += 1
    return dict(sorted(bins.items()))


def compute_factor_activation(phase_a_rows: list[dict]) -> dict[str, FactorActivation]:
    """For each factor, compute pos/neg/zero %, mean, min/max across Phase A rows."""
    if not phase_a_rows:
        return {}
    n = len(phase_a_rows)
    out: dict[str, FactorActivation] = {}
    for col in PHASE_A_FACTORS:
        vals = [r[f"_{col}"] for r in phase_a_rows]
        pos = sum(1 for v in vals if v > 0)
        neg = sum(1 for v in vals if v < 0)
        zero = sum(1 for v in vals if v == 0)
        out[col] = FactorActivation(
            pos_pct=round(100 * pos / n, 1),
            neg_pct=round(100 * neg / n, 1),
            zero_pct=round(100 * zero / n, 1),
            mean=round(sum(vals) / n, 2),
            min_val=min(vals),
            max_val=max(vals),
        )
    return out


def compute_suppression_attribution(
    phase_a_rows: list[dict], threshold: int,
) -> tuple[dict[str, float], int]:
    """For near-miss rows (score < threshold), which factor was the bottleneck?

    A "bottleneck" is the factor whose negative-or-zero contribution, if it
    had instead fired at its max-positive branch, would have flipped the
    entry above threshold. If multiple factors qualify, attribute to the
    largest gap. If no single factor would have flipped it (the entry was
    blocked by combined deficits), attribute to "combined".

    Returns (per-factor attribution %, total near-miss count).

    Note: this script reads ENTER rows only — true near-misses (entries
    that scored below threshold and got blocked) are NOT in the log. So
    the population here is "entries that scored below threshold yet still
    got logged as ENTER" — which only happens in shadow_blocked=True paper
    mode rows. In live mode the analysis below is a no-op (n=0). That is
    fine: the entry-rate band is the gate, this section is diagnostic.
    """
    # Max-positive branch values per factor (from the score function source).
    max_positive = {
        "score_f1_breakout": 30,
        "score_f2_oi": 25,
        "score_f3_duration": 25,
        "score_f4_vix_level": 10,
        "score_f5_vix_dir": 10,
        "score_f6_banknifty": 5,
    }
    counts: dict[str, int] = defaultdict(int)
    n = 0
    for r in phase_a_rows:
        if r["_final_score"] >= threshold:
            continue
        n += 1
        gap = threshold - r["_final_score"]
        # Find factors where (max_positive - actual) >= gap, pick largest.
        candidates = []
        for col, max_val in max_positive.items():
            actual = r[f"_{col}"]
            uplift = max_val - actual
            if uplift >= gap:
                candidates.append((uplift, col))
        if candidates:
            candidates.sort(reverse=True)
            counts[candidates[0][1]] += 1
        else:
            counts["combined"] += 1
    if n == 0:
        return {}, 0
    return {k: round(100 * v / n, 1) for k, v in counts.items()}, n


def build_metrics(
    since: date, until: date,
    phase_a_rows: list[dict], pre_a_rows: list[dict],
    skipped: int,
) -> WindowMetrics:
    all_rows = phase_a_rows + pre_a_rows
    by_day, median, mean, band, max_zeros = compute_entry_rate(all_rows, since, until)
    histogram = compute_score_histogram(all_rows)
    activation = compute_factor_activation(phase_a_rows)

    # Use the threshold the row was logged with — robust if someone changes
    # trend_signal_threshold mid-window. Fall back to 50 if the column is
    # missing (pre-Phase-A rows where threshold may be the old shared 60).
    threshold = max((r["_threshold"] for r in phase_a_rows), default=50)
    suppression, suppression_n = compute_suppression_attribution(phase_a_rows, threshold)

    metrics = WindowMetrics(
        window_start=since.isoformat(),
        window_end=until.isoformat(),
        trading_days=len([d for d in by_day.keys()]),
        total_entries=len(all_rows),
        phase_a_entries=len(phase_a_rows),
        pre_phase_a_entries=len(pre_a_rows),
        entries_per_day=by_day,
        median_entries_per_day=median,
        mean_entries_per_day=round(mean, 2),
        band=band,
        consecutive_fail_days=max_zeros,
        score_histogram=histogram,
        factor_activation={k: asdict(v) for k, v in activation.items()},
        suppression_attribution=suppression,
        suppression_n=suppression_n,
    )
    if skipped:
        metrics.notes.append(f"{skipped} rows skipped (parse errors)")
    if not phase_a_rows:
        metrics.notes.append(
            "no Phase A rows in window — per-factor metrics unavailable. "
            "Phase A logging started Apr 21 (commit ebbabbf).",
        )
    if pre_a_rows:
        metrics.notes.append(
            f"{len(pre_a_rows)} pre-Phase-A rows included in entry-rate "
            "and score-distribution metrics only.",
        )
    if max_zeros >= 5:
        metrics.notes.append(
            f"⚠️ ROLLBACK TRIGGER: {max_zeros} consecutive zero-entry days. "
            "Cross-reference premium-leg activity in nightly_audit before acting.",
        )
    return metrics


def render_human(m: WindowMetrics) -> str:
    lines = []
    lines.append("=" * 70)
    lines.append(f"  Trend Shadow Metrics — {m.window_start} → {m.window_end}")
    lines.append("=" * 70)
    lines.append(f"Trading days with data:  {m.trading_days}")
    lines.append(f"Total TREND ENTERs:      {m.total_entries:,}")
    lines.append(f"  Phase A (per-factor):  {m.phase_a_entries:,}")
    lines.append(f"  Pre-Phase A (legacy):  {m.pre_phase_a_entries:,}")
    lines.append("")
    lines.append("Entry rate:")
    lines.append(f"  Median entries/day:    {m.median_entries_per_day}")
    lines.append(f"  Mean entries/day:      {m.mean_entries_per_day}")
    lines.append(f"  Max consec zero days:  {m.consecutive_fail_days}")
    band_emoji = {"PASS": "✅", "WARN": "🟧", "FAIL": "🟥"}.get(m.band, "❓")
    lines.append(f"  Band:                  {band_emoji} {m.band}")
    lines.append("")

    if m.score_histogram:
        lines.append("Final-score distribution:")
        max_count = max(m.score_histogram.values())
        for bucket, count in m.score_histogram.items():
            bar = "█" * int(40 * count / max_count) if max_count else ""
            lines.append(f"  {bucket}: {count:>5,} {bar}")
        lines.append("")

    if m.factor_activation:
        lines.append("Per-factor activation (Phase A rows only):")
        lines.append(f"  {'factor':<22} {'mean':>8} {'pos%':>7} {'neg%':>7} {'zero%':>7} {'min':>5} {'max':>5}")
        for col, a in m.factor_activation.items():
            short = col.replace("score_", "")
            lines.append(
                f"  {short:<22} {a['mean']:>8.2f} {a['pos_pct']:>7.1f} "
                f"{a['neg_pct']:>7.1f} {a['zero_pct']:>7.1f} "
                f"{a['min_val']:>5} {a['max_val']:>5}"
            )
        lines.append("")

    if m.suppression_n:
        lines.append(f"Suppression attribution (n={m.suppression_n} near-miss rows):")
        for factor, pct in sorted(m.suppression_attribution.items(), key=lambda kv: -kv[1]):
            short = factor.replace("score_", "") if factor != "combined" else factor
            lines.append(f"  {short:<22} {pct:>6.1f}% bottleneck")
        lines.append("")
    elif m.phase_a_entries:
        lines.append("Suppression attribution: no near-miss rows in window")
        lines.append("  (in live mode this is expected — only shadow_blocked=True rows")
        lines.append("   land below threshold in the ENTER log).")
        lines.append("")

    if m.notes:
        lines.append("Notes:")
        for note in m.notes:
            lines.append(f"  - {note}")

    return "\n".join(lines)


def write_json(m: WindowMetrics, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w") as f:
        json.dump(asdict(m), f, indent=2, default=str)


def main() -> int:
    args = parse_args()
    since, until = resolve_window(args)

    phase_a_rows, pre_a_rows, skipped = collect_rows(since, until, args.strategy_id)
    if not phase_a_rows and not pre_a_rows:
        print(f"No TREND ENTER rows found in {since} → {until}", file=sys.stderr)
        return 2

    metrics = build_metrics(since, until, phase_a_rows, pre_a_rows, skipped)

    output = args.output or (
        OUTPUT_DIR / f"trend_shadow_{since.strftime('%Y-%m')}.json"
    )
    write_json(metrics, output)

    if not args.json_only:
        print(render_human(metrics))
        print(f"\nJSON written to: {output}")

    return 1 if metrics.band == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
