"""TelegramNotifier construction signature is load-bearing across scripts.

Apr 20: morning_advisor / nightly_audit / alert_chain_dropoff / restore_test
were all calling `TelegramNotifier(settings)` — passing the Settings object
as `bot_token`. The class accepts (bot_token: str, chat_id: str), so the
calls failed at runtime with:

    TelegramNotifier.__init__() missing 1 required positional argument: 'chat_id'

The advisor swallowed the exception and printed a "Telegram send failed"
line, so the morning notification disappeared without anyone noticing
until the user got an alert about the *advisor* failing for an unrelated
reason and asked us to look.

This test pins the constructor signature: anyone refactoring it must also
update the four call sites listed above.
"""

from __future__ import annotations

import inspect

import pytest

from src.notifications.telegram import TelegramNotifier


class TestTelegramNotifierSignature:
    def test_constructor_takes_bot_token_and_chat_id(self):
        sig = inspect.signature(TelegramNotifier.__init__)
        params = list(sig.parameters.keys())
        assert params == ["self", "bot_token", "chat_id"], (
            f"TelegramNotifier.__init__ signature changed to {params}. "
            "Update scripts/morning_advisor.py, scripts/nightly_audit.py, "
            "scripts/alert_chain_dropoff.py, scripts/restore_test.py — "
            "they all instantiate it positionally."
        )

    def test_passing_settings_object_raises(self):
        # Documents the failure mode: the old buggy call shape must not
        # silently appear to work (e.g. via an accidental **kwargs swallow).
        class _FakeSettings:
            telegram_bot_token = "x"
            telegram_chat_id = "y"

        with pytest.raises(TypeError):
            TelegramNotifier(_FakeSettings())  # type: ignore[arg-type]

    def test_disabled_when_token_or_chat_missing(self):
        # If either credential is empty, .enabled must be False so callers
        # can short-circuit without a network call.
        assert TelegramNotifier("", "chat").enabled is False
        assert TelegramNotifier("token", "").enabled is False
        assert TelegramNotifier("", "").enabled is False

    def test_enabled_when_both_present(self):
        assert TelegramNotifier("tok", "chat").enabled is True
