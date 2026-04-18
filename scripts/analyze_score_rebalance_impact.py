#!/usr/bin/env python3
"""Scenario-bounded impact analysis: would the Apr 18 score rebalance
have flipped historical TREND ENTER decisions?

Built after the dual code+quant review on commit ee62c70 found that the
Apr 18 trend-improvements port shifted the score distribution without
recalibrating the threshold. The rebalance reduced factor 4 (VIX level),
bumped factor 5 threshold (1%→2%), and inverted factor 6 (BankNifty)
asymmetry. Factors 5 and 6 are NEW — they did not exist in the old
score function.

WHAT THIS SCRIPT CAN DO
-----------------------
The decision log captures `rule_score` (the OLD aggregate score) but
NOT the per-factor breakdown (no breakout_strength, oi_confirmed,
trend_duration_minutes, vix_prev, banknifty_confirming columns). We
can't recompute the new score exactly, so we frame three scenarios
and report all of them. The honest comparable metric is `factor4_only`
because that is the only factor whose contribution we can recover from
the log (VIX is captured per row).

THE THREE SCENARIOS
-------------------
For each TREND ENTER row, given its logged VIX:

  factor4_only  (PRIMARY — apples-to-apples)
    Apply only the factor-4 weight reduction (-10 / -3 / 0 by VIX band).
    Factors 5 and 6 are net-neutral by assumption — we don't know what
    they would have contributed because the inputs weren't logged.

  pessimistic   (DOWNSIDE BOUND)
    factor 4 reduction PLUS assume factors 5 and 6 fire their NEGATIVE
    branches (-15 for rising VIX on UP / falling VIX on DOWN, -20 for
    diverging BN). Use to bound risk; usually unrealistic for ENTER
    rows because diverging BN would have killed most entries already.

  optimistic    (UPSIDE BOUND)
    factor 4 reduction PLUS assume factors 5 and 6 fire their POSITIVE
    branches (+10 for VIX confirming, +5 for BN aligned). Realistic for
    a clean trending day where everything aligned.

USAGE
-----
    uv run python scripts/analyze_score_rebalance_impact.py

    # Override threshold (default 60 = signal_threshold for premium leg).
    # The trend leg now has trend_signal_threshold=50, so re-run with that:
    uv run python scripts/analyze_score_rebalance_impact.py --threshold 50

OUTPUT
------
    - Block rate under each scenario (factor4_only is the headline)
    - Histogram of factor4_only new scores
    - Per-day breakdown to spot regime sensitivities

INTERPRETATION
--------------
If <5% of historical entries fall below threshold under factor4_only,
the rebalance is approximately a no-op for the part we can measure,
and the path forward is to ship behind the new trend_signal_threshold
and instrument the live rate. 5-20% means it's material but bounded —
collect 30 days of shadow data with per-factor breakdowns logged, then
decide. >20% means recalibrate or roll back before live capital.
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DECISIONS_DIR = REPO_ROOT / "data" / "decisions"


def shifted_new_score(rule_score: int, vix: float, scenario: str = "factor4_only") -> int:
    """New score for an old ENTER row under one of three scenarios.

    Critical context: the OLD rule_score in the decision logs was computed
    BEFORE factors 5 (VIX direction) and 6 (BankNifty) existed. So the
    only direct comparable shift is the factor 4 (VIX level) reduction.
    Factors 5 and 6 are NEW signals — they could push the new score up,
    down, or leave it flat depending on conditions we can't recover from
    the log alone.

    Scenarios:
      - "factor4_only": only the factor 4 reduction is applied. This is
        the apples-to-apples comparison: old 4-factor score → new 4-factor
        score (with factor 4 weight reduced). The fairest single-number
        estimate of the shift; new factors 5/6 are net-neutral assumption.
      - "pessimistic": factor 4 reduction PLUS assume factors 5/6 fire
        their negative branches (-15 and -20). This is the worst-case
        downside. Use to bound risk.
      - "optimistic": factor 4 reduction PLUS assume factors 5/6 fire
        their positive branches (+10 and +5). This is the upside case
        when a real trend has VIX confirming and BN aligned.
    """
    shift = 0
    # Factor 4 reduction — applied in all scenarios
    if vix >= 14:
        shift -= 10  # factor 4: was +20, now +10
    elif vix >= 11:
        shift -= 3   # factor 4: was +8, now +5

    if scenario == "pessimistic":
        shift -= 15  # factor 5 worst (rising on UP / falling on DOWN)
        shift -= 20  # factor 6 worst (diverging)
    elif scenario == "optimistic":
        shift += 10  # factor 5 best (stable on UP / rising on DOWN)
        shift += 5   # factor 6 best (confirming)

    # Clamp at [0, 100] — matches the new score function's clamp.
    return max(0, min(100, rule_score + shift))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--threshold", type=int, default=60,
        help="Trend score threshold (default 60 — matches PortfolioParams.signal_threshold)",
    )
    parser.add_argument(
        "--strategy-id", type=str, default=None,
        help="Filter to one strategy_id (e.g. 'portfolio_hist'); default = all",
    )
    parser.add_argument(
        "--leg", type=str, default="TREND",
        help="Leg to analyse (TREND or PREMIUM). Default TREND.",
    )
    args = parser.parse_args()

    files = sorted(DECISIONS_DIR.glob("decisions_*.csv"))
    if not files:
        print(f"No decision logs found in {DECISIONS_DIR}")
        return 1

    enter_rows: list[dict] = []
    skipped = 0
    for f in files:
        with open(f, newline="") as fp:
            reader = csv.DictReader(fp)
            for row in reader:
                if row.get("leg") != args.leg or row.get("decision") != "ENTER":
                    continue
                if args.strategy_id and row.get("strategy_id") != args.strategy_id:
                    continue
                try:
                    enter_rows.append({
                        "date": row["timestamp"][:10],
                        "vix": float(row["vix"]),
                        "rule_score": int(row["rule_score"]),
                        "final_score": int(row.get("final_score") or row["rule_score"]),
                    })
                except (KeyError, ValueError):
                    skipped += 1

    if not enter_rows:
        print(f"No {args.leg} ENTER rows matched filters")
        return 1

    # Compute new score per row under all three scenarios.
    # factor4_only is the apples-to-apples comparison (old rule_score predates
    # factors 5/6 — they didn't exist). pessimistic / optimistic bound the
    # impact once the new factors start contributing.
    for r in enter_rows:
        r["new_f4"]   = shifted_new_score(r["rule_score"], r["vix"], "factor4_only")
        r["new_pess"] = shifted_new_score(r["rule_score"], r["vix"], "pessimistic")
        r["new_opt"]  = shifted_new_score(r["rule_score"], r["vix"], "optimistic")
        # "Blocked" = old cleared the threshold but new doesn't.
        r["block_f4"]   = r["new_f4"]   < args.threshold and r["rule_score"] >= args.threshold
        r["block_pess"] = r["new_pess"] < args.threshold and r["rule_score"] >= args.threshold
        r["block_opt"]  = r["new_opt"]  < args.threshold and r["rule_score"] >= args.threshold

    n = len(enter_rows)
    blocked_f4   = sum(1 for r in enter_rows if r["block_f4"])
    blocked_pess = sum(1 for r in enter_rows if r["block_pess"])
    blocked_opt  = sum(1 for r in enter_rows if r["block_opt"])
    pct_f4   = 100 * blocked_f4   / n
    pct_pess = 100 * blocked_pess / n
    pct_opt  = 100 * blocked_opt  / n

    # Histogram bins (factor4_only — the comparable apples-to-apples view)
    bins = defaultdict(int)
    for r in enter_rows:
        bucket = (r["new_f4"] // 10) * 10
        bins[bucket] += 1

    print("=" * 70)
    print(f"  Score Rebalance Impact Analysis — {args.leg} ENTER decisions")
    print("=" * 70)
    print(f"Total ENTER rows analyzed:     {n:>6,}")
    print(f"Skipped (parse errors):        {skipped:>6,}")
    print(f"Threshold:                     {args.threshold:>6}")
    print(f"Strategy filter:               {args.strategy_id or '(all)'}")
    print()
    print(f"Old rule_score min/median/max: "
          f"{min(r['rule_score'] for r in enter_rows):>3} / "
          f"{sorted(r['rule_score'] for r in enter_rows)[n // 2]:>3} / "
          f"{max(r['rule_score'] for r in enter_rows):>3}")
    print(f"New (factor4_only) min/med/max:"
          f"{min(r['new_f4'] for r in enter_rows):>3} / "
          f"{sorted(r['new_f4'] for r in enter_rows)[n // 2]:>3} / "
          f"{max(r['new_f4'] for r in enter_rows):>3}")
    print()
    print("Blocked under each scenario (old≥threshold AND new<threshold):")
    print(f"  factor4_only (primary):  {blocked_f4:>5,} / {n:,} = {pct_f4:>5.1f}%")
    print(f"  pessimistic   (bound ↓): {blocked_pess:>5,} / {n:,} = {pct_pess:>5.1f}%")
    print(f"  optimistic    (bound ↑): {blocked_opt:>5,} / {n:,} = {pct_opt:>5.1f}%")
    print()

    # Verdict band — keyed off factor4_only (the honest comparable metric)
    if pct_f4 < 5:
        verdict = "✅ SAFE — rebalance is ~no-op for historical entries (factor4_only)"
    elif pct_f4 < 20:
        verdict = "🟧 NEEDS SHADOW DATA — material but bounded; instrument and decide after 30 days"
    else:
        verdict = "🟥 RESTRICTIVE — recalibrate threshold (or roll back rebalance) before live"
    print(f"Verdict: {verdict}")
    print()

    # factor4_only histogram
    print("New-score distribution (factor4_only scenario):")
    max_count = max(bins.values()) if bins else 0
    for bucket in sorted(bins.keys()):
        count = bins[bucket]
        bar = "█" * int(40 * count / max_count) if max_count else ""
        marker = " <-- threshold" if bucket <= args.threshold < bucket + 10 else ""
        print(f"  {bucket:3}-{bucket + 9:3}: {count:>5,} {bar}{marker}")
    print()

    # Per-day blocked summary (last 15 days) — uses factor4_only
    by_day = defaultdict(lambda: {"n": 0, "blocked": 0})
    for r in enter_rows:
        by_day[r["date"]]["n"] += 1
        if r["block_f4"]:
            by_day[r["date"]]["blocked"] += 1
    print("Per-day breakdown (last 15 trading days, factor4_only):")
    for date in sorted(by_day.keys())[-15:]:
        d = by_day[date]
        pct = 100 * d["blocked"] / d["n"] if d["n"] else 0
        marker = " ⚠️" if pct >= 20 else ""
        print(f"  {date}: {d['n']:>3} entries, {d['blocked']:>3} blocked ({pct:>4.0f}%){marker}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
