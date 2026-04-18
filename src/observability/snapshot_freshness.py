"""Pre-market check: did the daily config snapshotter fire today?

The counterfactual replay (``scripts/replay_counterfactual.py``) silently
falls back to live params when ``config/snapshots/<date>/`` doesn't exist
for the target date. We added a "snapshot-fallback" alert flavour to make
the silent-failure visible in the nightly report — but the *cause* is
"cron didn't fire" or "snapshotter crashed", and that's worth catching at
06:30 IST when the operator can still rerun the snapshotter manually
before market open, not at 21:00 IST when the day's data is already lost.

This module is the pure check; ``scripts/verify_system.py`` is the caller
that turns it into terminal output / exit code, and tests/unit/
test_snapshot_freshness.py owns the contract.

Companion to ``src/observability/heartbeat.py`` (recorder/trader liveness
check, same pattern).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

#: Default snapshot root mirrors ``scripts/snapshot_config.SNAPSHOT_ROOT``.
DEFAULT_SNAPSHOT_ROOT = Path("config/snapshots")

#: Files the snapshotter MUST write for a valid frozen-day-of replay.
#: ``params.json`` is what ``src/backtest/day_replay.py`` actually reads;
#: the others are human-review and replay-context.
REQUIRED_FILES: tuple[str, ...] = (
    "params.yaml",
    "params.json",
    "constants.json",
    "holidays.json",
)

#: Files we want but can live without. ``instruments.json`` is best-effort
#: because the broker call may be flaky at 09:00 IST; absence means replay
#: across an expiry roll *might* read today's instrument table instead of
#: day-of, which is wrong but rarely catastrophic.
RECOMMENDED_FILES: tuple[str, ...] = ("instruments.json",)

Level = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class SnapshotFreshness:
    """Outcome of a freshness check. ``level`` drives the caller's UX."""

    target: date
    level: Level
    message: str
    files_present: tuple[str, ...]
    files_missing_required: tuple[str, ...]
    files_missing_recommended: tuple[str, ...]


def check_snapshot_freshness(
    target: date,
    snapshot_root: Path = DEFAULT_SNAPSHOT_ROOT,
) -> SnapshotFreshness:
    """Inspect ``snapshot_root/<target>/manifest.json`` and classify freshness.

    Returns a :class:`SnapshotFreshness` regardless of state — never raises
    on missing files, so the verify-system runner doesn't need a try/except
    around every call.

    Level taxonomy:

    * ``ok``   — all required files present AND instruments.json present.
    * ``warn`` — directory missing entirely (cron hasn't fired yet — common
                 if verify-system runs before 09:00), OR required files are
                 there but instruments.json is missing (acceptable for
                 same-expiry-cycle replays).
    * ``fail`` — directory exists but the manifest is missing or unreadable
                 (partial write), OR one or more REQUIRED files are absent
                 (the snapshotter ran but blew up midway). This is the
                 failure mode that silently degrades the counterfactual.
    """
    snap_dir = snapshot_root / target.isoformat()

    if not snap_dir.exists():
        return SnapshotFreshness(
            target=target,
            level="warn",
            message=(
                f"No config snapshot for {target.isoformat()} — "
                f"run scripts/snapshot_config.py (cron may not have fired yet)"
            ),
            files_present=(),
            files_missing_required=REQUIRED_FILES,
            files_missing_recommended=RECOMMENDED_FILES,
        )

    manifest_path = snap_dir / "manifest.json"
    if not manifest_path.exists():
        return SnapshotFreshness(
            target=target,
            level="fail",
            message=(
                f"Snapshot dir for {target.isoformat()} exists but "
                f"manifest.json is missing — partial write, snapshotter likely crashed mid-run"
            ),
            files_present=(),
            files_missing_required=REQUIRED_FILES,
            files_missing_recommended=RECOMMENDED_FILES,
        )

    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
        files = tuple(sorted(manifest.get("files", {}).keys()))
    except (OSError, json.JSONDecodeError) as e:
        return SnapshotFreshness(
            target=target,
            level="fail",
            message=f"Snapshot manifest for {target.isoformat()} unreadable: {e}",
            files_present=(),
            files_missing_required=REQUIRED_FILES,
            files_missing_recommended=RECOMMENDED_FILES,
        )

    file_set = set(files)
    missing_req = tuple(f for f in REQUIRED_FILES if f not in file_set)
    missing_rec = tuple(f for f in RECOMMENDED_FILES if f not in file_set)

    if missing_req:
        return SnapshotFreshness(
            target=target,
            level="fail",
            message=(
                f"Snapshot for {target.isoformat()} is missing required files "
                f"{list(missing_req)} — counterfactual replay will silently use live params"
            ),
            files_present=files,
            files_missing_required=missing_req,
            files_missing_recommended=missing_rec,
        )

    if missing_rec:
        return SnapshotFreshness(
            target=target,
            level="warn",
            message=(
                f"Snapshot for {target.isoformat()} OK but missing recommended "
                f"{list(missing_rec)} — replay may break across expiry rolls"
            ),
            files_present=files,
            files_missing_required=(),
            files_missing_recommended=missing_rec,
        )

    return SnapshotFreshness(
        target=target,
        level="ok",
        message=f"Config snapshot fresh for {target.isoformat()} ({len(files)} files)",
        files_present=files,
        files_missing_required=(),
        files_missing_recommended=(),
    )
