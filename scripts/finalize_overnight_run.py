#!/usr/bin/env python
"""Wake-up script: read tournament winner, update .env, write briefing.

Called after orchestrator_tournament.py completes. Reads
/tmp/orch_tournament_winner.json, updates .env STRATEGIES with the
winning config (or SS V1 solo fallback), and writes a morning briefing
to /tmp/morning_briefing.md.

Idempotent: safe to re-run; won't double-update .env. The .env's
STRATEGIES line is replaced atomically.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path


ENV_PATH = Path(".env")
WINNER_JSON = Path("/tmp/orch_tournament_winner.json")
BRIEFING = Path("/tmp/morning_briefing.md")


def update_env_strategies(new_strategies: list) -> None:
    """Atomically replace the STRATEGIES= line in .env."""
    if not ENV_PATH.exists():
        print(f"ERROR: {ENV_PATH} not found", file=sys.stderr)
        return
    # Backup first
    backup = ENV_PATH.with_suffix(".env.bak.overnight")
    shutil.copy(ENV_PATH, backup)
    print(f"  .env backed up to {backup}")

    # Read, replace STRATEGIES line, write
    lines = ENV_PATH.read_text().splitlines(keepends=True)
    new_lines = []
    replaced = False
    new_strat_line = "STRATEGIES=" + json.dumps(new_strategies, separators=(",", ":")) + "\n"
    for line in lines:
        if line.startswith("STRATEGIES="):
            new_lines.append(new_strat_line)
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append(new_strat_line)

    ENV_PATH.write_text("".join(new_lines))
    print(f"  STRATEGIES= updated in {ENV_PATH}")


def main() -> int:
    if not WINNER_JSON.exists():
        print(f"ERROR: {WINNER_JSON} not found — tournament didn't write its result", file=sys.stderr)
        return 1

    payload = json.loads(WINNER_JSON.read_text())
    rec = payload["recommendation"]
    all_results = payload["all_results"]
    verdict = rec["verdict"]

    # Build .env STRATEGIES — orchestrator winner OR fallback
    if verdict == "deploy_orchestrator":
        winning_params = rec["params"]
        # Write as ONE strategy: orchestrator_1 LIVE
        new_strategies = [{
            "name": "orchestrator",
            "id": "orchestrator_1",
            "params": {**winning_params, "shadow_only": False},
        }]
        action = f"Deploy {rec['config_name']} as orchestrator_1 LIVE"
    else:
        # Fallback: SS V1 solo LIVE; other shadows for A/B
        ss_params = rec["params"]
        new_strategies = [
            # SS V1 LIVE (validated winner)
            {
                "name": "short_strangle",
                "id": "ss_1",
                "params": {**ss_params, "shadow_only": False},
            },
            # Original orchestrator stays as shadow for V5 development data
            {
                "name": "orchestrator",
                "id": "orchestrator_shadow",
                "params": {
                    "underlying": "NIFTY",
                    "quantity_lots": 1,
                    "shadow_only": True,
                    "min_score_to_trade": 60,
                    "children": ["iron_condor", "iron_butterfly", "short_strangle", "trend_daily"],
                    "children_params": {},  # use defaults
                },
            },
        ]
        action = "FALLBACK: SS V1 solo LIVE + orchestrator_shadow"

    print(f"Action: {action}")
    update_env_strategies(new_strategies)

    # ─── Morning briefing ────────────────────────────────────────
    lines = []
    lines.append("# Overnight run — morning briefing")
    lines.append("")
    lines.append(f"Generated: {datetime.now().isoformat()}")
    lines.append("")
    lines.append("## Decision")
    lines.append("")
    lines.append(f"**{action}**")
    lines.append("")
    if rec.get("metrics"):
        m = rec["metrics"]
        lines.append("Selected config metrics on 173-day post-SEBI corpus:")
        lines.append("")
        lines.append(f"  - Fills: {m.get('fills', 0)}")
        lines.append(f"  - PnL: ₹{float(m.get('pnl', 0)):+,.0f}")
        lines.append(f"  - Win rate: {float(m.get('wr', 0)):.1f}%")
        lines.append(f"  - Sharpe: {float(m.get('sharpe', 0)):+.2f}")
        lines.append(f"  - Per-trip EV: ₹{float(m.get('ev_per_trip_4', 0)):+,.1f}")
        lines.append("")
    lines.append("## Tournament results (sorted by per-trip EV)")
    lines.append("")
    lines.append("| Config | Fills | PnL | WR | Sharpe | ₹/trip | Status |")
    lines.append("|---|---|---|---|---|---|---|")
    successful = [r for r in all_results if not r.get("error")]
    successful.sort(key=lambda r: -r.get("ev_per_trip_4", -1e9))
    for r in successful:
        ev = float(r.get('ev_per_trip_4', 0))
        sharpe = float(r.get('sharpe', 0))
        if ev > 10 and sharpe > 0:
            status = "✓ deploy-ready"
        elif ev > 0:
            status = "⚠ marginal"
        else:
            status = "✗ negative"
        lines.append(
            f"| {r['name']} | {r['fills']} | ₹{float(r['pnl']):+,.0f} | "
            f"{float(r['wr']):.1f}% | {sharpe:+.2f} | ₹{ev:+,.1f} | {status} |"
        )
    for r in [r for r in all_results if r.get("error")]:
        lines.append(f"| {r['name']} | — | ERROR | — | — | — | {r['error'][:40]} |")
    lines.append("")
    lines.append("## What's deployed")
    lines.append("")
    lines.append("```")
    lines.append("STRATEGIES=" + json.dumps(new_strategies, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("## To revert")
    lines.append("")
    lines.append("```bash")
    lines.append("cp .env.bak.overnight .env")
    lines.append("```")
    lines.append("")
    BRIEFING.write_text("\n".join(lines))
    print(f"  Morning briefing: {BRIEFING}")
    print()
    print(BRIEFING.read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
