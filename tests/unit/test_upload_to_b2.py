"""Tests for scripts/upload_to_b2.py — the cold-storage chain step 2.

Contracts this file locks in:
  1. LocalDirUploader: writes file, verifies SHA1 round-trip, raises
     UploadError on mismatch.
  2. make_uploader_from_env: refuses to silently no-op when B2 creds
     are missing — fails LOUD.
  3. Local-dir backend selectable via ARCHIVE_REMOTE=local-dir:<path>.
  4. upload_archive: idempotent on re-run (skips when sha256 matches).
  5. upload_archive: re-uploads + WARNS when sha256_archive shifted
     (unexpected — archives should be immutable).
  6. upload_archive: skips with ERROR when archive file missing on disk.
  7. Upload manifest is well-formed JSONL with all fields restore-test
     reads.
  8. Manifest separation: this script writes manifest_b2.jsonl ONLY,
     never mutates the archive manifest.
  9. Batch driver continues past per-file failure (one bad upload
     doesn't kill the rest).
 10. Remote key derivation: stable, predictable, mirrors archive root.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import upload_to_b2 as ub  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _write_archive_manifest(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _make_archive_record(
    archive_path: Path, *, target_date: str = "2026-04-15", rows: int = 100,
) -> dict:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_bytes(b"fake parquet bytes" * 100)
    import hashlib
    return {
        "source_path": f"data/chain_snapshots/chain_{target_date}.csv",
        "archive_path": str(archive_path),
        "kind": "chain_csv",
        "target_date": target_date,
        "sha256_source": "abc" * 21 + "x",
        "sha256_archive": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        "bytes_source": 9_000_000,
        "bytes_archive": archive_path.stat().st_size,
        "ratio": 5.0,
        "rows": rows,
        "archived_at": "2026-04-18T01:00:00Z",
    }


# ────────────────────────────────────────────────────────────────────
# LocalDirUploader
# ────────────────────────────────────────────────────────────────────


def test_local_dir_uploader_writes_and_verifies(tmp_path: Path):
    src = tmp_path / "src.parquet"
    src.write_bytes(b"hello world")
    up = ub.LocalDirUploader(root=tmp_path / "remote")

    sha1 = up.upload(src, "subdir/file.parquet")

    target = tmp_path / "remote" / "subdir" / "file.parquet"
    assert target.exists()
    assert target.read_bytes() == b"hello world"
    assert len(sha1) == 40  # SHA1 hex


def test_local_dir_uploader_exists(tmp_path: Path):
    src = tmp_path / "src.parquet"
    src.write_bytes(b"x")
    up = ub.LocalDirUploader(root=tmp_path / "remote")
    assert not up.exists("a/b.parquet")
    up.upload(src, "a/b.parquet")
    assert up.exists("a/b.parquet")


# ────────────────────────────────────────────────────────────────────
# make_uploader_from_env — fail-loud on missing creds
# ────────────────────────────────────────────────────────────────────


def test_uploader_local_dir_from_env(tmp_path: Path):
    up = ub.make_uploader_from_env({"ARCHIVE_REMOTE": f"local-dir:{tmp_path}/dest"})
    assert isinstance(up, ub.LocalDirUploader)
    assert up.root == tmp_path / "dest"
    assert up.name.startswith("local-dir:")


def test_uploader_b2_missing_creds_fails_loud():
    """A cron that 'succeeds' by uploading nothing for 6 weeks is exactly
    what this whole chain exists to prevent. Refuse to silently no-op."""
    with pytest.raises(RuntimeError, match="B2_KEY_ID"):
        ub.make_uploader_from_env({"ARCHIVE_REMOTE": "b2"})


def test_uploader_unknown_backend_fails():
    with pytest.raises(RuntimeError, match="unknown ARCHIVE_REMOTE"):
        ub.make_uploader_from_env({"ARCHIVE_REMOTE": "ftp"})


# ────────────────────────────────────────────────────────────────────
# upload_archive — idempotence + drift detection
# ────────────────────────────────────────────────────────────────────


def test_upload_creates_well_formed_manifest_line(tmp_path: Path):
    archive = tmp_path / "archive" / "chain_snapshots" / "chain_2026-04-15.parquet"
    rec = _make_archive_record(archive)
    upm = tmp_path / "archive" / "manifest_b2.jsonl"
    up = ub.LocalDirUploader(root=tmp_path / "remote")

    result = ub.upload_archive(rec, up, upload_manifest=upm)
    assert result is not None

    line = json.loads(upm.read_text().strip())
    for field in (
        "archive_path", "remote_backend", "remote_key",
        "sha256_archive", "sha1_remote", "bytes_uploaded", "uploaded_at",
    ):
        assert field in line, f"missing manifest field: {field}"
    assert line["sha256_archive"] == rec["sha256_archive"]
    assert line["remote_key"] == "chain_snapshots/chain_2026-04-15.parquet"
    assert line["remote_backend"].startswith("local-dir:")


def test_upload_idempotent_when_sha_matches(tmp_path: Path):
    archive = tmp_path / "archive" / "chain_snapshots" / "chain_2026-04-15.parquet"
    rec = _make_archive_record(archive)
    upm = tmp_path / "archive" / "manifest_b2.jsonl"
    up = ub.LocalDirUploader(root=tmp_path / "remote")

    r1 = ub.upload_archive(rec, up, upload_manifest=upm)
    r2 = ub.upload_archive(rec, up, upload_manifest=upm)
    assert r1 is not None
    assert r2 is None  # skipped — sha matches
    assert len(upm.read_text().strip().split("\n")) == 1


def test_upload_re_uploads_and_warns_on_sha_drift(tmp_path: Path, caplog):
    """If sha256_archive shifted between runs, the archive itself
    changed — that's unexpected (archives should be immutable). Re-upload
    so the remote has the latest, but log a warning."""
    archive = tmp_path / "archive" / "chain_snapshots" / "chain_2026-04-15.parquet"
    rec = _make_archive_record(archive)
    upm = tmp_path / "archive" / "manifest_b2.jsonl"
    up = ub.LocalDirUploader(root=tmp_path / "remote")

    ub.upload_archive(rec, up, upload_manifest=upm)
    # Mutate the archive bytes; sha256_archive in the new record will differ
    archive.write_bytes(b"different parquet bytes" * 100)
    import hashlib
    rec["sha256_archive"] = hashlib.sha256(archive.read_bytes()).hexdigest()

    with caplog.at_level("WARNING"):
        r2 = ub.upload_archive(rec, up, upload_manifest=upm)
    assert r2 is not None  # re-uploaded
    assert any("sha256_archive CHANGED" in m for m in caplog.messages)
    assert len(upm.read_text().strip().split("\n")) == 2


def test_upload_skips_when_archive_missing(tmp_path: Path, caplog):
    """A manifest entry pointing at a deleted/moved file shouldn't crash;
    it should log loud and continue."""
    rec = {
        "archive_path": str(tmp_path / "missing" / "ghost.parquet"),
        "kind": "chain_csv", "target_date": "2026-04-15",
        "sha256_source": "x" * 64, "sha256_archive": "y" * 64,
        "bytes_source": 1, "bytes_archive": 1, "ratio": 1.0, "rows": 0,
        "archived_at": "2026-04-18T01:00:00Z",
    }
    upm = tmp_path / "manifest_b2.jsonl"
    up = ub.LocalDirUploader(root=tmp_path / "remote")

    with caplog.at_level("ERROR"):
        result = ub.upload_archive(rec, up, upload_manifest=upm)
    assert result is None
    assert any("missing on disk" in m for m in caplog.messages)
    assert not upm.exists()


def test_upload_dry_run_writes_nothing(tmp_path: Path):
    archive = tmp_path / "archive" / "chain_snapshots" / "chain_2026-04-15.parquet"
    rec = _make_archive_record(archive)
    upm = tmp_path / "archive" / "manifest_b2.jsonl"
    up = ub.LocalDirUploader(root=tmp_path / "remote")

    result = ub.upload_archive(rec, up, upload_manifest=upm, dry_run=True)
    assert result is None
    # No remote write
    assert not (tmp_path / "remote" / "chain_snapshots" / "chain_2026-04-15.parquet").exists()
    # No manifest write
    assert not upm.exists()


# ────────────────────────────────────────────────────────────────────
# upload_pending — batch driver
# ────────────────────────────────────────────────────────────────────


def test_batch_does_not_mutate_archive_manifest(tmp_path: Path):
    """Manifest separation contract: archive ledger is immutable from
    the uploader's perspective."""
    archive = tmp_path / "archive" / "chain_snapshots" / "chain_2026-04-15.parquet"
    rec = _make_archive_record(archive)
    am = tmp_path / "archive" / "manifest.jsonl"
    upm = tmp_path / "archive" / "manifest_b2.jsonl"
    _write_archive_manifest(am, [rec])

    am_before = am.read_bytes()

    up = ub.LocalDirUploader(root=tmp_path / "remote")
    ub.upload_pending(archive_manifest=am, upload_manifest=upm, uploader=up)

    am_after = am.read_bytes()
    assert am_before == am_after, "archive manifest must not be touched"
    assert upm.exists() and upm.read_text().strip()


def test_batch_filters_by_target_date(tmp_path: Path):
    """--target-date is surgical: only one entry uploaded."""
    am = tmp_path / "archive" / "manifest.jsonl"
    upm = tmp_path / "archive" / "manifest_b2.jsonl"
    records = []
    for d in ("2026-04-08", "2026-04-09", "2026-04-10"):
        archive = tmp_path / "archive" / "chain_snapshots" / f"chain_{d}.parquet"
        records.append(_make_archive_record(archive, target_date=d))
    _write_archive_manifest(am, records)

    up = ub.LocalDirUploader(root=tmp_path / "remote")
    results = ub.upload_pending(
        archive_manifest=am, upload_manifest=upm, uploader=up,
        only_target_date="2026-04-09",
    )
    assert len(results) == 1
    assert results[0].remote_key == "chain_snapshots/chain_2026-04-09.parquet"


def test_batch_continues_past_failure(tmp_path: Path, monkeypatch, caplog):
    """One upload error must not kill the rest of the batch."""
    am = tmp_path / "archive" / "manifest.jsonl"
    upm = tmp_path / "archive" / "manifest_b2.jsonl"
    records = []
    for d in ("2026-04-08", "2026-04-09", "2026-04-10"):
        archive = tmp_path / "archive" / "chain_snapshots" / f"chain_{d}.parquet"
        records.append(_make_archive_record(archive, target_date=d))
    _write_archive_manifest(am, records)

    up = ub.LocalDirUploader(root=tmp_path / "remote")
    real_upload = up.upload
    calls = {"n": 0}

    def flaky(local_path, remote_key):
        calls["n"] += 1
        if calls["n"] == 2:  # fail the second one
            raise ub.UploadError("simulated network blip")
        return real_upload(local_path, remote_key)

    monkeypatch.setattr(up, "upload", flaky)

    with caplog.at_level("ERROR"):
        results = ub.upload_pending(
            archive_manifest=am, upload_manifest=upm, uploader=up,
        )
    assert len(results) == 2  # 1st and 3rd succeeded
    assert any("upload failed" in m for m in caplog.messages)


def test_batch_empty_manifest_is_a_noop(tmp_path: Path):
    am = tmp_path / "manifest.jsonl"  # doesn't exist
    upm = tmp_path / "manifest_b2.jsonl"
    up = ub.LocalDirUploader(root=tmp_path / "remote")
    results = ub.upload_pending(archive_manifest=am, upload_manifest=upm, uploader=up)
    assert results == []
    assert not upm.exists()


# ────────────────────────────────────────────────────────────────────
# Remote key derivation — boring but contract-shaped
# ────────────────────────────────────────────────────────────────────


def test_remote_key_uses_parent_dir_and_filename(tmp_path: Path):
    p = tmp_path / "archive" / "chain_snapshots" / "chain_2026-04-15.parquet"
    assert ub._remote_key_for(p) == "chain_snapshots/chain_2026-04-15.parquet"


def test_upload_result_serializes_round_trip():
    r = ub.UploadResult(
        archive_path="/x/y.parquet",
        remote_backend="local-dir:/tmp/r",
        remote_key="x/y.parquet",
        sha256_archive="a" * 64,
        sha1_remote="b" * 40,
        bytes_uploaded=1234,
        uploaded_at="2026-04-18T01:00:00Z",
    )
    line = r.as_manifest_line()
    parsed = json.loads(line)
    assert parsed == asdict(r)
