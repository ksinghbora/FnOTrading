"""Archive raw recorder outputs to Parquet+ZSTD for long-term storage.

Why this exists (DATA_RELIABILITY_PLAN §9, cold-storage chain):
    Chain CSVs are the source of truth for replay (DATA_RELIABILITY_PLAN
    §7), but they're verbose: ~9MB/day for one underlying. Over a year that's
    ~1.5GB of raw text — workable on local disk, expensive in cloud cold
    storage. Parquet+ZSTD gives ~5–10x compression with lossless schema
    fidelity and column-pruning at read time.

    Archiving is the FIRST step in the cold-storage chain:
        raw CSV/JSONL → Parquet (this script)
                     → B2 upload (scripts/upload_to_b2.py)
                     → weekly restore-test (scripts/restore_test.py)

    Each archived file gets a line in ``archive/manifest.jsonl`` with:
        - sha256 of the ORIGINAL (so restore-test can verify round-trip)
        - sha256 of the Parquet archive (so we detect bit rot in B2)
        - original / archive byte sizes
        - compression ratio
        - archived_at timestamp (UTC)
        - source kind (chain_csv | log_jsonl)

    The manifest is the source of truth for what's been archived. The
    uploader reads it; restore-test verifies against it.

Idempotence:
    Re-running this script on the same input is safe. If a Parquet already
    exists at the target path AND the manifest has a matching sha256 of
    the original, the file is skipped. If the original has changed since
    the last archive (different sha256), we re-archive — but that's a
    suspicious signal (chain CSVs should be append-only during the
    trading day and immutable thereafter), so we log a WARNING.

Retention:
    This script does NOT delete originals. A separate retention policy is
    applied AFTER the restore-test for a window has passed (so we never
    delete data we haven't proven we can recover).

Usage:
    # Archive all chain CSVs older than 7 days
    uv run python scripts/archive_to_parquet.py --kind chain --min-age-days 7

    # Archive a specific date
    uv run python scripts/archive_to_parquet.py --kind chain --date 2026-04-15

    # Dry run — print what would be archived, don't write anything
    uv run python scripts/archive_to_parquet.py --kind chain --dry-run

Cron: 0 22 * * 1-5  (22:00 IST, after nightly_audit + replay_runs flush)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Iterator, Literal

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)

# Default locations — overridable via CLI flags so tests can use tmp_path.
DEFAULT_CHAIN_DIR = Path("data/chain_snapshots")
DEFAULT_LOG_DIR = Path("data/logs")
DEFAULT_ARCHIVE_ROOT = Path("archive")
DEFAULT_MANIFEST = DEFAULT_ARCHIVE_ROOT / "manifest.jsonl"

# ZSTD level: 3 is the pyarrow default and a good speed/ratio tradeoff.
# Bump to 9–22 if storage matters more than archive wall time.
ZSTD_LEVEL = 3

SourceKind = Literal["chain_csv", "log_jsonl"]


@dataclass(frozen=True)
class ArchiveResult:
    """One line in archive/manifest.jsonl. Frozen so callers can't mutate
    after the fact — the only way to update the manifest is to write a
    fresh line."""

    source_path: str
    archive_path: str
    kind: SourceKind
    target_date: str  # ISO date the archived file represents
    sha256_source: str
    sha256_archive: str
    bytes_source: int
    bytes_archive: int
    ratio: float  # bytes_source / bytes_archive — informational
    rows: int
    archived_at: str  # UTC ISO

    def as_manifest_line(self) -> str:
        """Serialize to a single newline-terminated JSON line."""
        return json.dumps(asdict(self), separators=(",", ":")) + "\n"


def _sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    """Streaming SHA256 — handles multi-GB files without blowing RAM."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _read_manifest(manifest: Path) -> dict[tuple[str, str], dict]:
    """Load the manifest as ``{(kind, target_date): record}`` for idempotence
    lookups. Missing manifest = empty dict (first run)."""
    out: dict[tuple[str, str], dict] = {}
    if not manifest.exists():
        return out
    with open(manifest) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # A truncated line at the very end of an interrupted write.
                # Skip — the caller will re-archive on next run.
                logger.warning("manifest line skipped (malformed JSON): %r", line[:80])
                continue
            key = (rec.get("kind", ""), rec.get("target_date", ""))
            # Last line wins — mirrors how an append-only log resolves.
            out[key] = rec
    return out


def _append_manifest(manifest: Path, result: ArchiveResult) -> None:
    """Append-only write with fsync — same pattern as data/replay_runs.jsonl."""
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest, "a") as f:
        f.write(result.as_manifest_line())
        f.flush()
        import os

        os.fsync(f.fileno())


def _date_from_chain_filename(path: Path) -> date | None:
    """``chain_2026-04-15.csv`` → ``date(2026, 4, 15)``. None if pattern misses."""
    stem = path.stem  # "chain_2026-04-15"
    if not stem.startswith("chain_"):
        return None
    try:
        return date.fromisoformat(stem[len("chain_") :])
    except ValueError:
        return None


def _iter_chain_candidates(
    chain_dir: Path,
    *,
    min_age_days: int | None,
    only_date: date | None,
    today: date | None = None,
) -> Iterator[tuple[Path, date]]:
    """Yield (csv_path, target_date) pairs eligible for archive.

    ``min_age_days`` filters out files that are too recent (we want to be
    sure nothing's still appending). ``only_date`` is the explicit single-day
    mode used by the CLI's ``--date`` flag. ``today`` is overridable for
    tests so we don't have to monkey-patch ``date.today``.
    """
    today = today or date.today()
    if not chain_dir.exists():
        return
    for path in sorted(chain_dir.glob("chain_*.csv")):
        d = _date_from_chain_filename(path)
        if d is None:
            continue
        if only_date is not None:
            if d == only_date:
                yield path, d
            continue
        if min_age_days is not None and (today - d).days < min_age_days:
            continue
        yield path, d


def _archive_chain_csv(
    csv_path: Path,
    archive_root: Path,
    *,
    target_date: date,
) -> tuple[Path, int]:
    """Read CSV → write Parquet+ZSTD. Returns (archive_path, row_count).

    Pandas/pyarrow handle dtype inference automatically and preserve the
    chain CSV's mixed columns (ISO timestamp string, strings, floats, ints).
    Timestamps stay as strings — converting to native parquet timestamps
    adds risk (timezone roundtrip bugs) for marginal storage gain. Replay
    treats the time column as a string anyway.
    """
    import pandas as pd  # local import — pandas startup is slow

    df = pd.read_csv(csv_path)
    archive_dir = archive_root / "chain_snapshots"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"chain_{target_date.isoformat()}.parquet"
    df.to_parquet(
        archive_path,
        engine="pyarrow",
        compression="zstd",
        compression_level=ZSTD_LEVEL,
        index=False,
    )
    return archive_path, len(df)


def archive_chain(
    csv_path: Path,
    *,
    target_date: date,
    archive_root: Path = DEFAULT_ARCHIVE_ROOT,
    manifest: Path = DEFAULT_MANIFEST,
    existing: dict[tuple[str, str], dict] | None = None,
    dry_run: bool = False,
) -> ArchiveResult | None:
    """Archive a single chain CSV. Returns None if skipped (idempotent hit).

    The ``existing`` parameter is the loaded manifest map; pass it in so a
    batch caller can amortize the read cost across many files.
    """
    sha_source = _sha256_file(csv_path)
    bytes_source = csv_path.stat().st_size

    existing = existing if existing is not None else _read_manifest(manifest)
    prior = existing.get(("chain_csv", target_date.isoformat()))
    if prior is not None and prior.get("sha256_source") == sha_source:
        logger.info(
            "skip already-archived chain %s (sha matches manifest)",
            target_date.isoformat(),
        )
        return None
    if prior is not None:
        logger.warning(
            "chain %s sha CHANGED since last archive (was %s, now %s) — "
            "originals should be immutable post-trading-day; re-archiving",
            target_date.isoformat(),
            prior.get("sha256_source", "?")[:12],
            sha_source[:12],
        )

    if dry_run:
        logger.info(
            "[DRY RUN] would archive %s (%d bytes)", csv_path, bytes_source,
        )
        return None

    archive_path, rows = _archive_chain_csv(
        csv_path, archive_root, target_date=target_date,
    )
    sha_archive = _sha256_file(archive_path)
    bytes_archive = archive_path.stat().st_size
    # ratio guard: if compression is somehow > 1.0 (Parquet larger than CSV)
    # for chain data, something is very wrong — log loud but don't block.
    ratio = bytes_source / bytes_archive if bytes_archive else 0.0
    if ratio < 1.5:
        logger.warning(
            "chain %s compressed poorly (ratio=%.2fx) — investigate dtype",
            target_date.isoformat(), ratio,
        )

    result = ArchiveResult(
        source_path=str(csv_path),
        archive_path=str(archive_path),
        kind="chain_csv",
        target_date=target_date.isoformat(),
        sha256_source=sha_source,
        sha256_archive=sha_archive,
        bytes_source=bytes_source,
        bytes_archive=bytes_archive,
        ratio=round(ratio, 3),
        rows=rows,
        archived_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    _append_manifest(manifest, result)
    logger.info(
        "archived chain %s: %d → %d bytes (%.2fx) rows=%d",
        target_date.isoformat(), bytes_source, bytes_archive, ratio, rows,
    )
    return result


def archive_chains(
    *,
    chain_dir: Path = DEFAULT_CHAIN_DIR,
    archive_root: Path = DEFAULT_ARCHIVE_ROOT,
    manifest: Path = DEFAULT_MANIFEST,
    min_age_days: int | None = 7,
    only_date: date | None = None,
    dry_run: bool = False,
    today: date | None = None,
) -> list[ArchiveResult]:
    """Batch driver: archive every eligible chain CSV.

    Returns the list of newly-archived results (skipped/dry-run items
    are NOT in the list — caller distinguishes "archived" from "all
    candidates" by length).
    """
    existing = _read_manifest(manifest)
    out: list[ArchiveResult] = []
    for csv_path, d in _iter_chain_candidates(
        chain_dir,
        min_age_days=min_age_days,
        only_date=only_date,
        today=today,
    ):
        try:
            r = archive_chain(
                csv_path,
                target_date=d,
                archive_root=archive_root,
                manifest=manifest,
                existing=existing,
                dry_run=dry_run,
            )
        except Exception:
            # Don't let one bad file kill the batch — there's typically a
            # week of catchup work and we want every healthy file through.
            logger.exception("archive failed for %s — continuing batch", csv_path)
            continue
        if r is not None:
            out.append(r)
            # Update the in-memory map so subsequent files in the same batch
            # see the freshly-archived record (defends against duplicate work
            # if the loop iterates the same date twice).
            existing[("chain_csv", d.isoformat())] = asdict(r)
    return out


def main():
    parser = argparse.ArgumentParser(
        description="Archive recorder outputs to Parquet+ZSTD",
    )
    parser.add_argument(
        "--kind", choices=["chain"], default="chain",
        help="What to archive (only 'chain' supported today; logs follow-up)",
    )
    parser.add_argument(
        "--chain-dir", type=Path, default=DEFAULT_CHAIN_DIR,
        help=f"Source dir (default {DEFAULT_CHAIN_DIR})",
    )
    parser.add_argument(
        "--archive-root", type=Path, default=DEFAULT_ARCHIVE_ROOT,
        help=f"Archive output root (default {DEFAULT_ARCHIVE_ROOT})",
    )
    parser.add_argument(
        "--manifest", type=Path, default=DEFAULT_MANIFEST,
        help=f"Manifest JSONL path (default {DEFAULT_MANIFEST})",
    )
    parser.add_argument(
        "--min-age-days", type=int, default=7,
        help="Only archive files at least N days old (default 7). Use 0 to "
             "archive everything; --date overrides.",
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="Archive a specific date (YYYY-MM-DD); overrides --min-age-days",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    only_date = date.fromisoformat(args.date) if args.date else None
    results = archive_chains(
        chain_dir=args.chain_dir,
        archive_root=args.archive_root,
        manifest=args.manifest,
        min_age_days=None if only_date else args.min_age_days,
        only_date=only_date,
        dry_run=args.dry_run,
    )
    if results:
        total_src = sum(r.bytes_source for r in results)
        total_arc = sum(r.bytes_archive for r in results)
        ratio = total_src / total_arc if total_arc else 0.0
        print(
            f"Archived {len(results)} files: "
            f"{total_src:,} → {total_arc:,} bytes ({ratio:.2f}x)"
        )
    else:
        print("Nothing to archive (all up to date or no candidates).")


if __name__ == "__main__":
    main()
