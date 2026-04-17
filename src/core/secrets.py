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
from typing import Final

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
    "BREEZE_API_SECRET",
    "BREEZE_SESSION_TOKEN",
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
    """Resolve a secret. Keychain wins; env is fallback.

    Empty-string values from the keychain are treated as not-set, so a
    user can blank a stale entry without seeing it shadow a newer .env value.
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
    return default


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
