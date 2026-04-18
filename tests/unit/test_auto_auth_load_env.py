"""Tests for scripts/auto_auth.load_env() force-vs-setdefault behavior.

History: prior to Apr 18, load_env() unconditionally clobbered os.environ
with .env values. This silently broke A/B harnesses that pre-set
os.environ before importing strategy modules — strategy __init__ called
load_env() and wiped the override. Both ab_portfolio_filters.py and
ab_advisor_confluence.py had to work around this by patching the .env
file in try/finally blocks.

The fix added a `force` kwarg (default True for production) and switched
the strategy call site to force=False. These tests pin both branches so
a future "simplify load_env" refactor can't silently regress either
production startup OR test/harness override.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import auto_auth  # noqa: E402


@pytest.fixture
def fake_env_file(tmp_path, monkeypatch):
    """Point auto_auth.ENV_FILE at a tmp .env with two known keys."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "FROM_DOTENV_ONLY=dotenv_value\n"
        "OVERRIDABLE=dotenv_wins_by_default\n"
        "EMPTY_KEY=\n"  # Empty values must be skipped — protects against
                         # accidental clobber of unrelated env state.
        "# COMMENT_KEY=ignored\n"
    )
    monkeypatch.setattr(auto_auth, "ENV_FILE", env_path)
    return env_path


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Remove our test keys before each test so the previous run can't leak."""
    for k in ("FROM_DOTENV_ONLY", "OVERRIDABLE", "EMPTY_KEY", "COMMENT_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_force_true_clobbers_caller_overrides(fake_env_file):
    """Production startup behavior — .env wins, even if caller pre-set."""
    os.environ["OVERRIDABLE"] = "caller_set_first"
    auto_auth.load_env(force=True)
    assert os.environ["OVERRIDABLE"] == "dotenv_wins_by_default", \
        "force=True must overwrite — production expects .env to be canonical"
    assert os.environ["FROM_DOTENV_ONLY"] == "dotenv_value"


def test_force_false_respects_caller_overrides(fake_env_file):
    """Harness/test behavior — caller wins. Fixes the A/B-harness clobber bug."""
    os.environ["OVERRIDABLE"] = "caller_set_first"
    auto_auth.load_env(force=False)
    assert os.environ["OVERRIDABLE"] == "caller_set_first", \
        "force=False must NOT overwrite — A/B harnesses depend on this"
    # Missing keys should still be filled in.
    assert os.environ["FROM_DOTENV_ONLY"] == "dotenv_value"


def test_default_is_force_true(fake_env_file):
    """Default kwarg must stay True so existing call sites don't regress."""
    os.environ["OVERRIDABLE"] = "caller_set_first"
    auto_auth.load_env()
    assert os.environ["OVERRIDABLE"] == "dotenv_wins_by_default"


def test_empty_value_does_not_clobber(fake_env_file):
    """A line like `EMPTY_KEY=` in .env must NOT wipe an existing value.

    Otherwise a stale empty entry in .env (common when an operator
    comments out a credential) would silently break the running process.
    """
    os.environ["EMPTY_KEY"] = "set_by_caller"
    auto_auth.load_env(force=True)
    assert os.environ["EMPTY_KEY"] == "set_by_caller"


def test_comment_lines_ignored(fake_env_file):
    """`# COMMENT_KEY=...` must not turn into a real env var."""
    auto_auth.load_env(force=True)
    assert "COMMENT_KEY" not in os.environ


def test_missing_env_file_is_noop(tmp_path, monkeypatch):
    """If .env doesn't exist, load_env() must not raise."""
    monkeypatch.setattr(auto_auth, "ENV_FILE", tmp_path / "nonexistent.env")
    auto_auth.load_env(force=True)  # Should not raise.
    auto_auth.load_env(force=False)  # Should not raise.
