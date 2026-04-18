"""Tests for src.observability.snapshot_freshness.

The contract this file locks in:

  1. **Missing snapshot dir → warn**, not fail. The snapshotter cron runs at
     09:00 IST; if verify-system runs at 06:30 the absence is expected.
  2. **Missing manifest with dir present → fail**, because that means the
     snapshotter started but blew up mid-run — the silent-fallback failure
     mode the counterfactual warns about.
  3. **Missing required file → fail**, ditto.
  4. **Missing recommended file (instruments.json) → warn**, not fail.
     Acceptable for same-expiry-cycle replays.
  5. **All required + recommended present → ok**, message states file count.
  6. **Unreadable manifest → fail**, error embedded in message.
  7. **Function never raises** — every branch returns a SnapshotFreshness.

These are pure tests against the classifier — no fs setup beyond tmp_path.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src.observability.snapshot_freshness import (
    REQUIRED_FILES,
    SnapshotFreshness,
    check_snapshot_freshness,
)

TARGET = date(2026, 4, 18)


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────

@pytest.fixture
def snapshot_root(tmp_path: Path) -> Path:
    """Empty snapshot root — tests populate it as needed."""
    root = tmp_path / "snapshots"
    root.mkdir()
    return root


def _write_manifest(snap_dir: Path, files: dict[str, str] | None = None) -> None:
    """Write a manifest.json mimicking what scripts/snapshot_config.py emits."""
    snap_dir.mkdir(parents=True, exist_ok=True)
    if files is None:
        files = {f: "deadbeef" * 8 for f in REQUIRED_FILES + ("instruments.json",)}
    payload = {
        "snapshot_date": TARGET.isoformat(),
        "captured_at": "2026-04-18T03:30:00Z",
        "git_sha": "abc123",
        "files": files,
        "skipped": [],
        "snapshot_version": 1,
    }
    (snap_dir / "manifest.json").write_text(json.dumps(payload))


# ────────────────────────────────────────────────────────────────────
# 1. Missing dir → warn (cron hasn't fired yet)
# ────────────────────────────────────────────────────────────────────

def test_missing_dir_returns_warn(snapshot_root: Path):
    rep = check_snapshot_freshness(TARGET, snapshot_root)
    assert isinstance(rep, SnapshotFreshness)
    assert rep.level == "warn"
    assert rep.target == TARGET
    assert "No config snapshot" in rep.message
    assert rep.files_present == ()
    assert set(rep.files_missing_required) == set(REQUIRED_FILES)


# ────────────────────────────────────────────────────────────────────
# 2. Dir present but no manifest → fail (partial write)
# ────────────────────────────────────────────────────────────────────

def test_dir_without_manifest_returns_fail(snapshot_root: Path):
    snap_dir = snapshot_root / TARGET.isoformat()
    snap_dir.mkdir()
    # Write a stray params.yaml but no manifest — simulates snapshotter
    # crash after the first write.
    (snap_dir / "params.yaml").write_text("foo: bar\n")
    rep = check_snapshot_freshness(TARGET, snapshot_root)
    assert rep.level == "fail"
    assert "manifest.json is missing" in rep.message
    assert "partial write" in rep.message


# ────────────────────────────────────────────────────────────────────
# 3. Manifest present but missing required file → fail
# ────────────────────────────────────────────────────────────────────

def test_missing_required_file_returns_fail(snapshot_root: Path):
    snap_dir = snapshot_root / TARGET.isoformat()
    # Manifest claims only params.yaml + constants.json — params.json missing.
    _write_manifest(snap_dir, files={"params.yaml": "h1", "constants.json": "h2"})
    rep = check_snapshot_freshness(TARGET, snapshot_root)
    assert rep.level == "fail"
    assert "missing required files" in rep.message
    assert "params.json" in rep.message
    assert "holidays.json" in rep.message
    assert set(rep.files_missing_required) >= {"params.json", "holidays.json"}


# ────────────────────────────────────────────────────────────────────
# 4. Recommended file (instruments.json) missing → warn
# ────────────────────────────────────────────────────────────────────

def test_missing_instruments_returns_warn(snapshot_root: Path):
    snap_dir = snapshot_root / TARGET.isoformat()
    files = {f: "deadbeef" for f in REQUIRED_FILES}
    _write_manifest(snap_dir, files=files)
    rep = check_snapshot_freshness(TARGET, snapshot_root)
    assert rep.level == "warn"
    assert "missing recommended" in rep.message
    assert "instruments.json" in rep.message
    assert rep.files_missing_required == ()
    assert rep.files_missing_recommended == ("instruments.json",)


# ────────────────────────────────────────────────────────────────────
# 5. All present → ok
# ────────────────────────────────────────────────────────────────────

def test_full_snapshot_returns_ok(snapshot_root: Path):
    snap_dir = snapshot_root / TARGET.isoformat()
    _write_manifest(snap_dir)  # default writes everything
    rep = check_snapshot_freshness(TARGET, snapshot_root)
    assert rep.level == "ok"
    assert "fresh" in rep.message
    assert rep.files_missing_required == ()
    assert rep.files_missing_recommended == ()
    # Files in `files_present` should be sorted for stable ordering
    assert list(rep.files_present) == sorted(rep.files_present)


# ────────────────────────────────────────────────────────────────────
# 6. Unreadable manifest → fail
# ────────────────────────────────────────────────────────────────────

def test_corrupt_manifest_returns_fail(snapshot_root: Path):
    snap_dir = snapshot_root / TARGET.isoformat()
    snap_dir.mkdir()
    (snap_dir / "manifest.json").write_text("{not valid json")
    rep = check_snapshot_freshness(TARGET, snapshot_root)
    assert rep.level == "fail"
    assert "unreadable" in rep.message


# ────────────────────────────────────────────────────────────────────
# 7. Function never raises — even on truly weird inputs
# ────────────────────────────────────────────────────────────────────

def test_function_does_not_raise_on_nonexistent_root(tmp_path: Path):
    """A snapshot root that doesn't exist on disk shouldn't crash the check —
    it should classify as warn (same as missing date dir)."""
    bogus_root = tmp_path / "definitely-does-not-exist"
    rep = check_snapshot_freshness(TARGET, bogus_root)
    assert rep.level == "warn"


def test_dataclass_is_frozen():
    """Lock the result as immutable so callers can pass it around without
    worrying about downstream mutation contaminating the message."""
    rep = SnapshotFreshness(
        target=TARGET, level="ok", message="x",
        files_present=(), files_missing_required=(), files_missing_recommended=(),
    )
    with pytest.raises((AttributeError, Exception)):
        rep.level = "fail"  # type: ignore[misc]
