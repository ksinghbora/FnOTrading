"""Tests for scripts/snapshot_config.py — daily config snapshot for replay.

Contracts the snapshotter must hold:
  1. Secret fields are redacted in env.yaml; the SHA digest changes when
     the secret rotates so reviewers can audit rotation.
  2. params.yaml contains every Pydantic params class import-time can find.
  3. constants.json captures lot sizes, charges, market hours, expiry sched.
  4. holidays.json carries the trading-day calendar the system sees today.
  5. manifest.json includes SHA256 of every written file + git SHA + version.
  6. Atomic JSON write: temp + rename leaves no .tmp file behind.
  7. Snapshotter is offline-safe via --no-instruments.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

import pytest

# Make scripts/ importable (it isn't a package)
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import snapshot_config as sc


@pytest.mark.asyncio
async def test_snapshot_writes_expected_files(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sc, "SNAPSHOT_ROOT", tmp_path)
    target = date(2026, 4, 17)
    out = await sc.snapshot(target, with_instruments=False)
    assert out == tmp_path / "2026-04-17"

    expected = ["params.yaml", "env.yaml", "constants.json", "holidays.json", "manifest.json"]
    for name in expected:
        assert (out / name).exists(), f"{name} missing from snapshot"


@pytest.mark.asyncio
async def test_manifest_includes_sha256_per_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sc, "SNAPSHOT_ROOT", tmp_path)
    out = await sc.snapshot(date(2026, 4, 17), with_instruments=False)

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["snapshot_version"] == 1
    assert manifest["snapshot_date"] == "2026-04-17"
    assert "captured_at" in manifest

    files = manifest["files"]
    for fname, sha in files.items():
        # SHA256 hex is 64 chars
        assert len(sha) == 64, f"{fname} sha not 64 chars"
        actual = hashlib.sha256((out / fname).read_bytes()).hexdigest()
        assert actual == sha, f"manifest sha for {fname} doesn't match file"


@pytest.mark.asyncio
async def test_env_redacts_secret_fields(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KITE_API_KEY", "test_api_key_xyz")
    monkeypatch.setenv("KITE_API_SECRET", "test_api_secret_abc")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1234:abc")
    monkeypatch.setattr(sc, "SNAPSHOT_ROOT", tmp_path)

    out = await sc.snapshot(date(2026, 4, 17), with_instruments=False)
    env_text = (out / "env.yaml").read_text()

    # Raw secrets must not appear
    assert "test_api_key_xyz" not in env_text
    assert "test_api_secret_abc" not in env_text
    assert "1234:abc" not in env_text

    # But the redaction sentinel should
    assert "<redacted:sha256:" in env_text


@pytest.mark.asyncio
async def test_secret_digest_changes_with_rotation(tmp_path: Path, monkeypatch):
    """Two different secret values must produce different SHA digests."""
    monkeypatch.setattr(sc, "SNAPSHOT_ROOT", tmp_path)

    monkeypatch.setenv("KITE_API_KEY", "alpha_key_1")
    out1 = await sc.snapshot(date(2026, 4, 16), with_instruments=False)
    digest1 = (out1 / "env.yaml").read_text()

    monkeypatch.setenv("KITE_API_KEY", "beta_key_2")
    out2 = await sc.snapshot(date(2026, 4, 17), with_instruments=False)
    digest2 = (out2 / "env.yaml").read_text()

    # Find the kite_api_key line in each output
    def _grab(line_text: str, prefix: str) -> str:
        for ln in line_text.splitlines():
            if ln.startswith(prefix):
                return ln
        return ""

    line1 = _grab(digest1, "kite_api_key:")
    line2 = _grab(digest2, "kite_api_key:")
    assert line1 != line2, "secret rotation should produce a different digest"


@pytest.mark.asyncio
async def test_no_tmp_files_left_behind(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sc, "SNAPSHOT_ROOT", tmp_path)
    out = await sc.snapshot(date(2026, 4, 17), with_instruments=False)
    leftovers = list(out.glob("*.tmp")) + list(out.glob("*.json.tmp"))
    assert leftovers == [], f"atomic writes should clean up: {leftovers}"


@pytest.mark.asyncio
async def test_skipped_includes_instruments_when_disabled(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sc, "SNAPSHOT_ROOT", tmp_path)
    out = await sc.snapshot(date(2026, 4, 17), with_instruments=False)

    manifest = json.loads((out / "manifest.json").read_text())
    skipped = manifest.get("skipped", [])
    assert any("instruments.json" in s for s in skipped), \
        "manifest must record why instruments were skipped"


@pytest.mark.asyncio
async def test_constants_capture_market_hours(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sc, "SNAPSHOT_ROOT", tmp_path)
    out = await sc.snapshot(date(2026, 4, 17), with_instruments=False)

    constants = json.loads((out / "constants.json").read_text())
    assert constants["market_open"] == "09:15:00"
    assert constants["market_close"] == "15:30:00"
    assert "lot_sizes" in constants
    assert constants["lot_sizes"]["NIFTY"] == 75
    assert "expiry_schedule" in constants


@pytest.mark.asyncio
async def test_holidays_include_target_date_metadata(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sc, "SNAPSHOT_ROOT", tmp_path)
    out = await sc.snapshot(date(2026, 4, 17), with_instruments=False)

    holidays = json.loads((out / "holidays.json").read_text())
    assert holidays["target_date"] == "2026-04-17"
    assert "holidays" in holidays
    assert "2026" in holidays["holidays"]
    # Sanity: at least a handful of holidays in a year
    assert len(holidays["holidays"]["2026"]) >= 5


def test_redact_handles_empty_secret_field():
    """An empty secret value still gets the sentinel — never a blank value."""
    assert sc._redact("kite_api_key", "") == "<redacted>"
    assert sc._redact("anthropic_api_key", None) == "<redacted>"


def test_redact_leaves_non_secret_alone():
    assert sc._redact("database_url", "postgres://...") == "postgres://..."
    assert sc._redact("max_day_loss", 15000) == 15000


def test_yaml_emitter_quotes_special_strings():
    # JSON-y strings must be quoted so the YAML parser sees them as strings
    out = sc._to_yaml({"x": "true", "y": "null", "z": "1.0:foo"})
    assert '"true"' in out
    assert '"null"' in out
    # Strings with colons must be quoted
    assert '"1.0:foo"' in out
