"""Application configuration using Pydantic Settings.

Reads from .env file and environment variables.
"""

from decimal import Decimal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
