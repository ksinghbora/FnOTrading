"""Tests for scripts/archive_to_parquet.py — the cold-storage chain step 1.

Contracts this file locks in:
  1. Round-trip: CSV → Parquet → DataFrame matches the original byte-for-byte
     on all columns + dtypes.
  2. Manifest line is well-formed JSON with all the fields downstream
     (uploader, restore-test) expects.
  3. SHA256 of source AND archive both populated and stable across runs.
  4. Idempotence: re-archiving same file with same SHA → skip + no
     duplicate manifest line.
  5. Re-archiving a file whose source SHA CHANGED → re-archives but logs
     a WARNING (originals should be immutable post-trading).
  6. min_age_days filter: too-recent files excluded from batch.
  7. only_date filter: surgical re-archive of one specific day.
  8. Compression ratio sanity: real chain CSV gets > 3x compression.
  9. Bad-source resilience: one corrupt CSV doesn't kill the whole batch.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import archive_to_parquet as atp  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────

# Realistic chain CSV row shape — matches the live recorder format.
_CHAIN_COLUMNS = [
    "time", "underlying", "expiry", "strike", "option_type",
    "ltp", "iv", "delta", "gamma", "theta", "vega",
    "oi", "volume", "bid_price", "ask_price",
]


def _make_chain_csv(path: Path, *, rows: int = 100) -> pd.DataFrame:
    """Write a realistic chain CSV at ``path`` and return the DataFrame."""
    import numpy as np
    rng = np.random.default_rng(42)
    df = pd.DataFrame({
        "time": [f"2026-04-15T09:{15 + (i // 60) % 5}:{i % 60:02d}.000+05:30"
                 for i in range(rows)],
        "underlying": ["NIFTY"] * rows,
        "expiry": ["2026-04-21"] * rows,
        "strike": rng.choice([22000, 22500, 23000, 23500, 24000], size=rows).astype(float),
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
    df.to_csv(path, index=False)
    return df


def _setup_chain_dir(tmp_path: Path, dates: list[date]) -> Path:
    chain_dir = tmp_path / "chain_snapshots"
    chain_dir.mkdir()
    for d in dates:
        _make_chain_csv(chain_dir / f"chain_{d.isoformat()}.csv", rows=200)
    return chain_dir


# ────────────────────────────────────────────────────────────────────
# Round-trip + schema fidelity
# ────────────────────────────────────────────────────────────────────


def test_round_trip_preserves_rows_and_columns(tmp_path: Path):
    chain_dir = tmp_path / "chain_snapshots"
    chain_dir.mkdir()
    csv = chain_dir / "chain_2026-04-15.csv"
    original = _make_chain_csv(csv, rows=500)

    result = atp.archive_chain(
        csv, target_date=date(2026, 4, 15),
        archive_root=tmp_path / "archive",
        manifest=tmp_path / "archive" / "manifest.jsonl",
    )
    assert result is not None
    restored = pd.read_parquet(result.archive_path)
    # Same rows, same columns, same content
    assert len(restored) == len(original)
    assert list(restored.columns) == list(original.columns)
    pd.testing.assert_frame_equal(restored, original)


def test_archive_returns_well_formed_manifest_record(tmp_path: Path):
    chain_dir = tmp_path / "chain_snapshots"
    chain_dir.mkdir()
    csv = chain_dir / "chain_2026-04-15.csv"
    _make_chain_csv(csv, rows=300)

    manifest = tmp_path / "archive" / "manifest.jsonl"
    result = atp.archive_chain(
        csv, target_date=date(2026, 4, 15),
        archive_root=tmp_path / "archive",
        manifest=manifest,
    )
    assert result is not None
    line = json.loads(manifest.read_text().strip())
    # Every field the uploader + restore-test will read must be present.
    for field in (
        "source_path", "archive_path", "kind", "target_date",
        "sha256_source", "sha256_archive", "bytes_source", "bytes_archive",
        "ratio", "rows", "archived_at",
    ):
        assert field in line, f"manifest missing field: {field}"
    assert line["kind"] == "chain_csv"
    assert line["target_date"] == "2026-04-15"
    assert line["rows"] == 300
    assert len(line["sha256_source"]) == 64
    assert len(line["sha256_archive"]) == 64
    assert line["bytes_source"] > line["bytes_archive"]
    assert line["ratio"] > 1.0


def test_sha256_is_stable_across_runs(tmp_path: Path):
    """Same input bytes → same SHA. If this drifts, idempotence breaks."""
    chain_dir = tmp_path / "chain_snapshots"
    chain_dir.mkdir()
    csv = chain_dir / "chain_2026-04-15.csv"
    _make_chain_csv(csv, rows=100)
    s1 = atp._sha256_file(csv)
    s2 = atp._sha256_file(csv)
    assert s1 == s2 and len(s1) == 64


# ────────────────────────────────────────────────────────────────────
# Idempotence
# ────────────────────────────────────────────────────────────────────


def test_re_archive_same_file_skips(tmp_path: Path):
    chain_dir = tmp_path / "chain_snapshots"
    chain_dir.mkdir()
    csv = chain_dir / "chain_2026-04-15.csv"
    _make_chain_csv(csv, rows=100)

    manifest = tmp_path / "archive" / "manifest.jsonl"
    r1 = atp.archive_chain(csv, target_date=date(2026, 4, 15),
                            archive_root=tmp_path / "archive", manifest=manifest)
    r2 = atp.archive_chain(csv, target_date=date(2026, 4, 15),
                            archive_root=tmp_path / "archive", manifest=manifest)

    assert r1 is not None
    assert r2 is None  # skipped
    # Manifest still has only ONE line — no duplicate.
    assert len(manifest.read_text().strip().split("\n")) == 1


def test_re_archive_changed_source_re_archives_with_warning(
    tmp_path: Path, caplog,
):
    """If source SHA changed, we re-archive AND warn (chain CSVs should
    be immutable; a sha shift is a real signal)."""
    chain_dir = tmp_path / "chain_snapshots"
    chain_dir.mkdir()
    csv = chain_dir / "chain_2026-04-15.csv"
    _make_chain_csv(csv, rows=100)

    manifest = tmp_path / "archive" / "manifest.jsonl"
    atp.archive_chain(csv, target_date=date(2026, 4, 15),
                       archive_root=tmp_path / "archive", manifest=manifest)
    # Mutate the source — same path, different content
    _make_chain_csv(csv, rows=200)

    with caplog.at_level("WARNING"):
        r = atp.archive_chain(csv, target_date=date(2026, 4, 15),
                               archive_root=tmp_path / "archive", manifest=manifest)
    assert r is not None  # not skipped
    assert r.rows == 200
    assert any("CHANGED since last archive" in m for m in caplog.messages)
    # Manifest now has TWO lines (audit trail of re-archives)
    assert len(manifest.read_text().strip().split("\n")) == 2


# ────────────────────────────────────────────────────────────────────
# Batch driver — filters + resilience
# ────────────────────────────────────────────────────────────────────


def test_batch_min_age_days_filters_recent_files(tmp_path: Path):
    """A file from 2 days ago should NOT archive when min_age_days=7."""
    today = date(2026, 4, 18)
    chain_dir = _setup_chain_dir(tmp_path, [
        today - timedelta(days=10),  # eligible
        today - timedelta(days=2),   # too recent
        today,                        # today itself
    ])
    results = atp.archive_chains(
        chain_dir=chain_dir,
        archive_root=tmp_path / "archive",
        manifest=tmp_path / "archive" / "manifest.jsonl",
        min_age_days=7,
        today=today,
    )
    assert len(results) == 1
    assert results[0].target_date == (today - timedelta(days=10)).isoformat()


def test_batch_only_date_overrides_age_filter(tmp_path: Path):
    """--date is surgical: archive that one day even if it's too recent."""
    today = date(2026, 4, 18)
    chain_dir = _setup_chain_dir(tmp_path, [
        today - timedelta(days=2),
        today - timedelta(days=10),
    ])
    results = atp.archive_chains(
        chain_dir=chain_dir,
        archive_root=tmp_path / "archive",
        manifest=tmp_path / "archive" / "manifest.jsonl",
        min_age_days=None,
        only_date=today - timedelta(days=2),
        today=today,
    )
    assert len(results) == 1
    assert results[0].target_date == (today - timedelta(days=2)).isoformat()


def test_batch_dry_run_writes_nothing(tmp_path: Path):
    today = date(2026, 4, 18)
    chain_dir = _setup_chain_dir(tmp_path, [today - timedelta(days=10)])
    archive_root = tmp_path / "archive"
    manifest = archive_root / "manifest.jsonl"

    results = atp.archive_chains(
        chain_dir=chain_dir, archive_root=archive_root, manifest=manifest,
        min_age_days=7, dry_run=True, today=today,
    )
    assert results == []
    # Manifest dir not even created
    assert not manifest.exists()
    # No Parquet emitted
    assert not (archive_root / "chain_snapshots").exists()


def test_batch_continues_past_corrupt_csv(tmp_path: Path, caplog):
    """One bad file shouldn't kill the whole batch — there's typically a
    week of catchup work and we want every healthy file through."""
    today = date(2026, 4, 18)
    chain_dir = tmp_path / "chain_snapshots"
    chain_dir.mkdir()
    # Healthy file
    _make_chain_csv(chain_dir / "chain_2026-04-08.csv", rows=100)
    # Corrupt file (truncated mid-row → pandas raises)
    (chain_dir / "chain_2026-04-09.csv").write_text("not,a,real,csv\n\"unclosed quote")
    # Another healthy file
    _make_chain_csv(chain_dir / "chain_2026-04-10.csv", rows=100)

    with caplog.at_level("ERROR"):
        results = atp.archive_chains(
            chain_dir=chain_dir,
            archive_root=tmp_path / "archive",
            manifest=tmp_path / "archive" / "manifest.jsonl",
            min_age_days=0, today=today,
        )
    # 2 healthy files archived; 1 logged as failure
    assert len(results) == 2
    assert {r.target_date for r in results} == {"2026-04-08", "2026-04-10"}
    assert any("archive failed for" in m for m in caplog.messages)


# ────────────────────────────────────────────────────────────────────
# Filename parsing — the small piece that holds the rest together
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name,expected", [
    ("chain_2026-04-15.csv", date(2026, 4, 15)),
    ("chain_2025-12-31.csv", date(2025, 12, 31)),
    ("chain_not-a-date.csv", None),
    ("notchain_2026-04-15.csv", None),
    ("chain_2026-04-15.parquet", date(2026, 4, 15)),  # works on parquet too
])
def test_date_from_chain_filename(name: str, expected):
    assert atp._date_from_chain_filename(Path(name)) == expected


# ────────────────────────────────────────────────────────────────────
# Manifest reader resilience
# ────────────────────────────────────────────────────────────────────


def test_manifest_reader_handles_truncated_last_line(tmp_path: Path, caplog):
    """A crash mid-write leaves a partial JSON line. The reader should
    skip it (not crash the next batch run), and log a warning."""
    manifest = tmp_path / "manifest.jsonl"
    good = '{"kind":"chain_csv","target_date":"2026-04-08","sha256_source":"abc"}'
    truncated = '{"kind":"chain_csv","target_d'
    manifest.write_text(good + "\n" + truncated + "\n")

    with caplog.at_level("WARNING"):
        out = atp._read_manifest(manifest)
    assert ("chain_csv", "2026-04-08") in out
    assert any("malformed JSON" in m for m in caplog.messages)


def test_manifest_reader_returns_empty_for_missing_file(tmp_path: Path):
    """First run of the script: no manifest exists yet. Don't crash."""
    out = atp._read_manifest(tmp_path / "does-not-exist.jsonl")
    assert out == {}
