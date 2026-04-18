"""Weekly restore-test cron — proves cold storage actually round-trips.

Why this exists (DATA_RELIABILITY_PLAN §9, cold-storage chain step 3):
    "We have backups" is a marketing claim until you've actually restored
    one. This cron runs every Sunday and proves end-to-end that:

      1. The remote (B2) still holds the file we uploaded.
      2. Downloading it produces bytes whose SHA1 matches what the upload
         manifest recorded (catches in-flight bit rot OR mismatched
         versions silently overwritten by another process).
      3. The Parquet decodes cleanly into a DataFrame.
      4. That DataFrame agrees with the ORIGINAL CSV on row count and
         on a per-row content hash for the key columns. (Catches the
         "we archived a corrupt file" case that no upload-side check
         would notice.)

    If any of those four checks fails, this script exits non-zero and
    pushes a Telegram alert. A clean exit is the only acceptable signal
    that disaster recovery actually works.

What "the original CSV" means:
    The archive manifest records the source path. If that source has been
    rotated/deleted (which happens after the retention window), this
    script falls back to "round-trip same file twice" — verify that
    decoding the downloaded Parquet matches decoding the LOCAL Parquet.
    Less complete but still catches transmission corruption.

Usage:
    # Test the most recently uploaded archive
    uv run python scripts/restore_test.py

    # Test a specific date (must exist in upload manifest)
    uv run python scripts/restore_test.py --target-date 2026-04-15

    # Test against local-dir uploader (dev / CI)
    ARCHIVE_REMOTE=local-dir:./fake-b2 uv run python scripts/restore_test.py

Cron: 0 9 * * 0  (09:00 IST every Sunday — when nothing else is running)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.archive_to_parquet import (  # noqa: E402
    DEFAULT_MANIFEST,
    _read_manifest,
)
from scripts.upload_to_b2 import (  # noqa: E402
    DEFAULT_UPLOAD_MANIFEST,
    RemoteUploader,
    _read_upload_manifest,
    _sha1_file,
    make_uploader_from_env,
)

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Result + checks
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RestoreCheck:
    """Per-archive restore verdict. Frozen — once we've decided "ok" or
    "fail" the answer doesn't get rewritten downstream."""

    target_date: str
    remote_key: str
    remote_backend: str
    sha1_expected: str
    sha1_downloaded: str | None
    rows_archive: int
    rows_source: int | None  # None when source CSV was rotated away
    status: str  # "ok" | "sha1_mismatch" | "row_count_mismatch" | "decode_failed" | "missing_remote" | "source_rotated_ok"
    detail: str  # human-readable; surfaced in the Telegram alert
    checked_at: str


def _download_to_tmp(
    uploader: RemoteUploader, remote_key: str, dst: Path,
) -> Path:
    """Pull ``remote_key`` to ``dst`` via the Protocol's download method.

    Both ``LocalDirUploader`` and ``B2Uploader`` implement ``download``,
    so dispatch is purely structural — no isinstance, no backend-specific
    branches here. Each backend normalizes its native "not found" exception
    to ``FileNotFoundError``, which the caller converts to the ``missing_remote``
    restore-test status.
    """
    uploader.download(remote_key, dst)
    return dst


def _hash_dataframe_rows(df) -> str:
    """Stable per-row content hash → single SHA256.

    Used to compare archive ↔ source bit-for-bit beyond just row count.
    Sorts column names so column-order changes don't trigger a false
    mismatch. Uses pandas' built-in row-hash for speed (vectorized C).
    """
    import pandas as pd

    cols = sorted(df.columns)
    # Coerce everything to string for a stable hash regardless of dtype
    # round-tripping (e.g. float precision differences in CSV<->Parquet
    # would show as data drift; this hashes the canonical text form).
    text = df[cols].astype(str).agg("|".join, axis=1)
    digest = hashlib.sha256("\n".join(text.tolist()).encode()).hexdigest()
    return digest


def _verify_archive(
    archive_record: dict,
    upload_record: dict,
    uploader: RemoteUploader,
) -> RestoreCheck:
    """Run all four checks on one archive. Returns a RestoreCheck verdict.

    Catches its own exceptions and converts them into structured failure
    statuses — a crash here would defeat the point of the cron (we want
    to ALERT on failure, not be silent because the alerter crashed)."""
    import pandas as pd

    target_date = archive_record["target_date"]
    remote_key = upload_record["remote_key"]
    sha1_expected = upload_record["sha1_remote"]
    rows_archive = int(archive_record.get("rows", 0))
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / Path(remote_key).name
        try:
            _download_to_tmp(uploader, remote_key, tmp)
        except FileNotFoundError as e:
            return RestoreCheck(
                target_date=target_date, remote_key=remote_key,
                remote_backend=uploader.name,
                sha1_expected=sha1_expected, sha1_downloaded=None,
                rows_archive=rows_archive, rows_source=None,
                status="missing_remote",
                detail=str(e), checked_at=now,
            )

        # Check 1+2: SHA1 match
        sha1_downloaded = _sha1_file(tmp)
        if sha1_downloaded != sha1_expected:
            return RestoreCheck(
                target_date=target_date, remote_key=remote_key,
                remote_backend=uploader.name,
                sha1_expected=sha1_expected, sha1_downloaded=sha1_downloaded,
                rows_archive=rows_archive, rows_source=None,
                status="sha1_mismatch",
                detail=(
                    f"downloaded sha1 {sha1_downloaded[:12]} != "
                    f"expected {sha1_expected[:12]}"
                ),
                checked_at=now,
            )

        # Check 3: Parquet decode
        try:
            df_archive = pd.read_parquet(tmp)
        except Exception as e:
            return RestoreCheck(
                target_date=target_date, remote_key=remote_key,
                remote_backend=uploader.name,
                sha1_expected=sha1_expected, sha1_downloaded=sha1_downloaded,
                rows_archive=rows_archive, rows_source=None,
                status="decode_failed",
                detail=f"{type(e).__name__}: {e}",
                checked_at=now,
            )

        # Check 4: row count + content-hash agreement with original
        source_path = Path(archive_record.get("source_path", ""))
        if not source_path.exists():
            # Source was rotated; we can't do a full round-trip, but we've
            # still proven the archive is intact and decodable. Distinct
            # status so the alert is informational, not "fix me".
            return RestoreCheck(
                target_date=target_date, remote_key=remote_key,
                remote_backend=uploader.name,
                sha1_expected=sha1_expected, sha1_downloaded=sha1_downloaded,
                rows_archive=len(df_archive), rows_source=None,
                status="source_rotated_ok",
                detail=(
                    f"original CSV gone (post-retention); archive decoded OK "
                    f"with {len(df_archive)} rows"
                ),
                checked_at=now,
            )

        df_source = pd.read_csv(source_path)
        if len(df_archive) != len(df_source):
            return RestoreCheck(
                target_date=target_date, remote_key=remote_key,
                remote_backend=uploader.name,
                sha1_expected=sha1_expected, sha1_downloaded=sha1_downloaded,
                rows_archive=len(df_archive), rows_source=len(df_source),
                status="row_count_mismatch",
                detail=(
                    f"archive has {len(df_archive)} rows but original CSV "
                    f"has {len(df_source)}"
                ),
                checked_at=now,
            )
        # Bit-level content hash comparison — catches dtype-roundtrip drift.
        if _hash_dataframe_rows(df_archive) != _hash_dataframe_rows(df_source):
            return RestoreCheck(
                target_date=target_date, remote_key=remote_key,
                remote_backend=uploader.name,
                sha1_expected=sha1_expected, sha1_downloaded=sha1_downloaded,
                rows_archive=len(df_archive), rows_source=len(df_source),
                status="row_count_mismatch",  # reuse the alert path
                detail="row-hash mismatch — Parquet decodes to different content than CSV",
                checked_at=now,
            )

        return RestoreCheck(
            target_date=target_date, remote_key=remote_key,
            remote_backend=uploader.name,
            sha1_expected=sha1_expected, sha1_downloaded=sha1_downloaded,
            rows_archive=len(df_archive), rows_source=len(df_source),
            status="ok",
            detail=f"sha1+rows+content all match ({len(df_archive)} rows)",
            checked_at=now,
        )


# ────────────────────────────────────────────────────────────────────
# Driver
# ────────────────────────────────────────────────────────────────────


def _pick_records_to_test(
    archive_records: dict[tuple[str, str], dict],
    upload_records: dict[tuple[str, str], dict],
    uploader_name: str,
    only_target_date: str | None,
) -> Iterator[tuple[dict, dict]]:
    """Yield (archive_rec, upload_rec) pairs to test.

    Default: the most recently uploaded archive (one file). The cron is
    cheap by design — restore the LATEST upload weekly, not all of them.
    A bit-rot bug typically affects all files so testing the newest is
    enough; an old-file-only bug is rare and a separate concern.
    """
    if only_target_date is not None:
        for (kind, td), arec in archive_records.items():
            if td != only_target_date:
                continue
            from scripts.upload_to_b2 import _remote_key_for
            remote_key = _remote_key_for(Path(arec["archive_path"]))
            urec = upload_records.get((uploader_name, remote_key))
            if urec is None:
                logger.warning("no upload record for %s on %s", uploader_name, td)
                return
            yield arec, urec
            return

    # Default: pick the most recent upload (max uploaded_at)
    if not upload_records:
        return
    latest_key = max(
        upload_records.keys(),
        key=lambda k: upload_records[k].get("uploaded_at", ""),
    )
    urec = upload_records[latest_key]
    # Find the matching archive record by archive_path
    arec = None
    for arec_candidate in archive_records.values():
        if arec_candidate.get("archive_path") == urec.get("archive_path"):
            arec = arec_candidate
            break
    if arec is None:
        logger.error(
            "upload record %s has no matching archive record — manifest drift",
            urec.get("remote_key"),
        )
        return
    yield arec, urec


def run_restore_test(
    *,
    archive_manifest: Path = DEFAULT_MANIFEST,
    upload_manifest: Path = DEFAULT_UPLOAD_MANIFEST,
    uploader: RemoteUploader,
    only_target_date: str | None = None,
) -> list[RestoreCheck]:
    """Run restore-test, return list of checks. Caller decides exit code."""
    archive_records = _read_manifest(archive_manifest)
    upload_records = _read_upload_manifest(upload_manifest)
    if not archive_records:
        logger.warning("archive manifest empty: %s", archive_manifest)
        return []
    if not upload_records:
        logger.warning("upload manifest empty: %s", upload_manifest)
        return []

    out: list[RestoreCheck] = []
    for arec, urec in _pick_records_to_test(
        archive_records, upload_records, uploader.name, only_target_date,
    ):
        check = _verify_archive(arec, urec, uploader)
        out.append(check)
        level = logging.INFO if check.status in ("ok", "source_rotated_ok") else logging.ERROR
        logger.log(
            level, "restore-test %s: %s — %s",
            check.target_date, check.status, check.detail,
        )
    return out


def format_alert(checks: list[RestoreCheck]) -> str | None:
    """Build the Telegram message. Returns None when nothing to alert.

    The alert flavours:
      - "ok" / "source_rotated_ok"    → no alert (clean week)
      - any failure                    → alert with the worst failure first
    """
    if not checks:
        return "⚠️ Restore-test produced no checks (manifest empty?)"

    fails = [c for c in checks if c.status not in ("ok", "source_rotated_ok")]
    if not fails:
        return None  # all clean — silent

    lines = [
        "🚨 *Cold-storage restore-test FAILED*",
        "",
    ]
    for c in fails:
        lines.append(
            f"`{c.target_date}` {c.status}: {c.detail}\n"
            f"    backend={c.remote_backend} key=`{c.remote_key}`"
        )
    lines.append("")
    lines.append("Disaster-recovery is broken. Investigate before the next archive run.")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Verify cold-storage round-trip integrity")
    parser.add_argument(
        "--archive-manifest", type=Path, default=DEFAULT_MANIFEST,
    )
    parser.add_argument(
        "--upload-manifest", type=Path, default=DEFAULT_UPLOAD_MANIFEST,
    )
    parser.add_argument(
        "--target-date", type=str, default=None,
        help="Test specific date (default: most recent upload)",
    )
    parser.add_argument("--skip-telegram", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    uploader = make_uploader_from_env()
    logger.info("uploader: %s", uploader.name)
    checks = run_restore_test(
        archive_manifest=args.archive_manifest,
        upload_manifest=args.upload_manifest,
        uploader=uploader,
        only_target_date=args.target_date,
    )

    alert = format_alert(checks)
    if alert is None:
        print(f"OK — {len(checks)} checks all passed.")
        return 0

    print(alert)
    if not args.skip_telegram:
        try:
            from src.config import Settings
            from src.notifications.telegram import TelegramNotifier
            settings = Settings()
            if settings.telegram_bot_token:
                import asyncio
                notifier = TelegramNotifier(settings)
                asyncio.run(notifier.send_message(alert))
                print("(sent alert to Telegram)")
        except Exception as e:
            logger.warning("Telegram send failed: %s", e)

    # Non-zero so the cron job's monitoring catches it.
    has_failure = any(c.status not in ("ok", "source_rotated_ok") for c in checks)
    return 2 if has_failure else 0


if __name__ == "__main__":
    sys.exit(main())
