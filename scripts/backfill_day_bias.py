"""Generate deterministic per-day DayBias files for replay/backtest A/B.

Why this exists
---------------
The AI advisor pipeline (`scripts/morning_advisor.py`) calls Claude once per
trading morning and writes one `data/day_bias.json` for that day. In live
trading that single file is read by the portfolio strategy at session start.

In a chain-replay backtest we want to A/B `advisor_confluence_enabled`
across many simulated days. The live path can't supply per-day files —
calling Claude N times costs money and re-introduces non-determinism.

So this script generates a *deterministic* DayBias per replay day from
observable market state (open spot vs prior close, VIX level, VIX change).
The bias is a synthetic stand-in — it is not an "AI prediction"; it's a
mechanical rule that produces the kind of confidence/score-adjustment
shape the live AI would produce, but reproducibly. Use the result to
validate plumbing and measure sensitivity, NOT to validate AI alpha.

How the strategy picks it up
----------------------------
`src/advisor/confluence.load_day_bias()` was extended to accept an
`as_of: date` arg and to consult the `BACKFILL_DAY_BIAS_DIR` env var.
When both are set it reads `<dir>/day_bias_<YYYY-MM-DD>.json` instead of
the live file. `portfolio_strategy._load_day_bias()` passes the simulated
clock date during replay reset_day_state. So the workflow is:

    uv run python scripts/backfill_day_bias.py \
        --start 2026-04-01 --end 2026-04-17 \
        --out data/day_bias_backfill

    BACKFILL_DAY_BIAS_DIR=data/day_bias_backfill \
    ADVISOR_CONFLUENCE_ENABLED=true \
    uv run python scripts/replay_chain.py ...

Calibration
-----------
The synthetic confidences are intentionally pushed ABOVE the live
0.7 confluence gate, otherwise the A/B is a no-op (this is exactly the
problem the Apr 18 audit found in production logs: 0/978 decisions ever
got an adjustment because live conf ran 0.4-0.6). Document this in any
A/B writeup — the synthetic bias will fire adjustments more often than
live AI did, so the measured delta is an upper bound on plumbing impact.

Rule (deliberately simple, easy to defend):
  * VIX > 18 → mode_bias=skip_premium, prem_score_adj=-12, conf=0.85
                (high vol → AI would discourage premium selling)
  * 13 ≤ VIX ≤ 18 → mode_bias=iron_condor, prem_score_adj=+8, conf=0.75
                (mid vol → AI would lean into IC)
  * VIX < 13 → mode_bias=strangle, prem_score_adj=+10, conf=0.80
                (low vol → AI would prefer naked-leg strangle)
  * Trend leg adj based on opening move from prior close:
      |gap| > 0.4% → trend_adj=+10, conf=0.75 (chase the gap)
      |gap| < 0.1% → trend_adj=-8, conf=0.75 (mean-revert mode)
      otherwise   → trend_adj=0, conf=0.5 (no opinion, will be ignored)
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SPOT_CSV = ROOT / "data" / "nifty_spot_minute.csv"
VIX_CSV = ROOT / "data" / "india_vix_minute.csv"
DEFAULT_OUT = ROOT / "data" / "day_bias_backfill"


def _load_day_aggregates(spot_path: Path, vix_path: Path) -> dict[date, dict]:
    """Return {day: {open_spot, close_spot, prev_close, open_vix, close_vix}}."""
    spot_by_day: dict[date, list[tuple[datetime, float]]] = defaultdict(list)
    vix_by_day: dict[date, list[tuple[datetime, float]]] = defaultdict(list)

    for path, sink in ((spot_path, spot_by_day), (vix_path, vix_by_day)):
        if not path.exists():
            print(f"WARN: {path} not found — skipping", file=sys.stderr)
            continue
        with open(path) as f:
            reader = csv.DictReader(f)
            for r in reader:
                try:
                    ts = datetime.fromisoformat(r["date"])
                except (ValueError, KeyError):
                    continue
                try:
                    close = float(r["close"])
                except (ValueError, KeyError):
                    continue
                sink[ts.date()].append((ts, close))

    days = sorted(set(spot_by_day) | set(vix_by_day))
    out: dict[date, dict] = {}
    prev_close = None
    for d in days:
        s = sorted(spot_by_day.get(d, []))
        v = sorted(vix_by_day.get(d, []))
        rec = {
            "open_spot": s[0][1] if s else 0.0,
            "close_spot": s[-1][1] if s else 0.0,
            "prev_close_spot": prev_close,
            "open_vix": v[0][1] if v else 0.0,
            "close_vix": v[-1][1] if v else 0.0,
            "spot_minutes": len(s),
        }
        out[d] = rec
        if s:
            prev_close = s[-1][1]
    return out


def _bias_for_day(day: date, agg: dict) -> dict:
    """Build a synthetic DayBias dict (matches src/advisor/models.DayBias)."""
    open_vix = agg["open_vix"] or agg["close_vix"]
    open_spot = agg["open_spot"]
    prev_close = agg["prev_close_spot"]

    # ─── Premium leg signal — keyed on VIX regime ────────────────
    if open_vix > 18:
        mode_bias = "skip_premium"
        prem_adj = -12
        prem_conf = 0.85
        risk_level = "HIGH"
    elif open_vix >= 13:
        mode_bias = "iron_condor"
        prem_adj = 8
        prem_conf = 0.75
        risk_level = "MEDIUM"
    elif open_vix > 0:
        mode_bias = "strangle"
        prem_adj = 10
        prem_conf = 0.80
        risk_level = "LOW"
    else:
        # No VIX data — refuse to opine (live behavior would be similar)
        mode_bias = "no_opinion"
        prem_adj = 0
        prem_conf = 0.0
        risk_level = "MEDIUM"

    # ─── Trend leg signal — keyed on opening gap from prior close ─
    if open_spot and prev_close:
        gap_pct = abs(open_spot - prev_close) / prev_close * 100.0
        if gap_pct > 0.4:
            trend_adj = 10
            trend_conf = 0.75
        elif gap_pct < 0.1:
            trend_adj = -8
            trend_conf = 0.75
        else:
            trend_adj = 0
            trend_conf = 0.5  # below confluence threshold → ignored downstream
    else:
        trend_adj = 0
        trend_conf = 0.0

    sizing = 0.8 if open_vix > 20 else (1.2 if open_vix < 12 and open_vix > 0 else 1.0)

    return {
        "date": day.isoformat(),
        "risk_level": risk_level,
        "mode_bias": mode_bias,
        "sizing_multiplier": sizing,
        "premium_score_adj": prem_adj,
        "premium_confidence": prem_conf,
        "trend_score_adj": trend_adj,
        "trend_confidence": trend_conf,
        "key_levels": {"support": 0.0, "resistance": 0.0, "max_pain": 0.0},
        "reasoning": (
            f"[SYNTHETIC] VIX open={open_vix:.2f} → premium {mode_bias} "
            f"(adj {prem_adj:+d}, conf {prem_conf:.2f}). "
            f"Spot open={open_spot:.0f} vs prev_close={prev_close or 0:.0f} → "
            f"trend adj {trend_adj:+d} (conf {trend_conf:.2f}). "
            f"Generated by scripts/backfill_day_bias.py — NOT an AI prediction. "
            f"Use only for plumbing/sensitivity tests."
        ),
        "confidence": max(prem_conf, trend_conf),
    }


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--start", type=str, required=False,
                   help="YYYY-MM-DD; default: earliest day in spot CSV")
    p.add_argument("--end", type=str, required=False,
                   help="YYYY-MM-DD; default: latest day in spot CSV")
    p.add_argument("--out", type=str, default=str(DEFAULT_OUT),
                   help="Output directory (default: data/day_bias_backfill)")
    p.add_argument("--spot-csv", type=str, default=str(SPOT_CSV))
    p.add_argument("--vix-csv", type=str, default=str(VIX_CSV))
    p.add_argument("--dry-run", action="store_true",
                   help="Print summary, write no files")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    out_dir = Path(args.out)
    aggregates = _load_day_aggregates(Path(args.spot_csv), Path(args.vix_csv))
    if not aggregates:
        print("ERROR: no spot/VIX data found", file=sys.stderr)
        return 1

    all_days = sorted(aggregates.keys())
    start = date.fromisoformat(args.start) if args.start else all_days[0]
    end = date.fromisoformat(args.end) if args.end else all_days[-1]

    days = [d for d in all_days if start <= d <= end]
    if not days:
        print(f"ERROR: no days in window {start} → {end}", file=sys.stderr)
        return 1

    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    skip_premium = 0
    ic = 0
    strangle = 0
    print(f"Window: {days[0]} → {days[-1]} ({len(days)} days)")
    for d in days:
        bias = _bias_for_day(d, aggregates[d])
        if bias["mode_bias"] == "skip_premium":
            skip_premium += 1
        elif bias["mode_bias"] == "iron_condor":
            ic += 1
        elif bias["mode_bias"] == "strangle":
            strangle += 1
        if not args.dry_run:
            target = out_dir / f"day_bias_{d.isoformat()}.json"
            with open(target, "w") as f:
                json.dump(bias, f, indent=2)
            written += 1

    print(f"\nMode-bias distribution over window:")
    print(f"  skip_premium : {skip_premium:>3}  (VIX > 18)")
    print(f"  iron_condor  : {ic:>3}  (13 ≤ VIX ≤ 18)")
    print(f"  strangle     : {strangle:>3}  (VIX < 13)")
    if args.dry_run:
        print(f"\n[DRY-RUN] Would have written {len(days)} files to {out_dir}")
    else:
        print(f"\nWrote {written} files to {out_dir}/")
        print(f"Use with: BACKFILL_DAY_BIAS_DIR={out_dir} ADVISOR_CONFLUENCE_ENABLED=true …")
    return 0


if __name__ == "__main__":
    sys.exit(main())
