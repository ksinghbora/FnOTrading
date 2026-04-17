"""Application configuration using Pydantic Settings.

Reads from .env file and environment variables. Secret fields (Kite, Telegram,
Anthropic, API key) prefer the OS keychain — see src/core/secrets.py.
"""

from decimal import Decimal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.core.secrets import get_secret


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ─── Zerodha Kite Connect ────────────────────────────────────────
    kite_api_key: str = ""
    kite_api_secret: str = ""
    kite_access_token: str = ""

    # ─── Database ────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://trader:tradersecret@localhost:5432/fnotrading"

    # ─── Redis ───────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ─── Telegram ────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ─── Risk Limits ─────────────────────────────────────────────────
    max_day_loss: Decimal = Decimal("15000")
    max_strategy_loss: Decimal = Decimal("5000")
    max_total_lots: int = 50
    max_open_orders: int = 20
    max_orders_per_second: int = 5

    # ─── Application ─────────────────────────────────────────────────
    log_level: str = "INFO"
    environment: str = "development"
    api_secret_key: str = "change-me-in-production"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # ─── AI Advisor ──────────────────────────────────────────────────
    anthropic_api_key: str = ""
    advisor_model: str = "claude-sonnet-4-20250514"
    advisor_confluence_enabled: bool = False  # False = shadow mode (log only)
    advisor_confluence_weight: float = 1.0  # Scale AI adjustment (0.5 = half weight)

    # ─── Paper Trading ───────────────────────────────────────────────
    paper_trading: bool = True  # Default to paper trading for safety

    # ─── Strategies ────────────────────────────────────────────────
    # JSON array of strategies to auto-start, e.g.:
    # [{"name": "short_strangle", "id": "nifty_strangle_1", "params": {"underlying": "NIFTY"}}]
    strategies: str = "[]"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @model_validator(mode="after")
    def _resolve_secrets_from_keychain(self) -> "Settings":
        """Override empty/default secret fields with values from the OS keychain.

        Env values still win when present (already loaded by pydantic-settings);
        the keychain only fills in blanks. This lets a deploy keep .env free of
        secrets while CI/Docker can still inject via environment.
        """
        # (env_key_name, attr_name, default_sentinel)
        _SECRET_ATTRS = (
            ("KITE_API_SECRET", "kite_api_secret", ""),
            ("KITE_ACCESS_TOKEN", "kite_access_token", ""),
            ("TELEGRAM_BOT_TOKEN", "telegram_bot_token", ""),
            ("ANTHROPIC_API_KEY", "anthropic_api_key", ""),
            ("API_SECRET_KEY", "api_secret_key", "change-me-in-production"),
        )
        for env_key, attr, sentinel in _SECRET_ATTRS:
            current = getattr(self, attr)
            if current == sentinel or not current:
                resolved = get_secret(env_key)
                if resolved:
                    object.__setattr__(self, attr, resolved)
        return self

    @model_validator(mode="after")
    def _validate_settings(self) -> "Settings":
        if not self.paper_trading:
            if not self.kite_api_key or not self.kite_access_token:
                raise ValueError(
                    "kite_api_key and kite_access_token are required for live trading"
                )
        if self.is_production and self.api_secret_key == "change-me-in-production":
            raise ValueError(
                "api_secret_key must be changed from default in production"
            )
        return self
