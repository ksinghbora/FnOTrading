"""OS-keychain backed secret loader (Apr 17 trader-analysis Critical-2 fix).

Plaintext secrets in `.env` were flagged as a leak risk: anyone with read
access to the repo dir or a stale tarball can extract Kite credentials and
trade. This module reads from the OS keychain first (macOS Keychain, Linux
Secret Service, Windows Credential Manager) and only falls back to env
when keyring is unavailable (CI, Docker without DBus, etc).

Migration: `python -m src.core.secrets migrate` — reads .env once, writes
to keychain, prints a list of lines safe to remove from .env.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Final

# Repo root resolved at import time so the .env fallback is CWD-independent.
# secrets.py lives at src/core/secrets.py — three parents up is the repo root.
# Any caller can therefore find .env regardless of where the daemon was
# launched from (launchd, ad-hoc shell, debugger, pytest in tmp dir, etc).
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_DEFAULT_DOTENV: Final[Path] = _REPO_ROOT / ".env"

logger = logging.getLogger(__name__)

KEYRING_SERVICE: Final[str] = "fnotrading"

# Keys treated as secret. Order matters only for migration listing.
SECRET_KEYS: Final[tuple[str, ...]] = (
    "KITE_API_SECRET",
    "KITE_ACCESS_TOKEN",
    "KITE_PASSWORD",
    "KITE_TOTP_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "ANTHROPIC_API_KEY",
    "API_SECRET_KEY",
)

try:
    import keyring as _keyring
    from keyring.errors import KeyringError as _KeyringError

    _KEYRING_AVAILABLE = True
except ImportError:
    _keyring = None  # type: ignore[assignment]
    _KeyringError = Exception  # type: ignore[misc,assignment]
    _KEYRING_AVAILABLE = False


def keyring_available() -> bool:
    """True if the OS keychain backend is reachable."""
    if not _KEYRING_AVAILABLE:
        return False
    try:
        backend = _keyring.get_keyring()
        # keyring's null/fail backends report themselves with these names
        name = type(backend).__name__.lower()
        return "null" not in name and "fail" not in name
    except Exception:
        return False


def get_secret(key: str, default: str = "") -> str:
    """Resolve a secret. Keychain wins; env then .env are fallbacks.

    Empty-string values from any source are treated as not-set, so a
    user can blank a stale entry without seeing it shadow a newer value.

    The .env fallback (Apr 20 fix) matters because Claude Desktop /
    Claude Code injects ANTHROPIC_API_KEY="" into the shell to prevent
    its own key from leaking. Pydantic-settings prioritises os.environ
    over the .env file, so the empty env value would shadow the real
    one in .env and the morning advisor would silently disable itself
    with "No API key configured". By re-reading .env directly here we
    short-circuit that shadowing for any caller using get_secret.
    """
    if _KEYRING_AVAILABLE:
        try:
            value = _keyring.get_password(KEYRING_SERVICE, key)
            if value:
                return value
        except _KeyringError as e:
            logger.debug(f"[SECRETS] keyring read failed for {key}: {e}")

    env_value = os.environ.get(key, "")
    if env_value:
        return env_value

    dotenv_value = _read_dotenv_value(key)
    if dotenv_value:
        return dotenv_value

    return default


def _read_dotenv_value(key: str, dotenv_path: str | Path | None = None) -> str:
    """Read a single key from .env. Returns "" if file missing or key absent.

    Hand-rolled (no python-dotenv dep) to keep this module self-contained.
    Handles `KEY=value`, `KEY = value`, ignores comments and blank lines,
    strips surrounding quotes. Stops at the first match — duplicate keys
    in .env take the first occurrence (matches pydantic-settings behavior).

    Path resolution (Apr 21 hardening — see commit 32dd91c follow-up):
      * `dotenv_path=None` (production default) → repo-root .env, computed
        from this file's location. CWD-independent so the daemon resolves
        secrets correctly regardless of where it was launched from.
      * Explicit relative path → resolved against current CWD (test usage —
        tests call with "monkeypatch.chdir(tmp_path)" + path=".env").
      * Explicit absolute path → used as-is.

    Why the CWD-independent default matters: the original Apr 20 fix used a
    bare relative ".env" path and worked only because launchd's
    restart-daemon.sh happens to `cd $REPO` first. Anyone restarting the
    daemon from a different CWD (debugger, ad-hoc shell, ops tool) would
    silently lose the .env tertiary fallback and be back to "advisor
    disabled" at 9 AM with no obvious cause.
    """
    if dotenv_path is None:
        path: Path = _DEFAULT_DOTENV
    else:
        path = Path(dotenv_path)
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() != key:
                    continue
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                    v = v[1:-1]
                return v
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.debug(f"[SECRETS] .env read failed for {key}: {e}")
    return ""


def set_secret(key: str, value: str) -> bool:
    """Persist a secret to the OS keychain. Returns False on failure."""
    if not _KEYRING_AVAILABLE:
        logger.error("[SECRETS] keyring not installed — cannot store secret")
        return False
    try:
        _keyring.set_password(KEYRING_SERVICE, key, value)
        return True
    except _KeyringError as e:
        logger.error(f"[SECRETS] keyring write failed for {key}: {e}")
        return False


def delete_secret(key: str) -> bool:
    """Remove a secret from the OS keychain. Idempotent."""
    if not _KEYRING_AVAILABLE:
        return False
    try:
        _keyring.delete_password(KEYRING_SERVICE, key)
        return True
    except _KeyringError:
        return False


def _migrate_from_env() -> None:
    """One-shot: copy SECRET_KEYS from current env into the keychain.

    Prints a safe-to-remove list at the end. Does NOT touch the .env file
    itself — manual edit only, so the user sees what they're deleting.
    """
    if not keyring_available():
        print("ERROR: keyring backend not available on this host. Aborting.", file=sys.stderr)
        sys.exit(1)

    moved: list[str] = []
    skipped: list[str] = []
    for key in SECRET_KEYS:
        val = os.environ.get(key, "")
        if not val:
            skipped.append(key)
            continue
        if set_secret(key, val):
            moved.append(key)
        else:
            print(f"FAIL  {key}: keychain write failed", file=sys.stderr)

    print(f"\nMoved {len(moved)} secret(s) into the OS keychain (service='{KEYRING_SERVICE}'):")
    for k in moved:
        print(f"  - {k}")
    if skipped:
        print(f"\nSkipped {len(skipped)} (not in env):")
        for k in skipped:
            print(f"  - {k}")
    if moved:
        print("\nSafe to remove the following lines from .env:")
        for k in moved:
            print(f"  {k}=...")
        print("\nKeep .env entries for hosts without a keychain (CI, Docker).")


def _list_keychain_state() -> None:
    """Show which SECRET_KEYS are present in the keychain (without values)."""
    if not keyring_available():
        print("keyring backend not available", file=sys.stderr)
        sys.exit(1)
    print(f"Keychain service: {KEYRING_SERVICE}")
    for key in SECRET_KEYS:
        present = bool(_keyring.get_password(KEYRING_SERVICE, key))
        print(f"  [{'x' if present else ' '}] {key}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "migrate":
        _migrate_from_env()
    elif cmd == "list":
        _list_keychain_state()
    else:
        print(f"Usage: python -m src.core.secrets [migrate|list]", file=sys.stderr)
        sys.exit(2)
