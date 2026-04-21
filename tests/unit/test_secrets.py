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

    def test_default_returned_when_neither_source_has_value(self, monkeypatch, tmp_path):
        monkeypatch.delenv("KITE_API_SECRET", raising=False)
        # Point the .env fallback at an empty tmp dir (Apr 21: default is
        # now repo-root, no longer CWD-relative — chdir alone won't isolate).
        monkeypatch.setattr(secrets, "_DEFAULT_DOTENV", tmp_path / ".env")
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


class TestDotenvFallback:
    """The .env tertiary fallback was added Apr 20 after Claude Desktop /
    Claude Code was found to inject `ANTHROPIC_API_KEY=""` into the shell,
    which then shadowed the real value in .env (pydantic-settings
    prioritises os.environ over .env). Without these fallback tests the
    advisor silently disables itself and we only notice via Telegram.
    """

    def test_dotenv_picked_up_when_env_is_empty_string(self, monkeypatch, tmp_path):
        # Empty env value masks the real .env line — must not win.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        env_file = tmp_path / ".env"
        env_file.write_text("ANTHROPIC_API_KEY=sk-ant-real-key\n")
        monkeypatch.setattr(secrets, "_DEFAULT_DOTENV", env_file)
        with patch.object(secrets, "_KEYRING_AVAILABLE", False):
            assert secrets.get_secret("ANTHROPIC_API_KEY") == "sk-ant-real-key"

    def test_dotenv_picked_up_when_env_unset(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("ANTHROPIC_API_KEY=sk-ant-real-key\n")
        monkeypatch.setattr(secrets, "_DEFAULT_DOTENV", env_file)
        with patch.object(secrets, "_KEYRING_AVAILABLE", False):
            assert secrets.get_secret("ANTHROPIC_API_KEY") == "sk-ant-real-key"

    def test_env_still_wins_over_dotenv_when_non_empty(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
        env_file = tmp_path / ".env"
        env_file.write_text("ANTHROPIC_API_KEY=from-dotenv\n")
        monkeypatch.setattr(secrets, "_DEFAULT_DOTENV", env_file)
        with patch.object(secrets, "_KEYRING_AVAILABLE", False):
            assert secrets.get_secret("ANTHROPIC_API_KEY") == "from-env"

    def test_keychain_still_wins_over_dotenv(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        env_file = tmp_path / ".env"
        env_file.write_text("ANTHROPIC_API_KEY=from-dotenv\n")
        monkeypatch.setattr(secrets, "_DEFAULT_DOTENV", env_file)
        with patch.object(secrets, "_KEYRING_AVAILABLE", True), \
             patch.object(secrets, "_keyring") as kr:
            kr.get_password.return_value = "from-keychain"
            assert secrets.get_secret("ANTHROPIC_API_KEY") == "from-keychain"

    def test_dotenv_handles_quoted_value(self, monkeypatch, tmp_path):
        monkeypatch.delenv("X", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text('X="quoted-value"\n')
        monkeypatch.setattr(secrets, "_DEFAULT_DOTENV", env_file)
        with patch.object(secrets, "_KEYRING_AVAILABLE", False):
            assert secrets.get_secret("X") == "quoted-value"

    def test_dotenv_skips_comments_and_blanks(self, monkeypatch, tmp_path):
        monkeypatch.delenv("X", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# header comment\n"
            "\n"
            "Y=other\n"
            "X=hit\n"
        )
        monkeypatch.setattr(secrets, "_DEFAULT_DOTENV", env_file)
        with patch.object(secrets, "_KEYRING_AVAILABLE", False):
            assert secrets.get_secret("X") == "hit"

    def test_dotenv_missing_file_returns_default(self, monkeypatch, tmp_path):
        monkeypatch.delenv("X", raising=False)
        # tmp_path has no .env — point default there to ensure miss
        monkeypatch.setattr(secrets, "_DEFAULT_DOTENV", tmp_path / ".env")
        with patch.object(secrets, "_KEYRING_AVAILABLE", False):
            assert secrets.get_secret("X", default="dflt") == "dflt"

    def test_dotenv_empty_value_treated_as_unset(self, monkeypatch, tmp_path):
        monkeypatch.delenv("X", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("X=\n")
        monkeypatch.setattr(secrets, "_DEFAULT_DOTENV", env_file)
        with patch.object(secrets, "_KEYRING_AVAILABLE", False):
            assert secrets.get_secret("X", default="dflt") == "dflt"


class TestDotenvPathResolution:
    """Apr 21 hardening: the .env fallback must work even when CWD isn't
    the repo root. The original Apr 20 fix used a bare relative ".env"
    which only worked because launchd's restart-daemon.sh happened to
    `cd $REPO` first. Anyone restarting the daemon from another CWD
    silently lost the fallback and saw "advisor disabled" at 9 AM.
    """

    def test_default_path_is_cwd_independent(self, monkeypatch, tmp_path):
        # With CWD elsewhere AND a spurious .env in tmp_path, the
        # production default must NOT pick up the spurious value — it
        # must look at the repo-root .env (resolved from this module's
        # own location, not CWD).
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=spurious-tmp-value\n")
        # Don't patch _DEFAULT_DOTENV — we're testing exactly that the
        # production default ignores CWD.
        with patch.object(secrets, "_KEYRING_AVAILABLE", False):
            result = secrets.get_secret("ANTHROPIC_API_KEY", default="dflt")
        assert result != "spurious-tmp-value", (
            "get_secret() picked up CWD-relative .env instead of "
            "repo-root .env. The daemon will silently break when "
            "started from any directory other than the repo root."
        )

    def test_default_dotenv_constant_points_to_repo_root(self):
        # Lock the constant so a future refactor of the parents[N] math
        # can't silently drift.
        from pathlib import Path as _P
        expected_root = _P(secrets.__file__).resolve().parents[2]
        assert secrets._DEFAULT_DOTENV == expected_root / ".env"
        assert secrets._DEFAULT_DOTENV.name == ".env"

    def test_explicit_relative_path_still_honoured(self, monkeypatch, tmp_path):
        # Tests use this form — must keep working.
        monkeypatch.delenv("X", raising=False)
        (tmp_path / ".env").write_text("X=tmp-value\n")
        monkeypatch.chdir(tmp_path)
        with patch.object(secrets, "_KEYRING_AVAILABLE", False):
            assert secrets._read_dotenv_value("X", ".env") == "tmp-value"

    def test_explicit_absolute_path_honoured(self, tmp_path):
        env_file = tmp_path / "custom.env"
        env_file.write_text("X=abs-value\n")
        with patch.object(secrets, "_KEYRING_AVAILABLE", False):
            assert secrets._read_dotenv_value("X", env_file) == "abs-value"


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
