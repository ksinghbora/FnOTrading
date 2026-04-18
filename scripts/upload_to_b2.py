"""Push Parquet archives to remote cold storage (Backblaze B2).

Why this exists (DATA_RELIABILITY_PLAN §9, cold-storage chain step 2):
    Local Parquet (scripts/archive_to_parquet.py) is good defense against
    accidental ``rm -rf`` of the source CSVs. It is NOT defense against
    drive failure, machine loss, or geographic disaster. Pushing to a
    different provider (Backblaze B2, S3, etc.) is what completes that
    defense.

    Plan picks B2 for cost (4–5× cheaper than S3 at our access pattern;
    see §11). The uploader is abstracted behind ``RemoteUploader`` though,
    so swapping providers later is one class plus a CLI flag.

Manifest discipline:
    - ``archive/manifest.jsonl``       — archive ledger (this script READS only)
    - ``archive/manifest_b2.jsonl``   — upload ledger (this script WRITES)

    Separating the two keeps the archive-truth ledger immutable: the fact
    that we archived a file at time T should never depend on whether the
    upload at time T+1 worked. Restore-test reads BOTH ledgers when it
    verifies B2 round-trips.

Failure modes we explicitly handle:
    - Missing B2 creds              → fail loud (refuse to silently no-op).
    - SHA1 mismatch from B2         → fail loud + leave manifest unmutated.
    - File already uploaded         → skip (idempotent).
    - Archive sha256 changed since  → re-upload + log warning (very weird).

Local-dir uploader:
    For tests AND for dev environments without B2 creds, ``LocalDirUploader``
    writes to a local directory mimicking the B2 keyspace. Restore-test uses
    the same uploader interface to read back, so the entire chain can be
    exercised end-to-end without external dependencies.

Usage:
    # Real B2 (requires B2_KEY_ID, B2_APP_KEY, B2_BUCKET in env)
    uv run python scripts/upload_to_b2.py

    # Local-dir (dev/test) — writes to ./fake-b2/<key>
    ARCHIVE_REMOTE=local-dir:./fake-b2 uv run python scripts/upload_to_b2.py

    # Single file
    uv run python scripts/upload_to_b2.py --target-date 2026-04-15

    # Dry-run (list what would upload, no transfer)
    uv run python scripts/upload_to_b2.py --dry-run

Cron: 0 23 * * 1-5  (23:00 IST, after archive_to_parquet at 22:00)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Protocol

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.archive_to_parquet import (  # noqa: E402  (after sys.path)
    DEFAULT_ARCHIVE_ROOT,
    DEFAULT_MANIFEST,
    _read_manifest,
)

logger = logging.getLogger(__name__)

DEFAULT_UPLOAD_MANIFEST = DEFAULT_ARCHIVE_ROOT / "manifest_b2.jsonl"


# ────────────────────────────────────────────────────────────────────
# Uploader abstraction
# ────────────────────────────────────────────────────────────────────


class UploadError(Exception):
    """Raised when a remote upload completes but verification fails.

    Distinct from generic exceptions so callers can decide whether to
    retry (upload error) vs surface (programming error)."""


class RemoteUploader(Protocol):
    """Anything that can put a file at a remote key and report its SHA1.

    SHA1 is the natural checksum for B2 (their `x-bz-content-sha1` header).
    For non-B2 backends the implementation just returns the SHA1 it
    computed locally; the call still serves as round-trip verification.

    All three methods (upload, exists, download) are part of the contract
    so :mod:`restore_test` can dispatch by Protocol instead of by isinstance
    — keeps the cold-storage chain provider-agnostic.
    """

    name: str  # e.g. "b2:my-bucket" or "local-dir:./fake-b2"

    def upload(self, local_path: Path, remote_key: str) -> str:
        """Upload ``local_path`` to ``remote_key``. Return the remote-side
        SHA1 hex digest. Raise ``UploadError`` on verification mismatch."""
        ...

    def exists(self, remote_key: str) -> bool:
        """Return True iff a file is already at ``remote_key``. Used by
        ``upload_archive`` to short-circuit idempotent re-runs."""
        ...

    def download(self, remote_key: str, dst: Path) -> None:
        """Download ``remote_key`` to ``dst``. Used by restore-test for
        round-trip verification. Raise ``FileNotFoundError`` if the
        remote key doesn't exist."""
        ...


def _sha1_file(path: Path, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()  # noqa: S324  — B2 uses SHA1 as content checksum
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


@dataclass
class LocalDirUploader:
    """Writes to a local directory mimicking the B2 keyspace.

    Used by tests and by dev environments without B2 creds. Same interface
    as ``B2Uploader`` so callers don't branch on backend type."""

    root: Path

    def __post_init__(self):
        self.name = f"local-dir:{self.root}"
        self.root.mkdir(parents=True, exist_ok=True)

    def upload(self, local_path: Path, remote_key: str) -> str:
        target = self.root / remote_key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(local_path.read_bytes())
        # Verify: re-read from "remote" and compare. Catches the obvious
        # bug where we wrote to the wrong path.
        local_sha = _sha1_file(local_path)
        remote_sha = _sha1_file(target)
        if local_sha != remote_sha:
            raise UploadError(
                f"local-dir round-trip sha1 mismatch: "
                f"local={local_sha} remote={remote_sha}"
            )
        return remote_sha

    def exists(self, remote_key: str) -> bool:
        return (self.root / remote_key).exists()

    def download(self, remote_key: str, dst: Path) -> None:
        src = self.root / remote_key
        if not src.exists():
            raise FileNotFoundError(f"remote key missing: {remote_key}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())


class B2Uploader:
    """Backblaze B2 native API client. Lazy-imports ``b2sdk`` so this
    module is importable without the dep for tests / local-dir runs."""

    def __init__(self, key_id: str, app_key: str, bucket_name: str):
        try:
            from b2sdk.v2 import B2Api, InMemoryAccountInfo  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "b2sdk not installed — `uv add b2sdk` or use ARCHIVE_REMOTE="
                "local-dir:<path> to run without B2"
            ) from e

        info = InMemoryAccountInfo()
        api = B2Api(info)
        api.authorize_account("production", key_id, app_key)
        self._bucket = api.get_bucket_by_name(bucket_name)
        self.name = f"b2:{bucket_name}"

    def upload(self, local_path: Path, remote_key: str) -> str:
        local_sha1 = _sha1_file(local_path)
        info = self._bucket.upload_local_file(
            local_file=str(local_path),
            file_name=remote_key,
            sha1_sum=local_sha1,  # B2 verifies on receipt
        )
        # B2 returns the SHA1 it computed. If it differs from our pre-computed
        # one, the server detected corruption; b2sdk would normally raise but
        # double-check anyway — this is the line that catches silent silent
        # in-flight bit flips.
        if info.content_sha1 != local_sha1:
            raise UploadError(
                f"B2 sha1 mismatch for {remote_key}: "
                f"local={local_sha1} remote={info.content_sha1}"
            )
        return info.content_sha1

    def exists(self, remote_key: str) -> bool:
        # b2sdk's preferred way to check existence without downloading.
        try:
            self._bucket.get_file_info_by_name(remote_key)
            return True
        except Exception:
            return False

    def download(self, remote_key: str, dst: Path) -> None:
        try:
            downloaded = self._bucket.download_file_by_name(remote_key)
        except Exception as e:
            # b2sdk raises FileNotPresent / similar; normalize to stdlib so
            # the restore-test path can `except FileNotFoundError` once.
            raise FileNotFoundError(f"B2 download failed for {remote_key}: {e}") from e
        dst.parent.mkdir(parents=True, exist_ok=True)
        downloaded.save_to(str(dst))


def make_uploader_from_env(env: dict | None = None) -> RemoteUploader:
    """Resolve the uploader from environment variables.

    ``ARCHIVE_REMOTE`` selects the backend:
      - ``b2`` (default)             — needs B2_KEY_ID, B2_APP_KEY, B2_BUCKET
      - ``local-dir:<path>``         — for tests and dev

    Refusing to silently no-op is intentional: a cron that "succeeds" by
    uploading nothing for 6 weeks is exactly the failure mode we're
    building this whole chain to prevent.
    """
    env = env if env is not None else dict(os.environ)
    spec = env.get("ARCHIVE_REMOTE", "b2")
    if spec.startswith("local-dir:"):
        path = Path(spec[len("local-dir:") :])
        return LocalDirUploader(root=path)
    if spec == "b2":
        for key in ("B2_KEY_ID", "B2_APP_KEY", "B2_BUCKET"):
            if not env.get(key):
                raise RuntimeError(
                    f"B2 env var {key} is unset — refusing to silently skip "
                    f"upload. Set it, or use ARCHIVE_REMOTE=local-dir:<path>."
                )
        return B2Uploader(
            key_id=env["B2_KEY_ID"],
            app_key=env["B2_APP_KEY"],
            bucket_name=env["B2_BUCKET"],
        )
    raise RuntimeError(f"unknown ARCHIVE_REMOTE backend: {spec!r}")


# ────────────────────────────────────────────────────────────────────
# Upload manifest
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UploadResult:
    """One line in archive/manifest_b2.jsonl. Mirrors archive ledger
    discipline: append-only, fsync'd, frozen so callers can't mutate."""

    archive_path: str
    remote_backend: str  # e.g. "b2:my-bucket"
    remote_key: str
    sha256_archive: str  # from the archive manifest; lets restore-test cross-check
    sha1_remote: str  # what the remote returned (B2's native checksum)
    bytes_uploaded: int
    uploaded_at: str  # UTC ISO

    def as_manifest_line(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":")) + "\n"


def _read_upload_manifest(manifest: Path) -> dict[tuple[str, str], dict]:
    """``{(remote_backend, remote_key): record}`` for idempotence lookups."""
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
                logger.warning("upload manifest line skipped (malformed): %r", line[:80])
                continue
            key = (rec.get("remote_backend", ""), rec.get("remote_key", ""))
            out[key] = rec
    return out


def _append_upload_manifest(manifest: Path, result: UploadResult) -> None:
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest, "a") as f:
        f.write(result.as_manifest_line())
        f.flush()
        os.fsync(f.fileno())


def _remote_key_for(archive_path: Path) -> str:
    """Map an on-disk archive path to its remote key.

    We mirror the directory structure under the archive root so the remote
    keyspace looks like ``chain_snapshots/chain_2026-04-15.parquet``.
    Stable, predictable, easy to grep on the B2 web UI.
    """
    # Find the part of the path AFTER the archive root.
    # Use the parent dir name + filename — this gives us "chain_snapshots/<file>"
    # without requiring the caller to pass the archive root in.
    return f"{archive_path.parent.name}/{archive_path.name}"


# ────────────────────────────────────────────────────────────────────
# Driver
# ────────────────────────────────────────────────────────────────────


def upload_archive(
    archive_record: dict,
    uploader: RemoteUploader,
    upload_manifest: Path,
    *,
    existing: dict[tuple[str, str], dict] | None = None,
    dry_run: bool = False,
) -> UploadResult | None:
    """Upload one archived file. Returns None if skipped (idempotent hit)."""
    archive_path = Path(archive_record["archive_path"])
    if not archive_path.exists():
        logger.error(
            "archive path missing on disk for date=%s: %s — skip",
            archive_record.get("target_date"), archive_path,
        )
        return None
    sha256_archive = archive_record["sha256_archive"]
    remote_key = _remote_key_for(archive_path)

    existing = (
        existing if existing is not None else _read_upload_manifest(upload_manifest)
    )
    prior = existing.get((uploader.name, remote_key))
    if prior is not None and prior.get("sha256_archive") == sha256_archive:
        logger.info("skip already-uploaded %s (sha256 matches)", remote_key)
        return None
    if prior is not None:
        logger.warning(
            "%s sha256_archive CHANGED since last upload (was %s, now %s) — "
            "re-uploading; investigate why archive bytes shifted",
            remote_key,
            prior.get("sha256_archive", "?")[:12],
            sha256_archive[:12],
        )

    if dry_run:
        logger.info("[DRY RUN] would upload %s → %s", archive_path, remote_key)
        return None

    sha1_remote = uploader.upload(archive_path, remote_key)
    result = UploadResult(
        archive_path=str(archive_path),
        remote_backend=uploader.name,
        remote_key=remote_key,
        sha256_archive=sha256_archive,
        sha1_remote=sha1_remote,
        bytes_uploaded=archive_path.stat().st_size,
        uploaded_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    _append_upload_manifest(upload_manifest, result)
    logger.info("uploaded %s (%d bytes, sha1=%s)", remote_key, result.bytes_uploaded, sha1_remote[:12])
    return result


def upload_pending(
    *,
    archive_manifest: Path = DEFAULT_MANIFEST,
    upload_manifest: Path = DEFAULT_UPLOAD_MANIFEST,
    uploader: RemoteUploader,
    only_target_date: str | None = None,
    dry_run: bool = False,
) -> list[UploadResult]:
    """Walk the archive manifest, upload everything not already uploaded.

    ``only_target_date`` (ISO) restricts the walk to one trading day, used
    by the CLI's ``--target-date`` flag for surgical re-uploads."""
    archive_records = _read_manifest(archive_manifest)
    if not archive_records:
        logger.info("archive manifest empty (%s) — nothing to upload", archive_manifest)
        return []

    upload_existing = _read_upload_manifest(upload_manifest)
    out: list[UploadResult] = []
    # Walk in stable order (sorted by target_date) so logs are easy to
    # reason about across multiple runs.
    for (kind, td), rec in sorted(archive_records.items()):
        if only_target_date is not None and td != only_target_date:
            continue
        try:
            r = upload_archive(
                rec, uploader,
                upload_manifest=upload_manifest,
                existing=upload_existing,
                dry_run=dry_run,
            )
        except Exception:
            logger.exception("upload failed for %s — continuing batch", rec.get("archive_path"))
            continue
        if r is not None:
            out.append(r)
            upload_existing[(uploader.name, r.remote_key)] = asdict(r)
    return out


def main():
    parser = argparse.ArgumentParser(description="Upload archives to remote cold storage")
    parser.add_argument(
        "--archive-manifest", type=Path, default=DEFAULT_MANIFEST,
        help=f"Archive ledger to read (default {DEFAULT_MANIFEST})",
    )
    parser.add_argument(
        "--upload-manifest", type=Path, default=DEFAULT_UPLOAD_MANIFEST,
        help=f"Upload ledger to write (default {DEFAULT_UPLOAD_MANIFEST})",
    )
    parser.add_argument(
        "--target-date", type=str, default=None,
        help="Upload only the entry for this trading date (YYYY-MM-DD)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    uploader = make_uploader_from_env()
    logger.info("uploader: %s", uploader.name)
    results = upload_pending(
        archive_manifest=args.archive_manifest,
        upload_manifest=args.upload_manifest,
        uploader=uploader,
        only_target_date=args.target_date,
        dry_run=args.dry_run,
    )
    if results:
        total = sum(r.bytes_uploaded for r in results)
        print(f"Uploaded {len(results)} files: {total:,} bytes")
    else:
        print("Nothing to upload (all up to date or no candidates).")


if __name__ == "__main__":
    main()
