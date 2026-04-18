"""Move chain CSV files captured on non-trading days to _quarantine/.

Background
----------
The chain recorder gained a weekend/holiday gate in commit 921235b
(Apr 18, 2026), but a long-running daemon started before that commit
will keep producing files until restarted. This script is the cleanup
broom — run it whenever you suspect orphan captures, and definitely
right after restarting the daemon to pick up a recorder gate fix.

It scans `data/chain_snapshots/chain_YYYY-MM-DD.csv` for any file
whose date `MarketClock.is_trading_holiday()` returns True for, and
moves it to `data/chain_snapshots/_quarantine/`. Idempotent: re-running
on a clean dir is a no-op.

It also updates `_quarantine/README.md` with a row per newly-quarantined
file so the audit trail stays current.

Usage
-----
    uv run python scripts/quarantine_holiday_chains.py
    uv run python scripts/quarantine_holiday_chains.py --dry-run

The replay engine and any A/B harness must NEVER read from _quarantine/.
This script does not touch active recorder files of trading days, even
if those files look suspicious — that's a separate quality-gate problem.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.clock import MarketClock  # noqa: E402

CHAINS_DIR = ROOT / "data" / "chain_snapshots"
QUARANTINE_DIR = CHAINS_DIR / "_quarantine"
README_PATH = QUARANTINE_DIR / "README.md"
FILENAME_RE = re.compile(r"^chain_(\d{4}-\d{2}-\d{2})\.csv$")


def _scan(active_dir: Path, clock: MarketClock) -> list[tuple[Path, date]]:
    """Return [(path, parsed_date)] for files whose date is a non-trading day."""
    out: list[tuple[Path, date]] = []
    if not active_dir.exists():
        return out
    for entry in sorted(active_dir.iterdir()):
        if not entry.is_file():
            continue
        m = FILENAME_RE.match(entry.name)
        if not m:
            continue
        try:
            d = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if clock.is_trading_holiday(d):
            out.append((entry, d))
    return out


def _resolve_destination(target_dir: Path, name: str) -> Path:
    """Return a path that doesn't clobber an existing quarantined file.

    If `_quarantine/chain_YYYY-MM-DD.csv` already exists (the prior orphan
    from before the daemon restart), append `.1`, `.2`, ... to the new file
    so we keep both. The replay engine ignores the whole quarantine
    directory anyway, but losing the diff between two orphan captures
    would hide useful audit information about what the daemon was doing.
    """
    target = target_dir / name
    if not target.exists():
        return target
    base = target_dir / name
    for i in range(1, 100):
        candidate = base.with_suffix(f".csv.{i}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find a free quarantine slot for {name}")


def _update_readme(moved: list[tuple[Path, date]]) -> None:
    """Append rows to the quarantine README so audit history stays current."""
    if not moved or not README_PATH.exists():
        return
    existing = README_PATH.read_text()
    new_rows: list[str] = []
    for src, d in moved:
        # Use the resolved (possibly suffixed) filename, not the original.
        weekday = d.strftime("%A")
        # Reason guess: weekend vs published holiday.
        reason = "NSE closed" if d.weekday() >= 5 else "NSE holiday"
        row = f"| `{src.name}` | {weekday:<8} | {reason} |"
        if row in existing:
            continue  # Already documented — idempotent re-runs.
        new_rows.append(row)
    if not new_rows:
        return
    addendum = (
        "\n\n## Auto-quarantine (scripts/quarantine_holiday_chains.py)\n\n"
        if "## Auto-quarantine" not in existing else ""
    )
    if addendum:
        existing += addendum + "| File | Weekday | Reason |\n|------|---------|--------|\n"
    existing += "\n".join(new_rows) + "\n"
    README_PATH.write_text(existing)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dry-run", action="store_true",
                   help="List orphan files but don't move them")
    p.add_argument("--chains-dir", type=str, default=str(CHAINS_DIR))
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    chains_dir = Path(args.chains_dir)
    quarantine_dir = chains_dir / "_quarantine"

    clock = MarketClock()
    orphans = _scan(chains_dir, clock)

    if not orphans:
        print(f"No orphan files found in {chains_dir}/ — nothing to quarantine.")
        return 0

    print(f"Found {len(orphans)} orphan file(s):")
    for path, d in orphans:
        size = path.stat().st_size if path.exists() else 0
        print(f"  {path.name}  ({d.strftime('%A')}, {size:,} bytes)")

    if args.dry_run:
        print("\n[DRY-RUN] No files moved.")
        return 0

    quarantine_dir.mkdir(parents=True, exist_ok=True)
    moved: list[tuple[Path, date]] = []
    for src, d in orphans:
        dest = _resolve_destination(quarantine_dir, src.name)
        shutil.move(str(src), str(dest))
        print(f"  moved {src.name} → _quarantine/{dest.name}")
        # Track the destination path so the README gets the suffixed name.
        moved.append((dest, d))

    _update_readme(moved)
    print(f"\nQuarantined {len(moved)} file(s). README updated.")
    print("REMINDER: if the recorder daemon is still running, restart it "
          "to pick up the weekend gate, otherwise this dir will fill up again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
