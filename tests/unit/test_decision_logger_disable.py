"""Tests for ``FNO_DISABLE_DECISIONS`` env var on ``DecisionLogger``.

Parallel CPCV workers set this var so they don't race on the shared
``data/decisions/decisions_YYYY-MM-DD.csv`` files. Without the var,
multiple processes opening the same file in append mode produce duplicate
header rows and interleaved data rows — which breaks the stratifier's
cumcount-based ENTER/EXIT pairing (see
``src/backtest/validation/regime.py::_pair_enter_exit``).

The contract:

1. With the var unset (or empty/0/false), ``log()`` writes normally.
2. With the var truthy (1/true/yes/anything else), ``log()`` is a no-op
   — no file created, no rows written.
3. The check is per-call, so toggling mid-process is observable.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.strategy.decision_logger import DecisionLogger, DecisionSnapshot


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test starts with the var unset, regardless of outer env."""
    monkeypatch.delenv("FNO_DISABLE_DECISIONS", raising=False)
    yield


def _snap(timestamp: str = "2024-09-02T10:00:00+05:30") -> DecisionSnapshot:
    return DecisionSnapshot(
        timestamp=timestamp,
        strategy_id="test",
        leg="PREMIUM",
        decision="ENTER",
        rule_score=70,
    )


def test_log_writes_when_env_var_unset(tmp_path: Path):
    dl = DecisionLogger(output_dir=tmp_path)
    dl.log(_snap())
    files = list(tmp_path.glob("decisions_*.csv"))
    assert len(files) == 1
    # Header + 1 data row
    assert len(files[0].read_text().splitlines()) == 2


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_log_disabled_when_env_var_truthy(tmp_path: Path, monkeypatch, val: str):
    monkeypatch.setenv("FNO_DISABLE_DECISIONS", val)
    dl = DecisionLogger(output_dir=tmp_path)
    dl.log(_snap())
    dl.log(_snap("2024-09-02T11:00:00+05:30"))
    # No file should be created.
    files = list(tmp_path.glob("decisions_*.csv"))
    assert files == []


@pytest.mark.parametrize("val", ["0", "false", "FALSE", "no", ""])
def test_log_writes_when_env_var_falsy(tmp_path: Path, monkeypatch, val: str):
    monkeypatch.setenv("FNO_DISABLE_DECISIONS", val)
    dl = DecisionLogger(output_dir=tmp_path)
    dl.log(_snap())
    files = list(tmp_path.glob("decisions_*.csv"))
    assert len(files) == 1


def test_env_var_check_is_per_call_not_cached(tmp_path: Path, monkeypatch):
    """Toggling the var mid-process must be observable.

    Important for tests that re-enable logging after a setup phase, and
    for any future use where a parent process disables for workers but
    re-enables for its own use after the pool returns.
    """
    dl = DecisionLogger(output_dir=tmp_path)

    # Start with var set → no write.
    monkeypatch.setenv("FNO_DISABLE_DECISIONS", "1")
    dl.log(_snap())
    assert list(tmp_path.glob("decisions_*.csv")) == []

    # Unset → next log writes.
    monkeypatch.delenv("FNO_DISABLE_DECISIONS")
    dl.log(_snap())
    files = list(tmp_path.glob("decisions_*.csv"))
    assert len(files) == 1


def test_disable_does_not_open_file_or_create_dir(tmp_path: Path, monkeypatch):
    """When disabled, the logger should NOT create the output dir.

    This protects against a noisy side-effect when parallel workers each
    spawn under the umbrella of a fresh temp dir — we don't want them
    poking the file system at all.
    """
    monkeypatch.setenv("FNO_DISABLE_DECISIONS", "1")
    nested = tmp_path / "deeply" / "nested" / "dir_that_should_not_exist"
    dl = DecisionLogger(output_dir=nested)
    dl.log(_snap())
    assert not nested.exists()
