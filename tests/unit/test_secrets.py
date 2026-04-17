"""Tests for OS-keychain secret loader (Apr 17 Critical-2 fix).

Plaintext secrets in .env were a leak risk. The shim must:
  1. Prefer keychain over env when both are set (defence in depth — a
     keychain rotation should win over a stale .env line).
  2. Fall back to env when keychain is empty/missing (CI, Docker).
  3. Never crash when keyring backend is missing.
"""

from unittest.mock import MagicMock, patch

from src.core import secrets


class TestGetSecret:
    def test_keychain_value_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("KITE_API_SECRET", "from-env")
        with patch.object(secrets, "_KEYRING_AVAILABLE", True), \
             patch.object(secrets, "_keyring") as kr:
            kr.get_password.return_value = "from-keychain"
            assert secrets.get_secret("KITE_API_SECRET") == "from-keychain"

    def test_env_fallback_when_keychain_empty(self, monkeypatch):
        monkeypatch.setenv("KITE_API_SECRET", "from-env")
        with patch.object(secrets, "_KEYRING_AVAILABLE", True), \
             patch.object(secrets, "_keyring") as kr:
            kr.get_password.return_value = None
            assert secrets.get_secret("KITE_API_SECRET") == "from-env"

    def test_env_fallback_when_keyring_missing(self, monkeypatch):
        monkeypatch.setenv("KITE_API_SECRET", "from-env")
        with patch.object(secrets, "_KEYRING_AVAILABLE", False):
            assert secrets.get_secret("KITE_API_SECRET") == "from-env"

    def test_default_returned_when_neither_source_has_value(self, monkeypatch):
        monkeypatch.delenv("KITE_API_SECRET", raising=False)
        with patch.object(secrets, "_KEYRING_AVAILABLE", False):
            assert secrets.get_secret("KITE_API_SECRET", default="fallback") == "fallback"

    def test_empty_keychain_value_treated_as_unset(self, monkeypatch):
        # User who blanks a stale entry expects env (or default) to take over,
        # not see the empty string shadow it.
        monkeypatch.setenv("KITE_API_SECRET", "from-env")
        with patch.object(secrets, "_KEYRING_AVAILABLE", True), \
             patch.object(secrets, "_keyring") as kr:
            kr.get_password.return_value = ""
            assert secrets.get_secret("KITE_API_SECRET") == "from-env"

    def test_keyring_exception_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv("KITE_API_SECRET", "from-env")
        with patch.object(secrets, "_KEYRING_AVAILABLE", True), \
             patch.object(secrets, "_keyring") as kr:
            kr.get_password.side_effect = secrets._KeyringError("backend down")
            assert secrets.get_secret("KITE_API_SECRET") == "from-env"


class TestSetSecret:
    def test_returns_false_when_keyring_missing(self):
        with patch.object(secrets, "_KEYRING_AVAILABLE", False):
            assert secrets.set_secret("X", "y") is False

    def test_returns_true_on_success(self):
        with patch.object(secrets, "_KEYRING_AVAILABLE", True), \
             patch.object(secrets, "_keyring") as kr:
            kr.set_password.return_value = None
            assert secrets.set_secret("X", "y") is True
            kr.set_password.assert_called_once_with(secrets.KEYRING_SERVICE, "X", "y")

    def test_returns_false_on_backend_error(self):
        with patch.object(secrets, "_KEYRING_AVAILABLE", True), \
             patch.object(secrets, "_keyring") as kr:
            kr.set_password.side_effect = secrets._KeyringError("denied")
            assert secrets.set_secret("X", "y") is False


class TestKeyringAvailable:
    def test_false_when_module_missing(self):
        with patch.object(secrets, "_KEYRING_AVAILABLE", False):
            assert secrets.keyring_available() is False

    def test_false_when_null_backend(self):
        with patch.object(secrets, "_KEYRING_AVAILABLE", True), \
             patch.object(secrets, "_keyring") as kr:
            backend = MagicMock()
            backend.__class__.__name__ = "NullBackend"
            kr.get_keyring.return_value = backend
            # Patching __class__.__name__ on a MagicMock is brittle; rely on
            # the type() lookup hitting MagicMock — verify by force-string
            with patch("src.core.secrets.type", return_value=type("NullBackend", (), {})):
                assert secrets.keyring_available() is False


class TestSecretKeysCoverage:
    def test_critical_kite_secrets_listed(self):
        for key in ("KITE_API_SECRET", "KITE_ACCESS_TOKEN", "KITE_PASSWORD", "KITE_TOTP_SECRET"):
            assert key in secrets.SECRET_KEYS

    def test_telegram_and_anthropic_listed(self):
        assert "TELEGRAM_BOT_TOKEN" in secrets.SECRET_KEYS
        assert "ANTHROPIC_API_KEY" in secrets.SECRET_KEYS

    def test_api_secret_key_listed(self):
        # The API auth key must rotate via keychain too — not just .env
        assert "API_SECRET_KEY" in secrets.SECRET_KEYS
