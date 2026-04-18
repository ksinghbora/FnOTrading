"""Tests for scripts/restore_test.py — the cold-storage chain step 3.

Contracts this file locks in:
  1. Status "ok" when sha1 + row count + content hash all agree.
  2. Status "sha1_mismatch" when remote bytes differ from upload manifest.
  3. Status "missing_remote" when the remote key doesn't exist.
  4. Status "decode_failed" when downloaded bytes aren't valid Parquet.
  5. Status "row_count_mismatch" when archive ↔ source row counts diverge.
  6. Status "source_rotated_ok" when source CSV gone but archive intact —
     informational, NOT failure.
  7. format_alert returns None on all-clean (silent), string on any failure.
  8. Worst-failure status surfaces in the alert text.
  9. Default picker selects the most recent upload.
 10. --target-date selector picks a specific archive entry.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import archive_to_parquet as atp  # noqa: E402
import restore_test as rt  # noqa: E402
import upload_to_b2 as ub  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# End-to-end harness — sets up archive + upload manifests with one file
# ────────────────────────────────────────────────────────────────────


def _setup_chain(tmp_path: Path, *, target_date: str = "2026-04-15", rows: int = 200):
    """Create the FULL chain: source CSV → archive → upload → return paths.

    Returns dict with:
      - source_csv, archive_path, am, upm, uploader, archive_rec, upload_rec
    """
    import numpy as np
    rng = np.random.default_rng(42)
    src_dir = tmp_path / "data" / "chain_snapshots"
    src_dir.mkdir(parents=True)
    src = src_dir / f"chain_{target_date}.csv"
    df = pd.DataFrame({
        "time": [f"2026-04-15T09:{15+i//60}:{i%60:02d}.000+05:30" for i in range(rows)],
        "underlying": ["NIFTY"] * rows,
        "expiry": ["2026-04-21"] * rows,
        "strike": rng.choice([22500, 23000, 23500], size=rows).astype(float),
        "option_type": rng.choice(["CE", "PE"], size=rows),
        "ltp": rng.uniform(10, 200, size=rows).round(2),
        "iv": rng.uniform(10, 30, size=rows).round(2),
        "delta": rng.uniform(-1, 1, size=rows).round(4),
        "gamma": rng.uniform(0, 0.01, size=rows).round(6),
        "theta": rng.uniform(-50, 0, size=rows).round(4),
        "vega": rng.uniform(0, 100, size=rows).round(4),
        "oi": rng.integers(0, 1_000_000, size=rows),
        "volume": rng.integers(0, 100_000, size=rows),
        "bid_price": rng.uniform(0, 100, size=rows).round(2),
        "ask_price": rng.uniform(0, 100, size=rows).round(2),
    })
    df.to_csv(src, index=False)

    archive_root = tmp_path / "archive"
    am = archive_root / "manifest.jsonl"
    upm = archive_root / "manifest_b2.jsonl"
    from datetime import date as _date
    arec = atp.archive_chain(
        src,
        target_date=_date.fromisoformat(target_date),
        archive_root=archive_root,
        manifest=am,
    )
    assert arec is not None

    up = ub.LocalDirUploader(root=tmp_path / "remote")
    urec = ub.upload_archive(
        json.loads(am.read_text().strip()), up, upload_manifest=upm,
    )
    assert urec is not None

    return {
        "source_csv": src,
        "archive_path": Path(arec.archive_path),
        "am": am,
        "upm": upm,
        "uploader": up,
        "archive_rec": json.loads(am.read_text().strip()),
        "upload_rec": json.loads(upm.read_text().strip()),
    }


# ────────────────────────────────────────────────────────────────────
# Status: ok
# ────────────────────────────────────────────────────────────────────


def test_status_ok_when_everything_matches(tmp_path: Path):
    s = _setup_chain(tmp_path)
    check = rt._verify_archive(s["archive_rec"], s["upload_rec"], s["uploader"])
    assert check.status == "ok"
    assert check.sha1_downloaded == s["upload_rec"]["sha1_remote"]
    assert check.rows_archive == check.rows_source == 200


# ────────────────────────────────────────────────────────────────────
# Status: sha1_mismatch (remote bit-rot)
# ────────────────────────────────────────────────────────────────────


def test_status_sha1_mismatch_when_remote_corrupted(tmp_path: Path):
    s = _setup_chain(tmp_path)
    # Corrupt the remote bytes — append a stray byte
    remote_file = s["uploader"].root / s["upload_rec"]["remote_key"]
    with open(remote_file, "ab") as f:
        f.write(b"BIT_FLIP")

    check = rt._verify_archive(s["archive_rec"], s["upload_rec"], s["uploader"])
    assert check.status == "sha1_mismatch"
    assert check.sha1_downloaded != check.sha1_expected
    assert "sha1" in check.detail


# ────────────────────────────────────────────────────────────────────
# Status: missing_remote
# ────────────────────────────────────────────────────────────────────


def test_status_missing_remote_when_key_absent(tmp_path: Path):
    s = _setup_chain(tmp_path)
    remote_file = s["uploader"].root / s["upload_rec"]["remote_key"]
    remote_file.unlink()  # gone

    check = rt._verify_archive(s["archive_rec"], s["upload_rec"], s["uploader"])
    assert check.status == "missing_remote"
    assert check.sha1_downloaded is None


# ────────────────────────────────────────────────────────────────────
# Status: decode_failed
# ────────────────────────────────────────────────────────────────────


def test_status_decode_failed_when_remote_not_parquet(tmp_path: Path):
    """If the remote's bytes are valid (sha1 matches) but aren't a valid
    Parquet — e.g. someone uploaded random bytes by mistake — surface as
    decode_failed, not as silent success."""
    s = _setup_chain(tmp_path)
    # Replace remote AND the upload manifest's sha1 to match the new content,
    # so we get past the sha1 check and into the decode check.
    remote_file = s["uploader"].root / s["upload_rec"]["remote_key"]
    bogus = b"this is not parquet" * 1000
    remote_file.write_bytes(bogus)
    new_sha1 = hashlib.sha1(bogus).hexdigest()  # noqa: S324
    s["upload_rec"]["sha1_remote"] = new_sha1

    check = rt._verify_archive(s["archive_rec"], s["upload_rec"], s["uploader"])
    assert check.status == "decode_failed"
    assert check.detail


# ────────────────────────────────────────────────────────────────────
# Status: row_count_mismatch
# ────────────────────────────────────────────────────────────────────


def test_status_row_count_mismatch_when_source_changed(tmp_path: Path):
    """If someone modified the original CSV after archiving (shouldn't
    happen, but humans), surface row-count drift."""
    s = _setup_chain(tmp_path, rows=200)
    # Truncate source CSV to half its rows
    df = pd.read_csv(s["source_csv"])
    df.head(100).to_csv(s["source_csv"], index=False)

    check = rt._verify_archive(s["archive_rec"], s["upload_rec"], s["uploader"])
    assert check.status == "row_count_mismatch"
    assert check.rows_archive == 200
    assert check.rows_source == 100


# ────────────────────────────────────────────────────────────────────
# Status: source_rotated_ok (informational)
# ────────────────────────────────────────────────────────────────────


def test_status_source_rotated_ok_when_csv_gone(tmp_path: Path):
    """After the retention window, the source CSV is deleted. The archive
    is still verifiable, just not against the original. This is fine —
    it's the steady state for old data."""
    s = _setup_chain(tmp_path)
    s["source_csv"].unlink()

    check = rt._verify_archive(s["archive_rec"], s["upload_rec"], s["uploader"])
    assert check.status == "source_rotated_ok"
    assert check.rows_source is None
    assert check.rows_archive == 200
    # Critically: this is NOT a failure that triggers the alert.
    assert rt.format_alert([check]) is None


# ────────────────────────────────────────────────────────────────────
# format_alert + run_restore_test
# ────────────────────────────────────────────────────────────────────


def test_format_alert_silent_on_clean(tmp_path: Path):
    s = _setup_chain(tmp_path)
    check = rt._verify_archive(s["archive_rec"], s["upload_rec"], s["uploader"])
    assert check.status == "ok"
    assert rt.format_alert([check]) is None


def test_format_alert_emits_on_any_failure(tmp_path: Path):
    s = _setup_chain(tmp_path)
    (s["uploader"].root / s["upload_rec"]["remote_key"]).unlink()
    check = rt._verify_archive(s["archive_rec"], s["upload_rec"], s["uploader"])
    msg = rt.format_alert([check])
    assert msg is not None
    assert "FAILED" in msg
    assert "missing_remote" in msg
    assert s["upload_rec"]["remote_key"] in msg


def test_format_alert_handles_empty_checks_with_warning(tmp_path: Path):
    msg = rt.format_alert([])
    assert msg and "no checks" in msg


# ────────────────────────────────────────────────────────────────────
# Picker logic
# ────────────────────────────────────────────────────────────────────


def test_run_restore_test_picks_latest_upload_by_default(tmp_path: Path):
    """When no --target-date given, restore-test picks the most recent
    upload. The cron is cheap by design — verify the newest, not all of
    them."""
    # Set up two days, upload both
    s1 = _setup_chain(tmp_path, target_date="2026-04-08", rows=50)
    # Hack: build a second day manually so the upload manifest has 2 entries
    # (re-using _setup_chain would clobber the manifests — refactor the
    # second day into the same archive root).
    src2 = tmp_path / "data" / "chain_snapshots" / "chain_2026-04-15.csv"
    pd.DataFrame({"time": ["x"], "underlying": ["NIFTY"], "expiry": ["x"],
                  "strike": [1.0], "option_type": ["CE"], "ltp": [1.0],
                  "iv": [1.0], "delta": [1.0], "gamma": [1.0], "theta": [1.0],
                  "vega": [1.0], "oi": [1], "volume": [1], "bid_price": [1.0],
                  "ask_price": [1.0]}).to_csv(src2, index=False)
    from datetime import date as _date
    arec2 = atp.archive_chain(
        src2, target_date=_date(2026, 4, 15),
        archive_root=tmp_path / "archive",
        manifest=s1["am"],
    )
    assert arec2 is not None
    # Upload the second one — its uploaded_at will be > the first
    import time
    time.sleep(0.01)  # ensure timestamp ordering
    am_lines = s1["am"].read_text().strip().split("\n")
    arec2_dict = json.loads(am_lines[-1])
    ub.upload_archive(arec2_dict, s1["uploader"], upload_manifest=s1["upm"])

    checks = rt.run_restore_test(
        archive_manifest=s1["am"], upload_manifest=s1["upm"],
        uploader=s1["uploader"],
    )
    # Should have run exactly ONE check, on the latest (2026-04-15)
    assert len(checks) == 1
    assert checks[0].target_date == "2026-04-15"


def test_run_restore_test_target_date_selects_specific(tmp_path: Path):
    s = _setup_chain(tmp_path, target_date="2026-04-15")
    checks = rt.run_restore_test(
        archive_manifest=s["am"], upload_manifest=s["upm"],
        uploader=s["uploader"], only_target_date="2026-04-15",
    )
    assert len(checks) == 1
    assert checks[0].target_date == "2026-04-15"


def test_run_restore_test_empty_manifests_noop(tmp_path: Path):
    am = tmp_path / "manifest.jsonl"
    upm = tmp_path / "manifest_b2.jsonl"
    up = ub.LocalDirUploader(root=tmp_path / "remote")
    checks = rt.run_restore_test(
        archive_manifest=am, upload_manifest=upm, uploader=up,
    )
    assert checks == []


# ────────────────────────────────────────────────────────────────────
# Helper sanity
# ────────────────────────────────────────────────────────────────────


def test_hash_dataframe_rows_is_stable():
    df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    h1 = rt._hash_dataframe_rows(df)
    h2 = rt._hash_dataframe_rows(df)
    assert h1 == h2


def test_hash_dataframe_rows_changes_when_data_changes():
    df1 = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    df2 = pd.DataFrame({"a": [1, 2, 4], "b": ["x", "y", "z"]})  # one cell changed
    assert rt._hash_dataframe_rows(df1) != rt._hash_dataframe_rows(df2)


def test_hash_dataframe_rows_ignores_column_order():
    """Columns get sorted before hashing → reordering shouldn't trigger
    a false mismatch."""
    df1 = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    df2 = pd.DataFrame({"b": ["x", "y"], "a": [1, 2]})  # same data, different col order
    assert rt._hash_dataframe_rows(df1) == rt._hash_dataframe_rows(df2)
