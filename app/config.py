"""Application configuration loaded from environment variables / .env file."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(default="", alias="BOT_TOKEN")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")

    payment_provider: str = Field(default="mock", alias="PAYMENT_PROVIDER")
    payment_token: str = Field(default="", alias="PAYMENT_TOKEN")

    database_url: str = Field(
        default="sqlite+aiosqlite:///./lucky_num.db", alias="DATABASE_URL"
    )

    price_rub: int = Field(default=99, alias="PRICE_RUB")
    currency: str = Field(default="RUB", alias="CURRENCY")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def telegram_payments_configured(self) -> bool:
        return self.payment_provider == "telegram" and bool(self.payment_token)


settings = Settings()
