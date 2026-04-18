"""A/B replay: portfolio strategy with hard PCR + max-pain filters OFF vs ON.

Step (b) of the Apr 18 audit follow-up. After fixing the chain-aggregate
pollution bug in `_apply_snapshot` (step a), the portfolio strategy now
sees real PCR_OI values (median 0.84, in-band 66 %) instead of the polluted
PCR (median 6.34, in-band 0.5 %). With realistic PCR in hand, we can
finally measure the impact of wiring the hard filters into the portfolio
premium leg — they were defaulting to enabled in `BaseStrategyParams` but
never called from `portfolio_strategy.py` (only iron_condor / strangle /
straddle called them).

This harness runs the portfolio replay twice on the same chain snapshots:
  Run 1: portfolio_filters_enabled = False  (current behavior)
  Run 2: portfolio_filters_enabled = True   (filters wired)

CRITICAL: PAPER_TRADING must be "false" before any strategy module loads,
otherwise `_paper_mode` is True and the hard-filter blocks become
[SHADOW_BLOCK] log lines (no actual return None) — A/B run identical.

Both runs use the same chain CSVs, same spot/VIX, same capital, same
random seeding (none — deterministic). The only delta is the params flag.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# 1. Force live-mode BEFORE any strategy module loads. portfolio_strategy.py
#    line 137 reads os.environ at __init__ time; if PAPER_TRADING is "true"
#    the [SHADOW_BLOCK] branches swallow the filter blocks.
#    NOTE: setting os.environ here is NOT enough — portfolio_strategy.__init__
#    calls scripts.auto_auth.load_env() which unconditionally re-reads .env
#    and clobbers our setting. The .env file itself is patched in main()
#    inside a try/finally so the override survives load_env().
os.environ["PAPER_TRADING"] = "false"
# 2. Disable AI confluence so it doesn't conflate the result.
os.environ["ADVISOR_CONFLUENCE_ENABLED"] = "false"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"

from src.backtest.replay_engine import ReplayBacktestEngine  # noqa: E402

CAPITAL = 1_000_000
SNAPSHOT_DIR = "data/chain_snapshots"
SPOT_CSV = "data/nifty_spot_minute_chain.csv"
VIX_CSV = "data/india_vix_minute_chain.csv"


def fmt_inr(x: float) -> str:
    return f"₹{x:>+12,.2f}"


def get(metrics: dict, *keys, default=0):
    for k in keys:
        if k in metrics:
            return metrics[k]
    return default


async def run_one(filters_on: bool, log_path: Path) -> dict:
    """Run one portfolio replay with portfolio_filters_enabled flipped.

    Tees the strategy's INFO logs to `log_path` so we can post-hoc count
    [SHADOW_BLOCK] / [PREMIUM blocked] lines for filter impact attribution.
    """
    import logging
    root = logging.getLogger()
    handler = logging.FileHandler(log_path, mode="w")
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    # Force INFO level so the strategy's filter logs make it to disk.
    prev_level = root.level
    root.setLevel(logging.INFO)
    try:
        engine = ReplayBacktestEngine()
        params = {"portfolio_filters_enabled": bool(filters_on)}
        result = await engine.run(
            strategy_name="portfolio",
            snapshot_dir=SNAPSHOT_DIR,
            spot_csv=SPOT_CSV,
            vix_csv=VIX_CSV,
            initial_capital=CAPITAL,
            strategy_params=params,
        )
    finally:
        root.removeHandler(handler)
        handler.close()
        root.setLevel(prev_level)
    return result


def _count_blocks(log_path: Path) -> dict[str, int]:
    """Count filter-block log lines in the per-run log."""
    counts = {"pcr": 0, "max_pain": 0}
    if not log_path.exists():
        return counts
    for line in log_path.read_text().splitlines():
        if "PREMIUM blocked: PCR_OI" in line:
            counts["pcr"] += 1
        elif "PREMIUM blocked: Spot" in line and "max pain" in line:
            counts["max_pain"] += 1
    return counts


def summarize(label: str, result: dict) -> dict:
    m = result.get("metrics", {})
    return {
        "label": label,
        "trades": get(m, "num_trades", "total_trades"),
        "win_rate": get(m, "win_rate"),
        "gross": get(m, "total_pnl") + get(m, "total_charges"),
        "charges": get(m, "total_charges"),
        "net": get(m, "total_pnl"),
        "sharpe": get(m, "sharpe_ratio"),
        "max_dd": get(m, "max_drawdown_pct"),
    }


def print_row(s: dict) -> None:
    print(
        f"{s['label']:<8} {s['trades']:>6}  "
        f"{s['win_rate']:>4.1f}%  {fmt_inr(s['gross'])} {fmt_inr(-s['charges'])} "
        f"{fmt_inr(s['net'])}  {(s['net']/CAPITAL)*100:>+6.3f}%  "
        f"{s['sharpe']:>+5.2f}  {s['max_dd']:>5.2f}%"
    )


def _patch_env_file() -> str:
    """Force PAPER_TRADING=false in .env, return original contents for restore."""
    original = ENV_FILE.read_text()
    new_lines = []
    saw_key = False
    for line in original.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("PAPER_TRADING="):
            new_lines.append("PAPER_TRADING=false\n")
            saw_key = True
        else:
            new_lines.append(line)
    if not saw_key:
        new_lines.append("PAPER_TRADING=false\n")
    ENV_FILE.write_text("".join(new_lines))
    return original


def _restore_env_file(original: str) -> None:
    ENV_FILE.write_text(original)


async def main() -> int:
    print("\nA/B replay — portfolio hard filters (PCR + max-pain)")
    print(f"PAPER_TRADING = {os.environ['PAPER_TRADING']}  "
          f"ADVISOR_CONFLUENCE = {os.environ['ADVISOR_CONFLUENCE_ENABLED']}")
    print("-" * 110)
    print(
        f"{'filter':<8} {'trades':>6}  {'win%':>4}  {'gross':>14} {'charges':>14} "
        f"{'net P&L':>14}  {'%cap':>6}  {'sharpe':>6}  {'maxDD':>6}"
    )
    print("-" * 110)

    original_env = _patch_env_file()
    log_off = Path("/tmp/ab_off.log")
    log_on = Path("/tmp/ab_on.log")
    try:
        print("  running OFF run ...", flush=True)
        r_off = await run_one(filters_on=False, log_path=log_off)
        s_off = summarize("OFF", r_off)
        print_row(s_off)

        print("  running ON  run ...", flush=True)
        r_on = await run_one(filters_on=True, log_path=log_on)
        s_on = summarize("ON", r_on)
        print_row(s_on)
    finally:
        _restore_env_file(original_env)
        print(f"  (restored .env)", flush=True)

    # Filter-block attribution (only meaningful for the ON run; OFF skips them)
    blocks_on = _count_blocks(log_on)
    blocks_off = _count_blocks(log_off)
    print(
        f"\nFilter-block log lines:  OFF pcr={blocks_off['pcr']} "
        f"mp={blocks_off['max_pain']}   "
        f"ON pcr={blocks_on['pcr']} mp={blocks_on['max_pain']}"
    )

    print("-" * 110)
    delta_trades = s_on["trades"] - s_off["trades"]
    delta_net = s_on["net"] - s_off["net"]
    print(f"\nDelta (ON - OFF): trades {delta_trades:+d}, "
          f"net P&L {fmt_inr(delta_net)}")

    if s_off["trades"] > 0:
        block_rate = (s_off["trades"] - s_on["trades"]) / s_off["trades"] * 100
        print(f"Filter block rate: {block_rate:+.1f}% of OFF-run trades blocked when ON")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
