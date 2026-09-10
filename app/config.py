"""Application configuration loaded from environment variables / .env file."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(default="", alias="BOT_TOKEN")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-3.5-turbo", alias="OPENAI_MODEL")

    payment_provider: str = Field(default="mock", alias="PAYMENT_PROVIDER")
    # Native Telegram Payments provider token (PAYMENT_PROVIDER=telegram only).
    payment_token: str = Field(default="", alias="PAYMENT_TOKEN")

    # YooKassa (PAYMENT_PROVIDER=yookassa). Two credential pairs exist in a
    # YooKassa account: the *shop* keys authenticate the merchant/store and
    # are what actually create and verify payments (charging the user) —
    # that's the only thing this MVP does. The *agent* keys authenticate a
    # separate payouts API (sending money out, e.g. to a sub-merchant) that
    # this product has no feature for yet; they're accepted/stored here for
    # forward-compatibility but are NOT used by YooKassaPaymentProvider.
    yookassa_shop_id: str = Field(default="", alias="YOOKASSA_SHOP_ID")
    yookassa_shop_api_key: str = Field(default="", alias="YOOKASSA_SHOP_API_KEY")
    yookassa_agent_id: str = Field(default="", alias="YOOKASSA_AGENT_ID")
    yookassa_agent_api_key: str = Field(default="", alias="YOOKASSA_AGENT_API_KEY")

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

    @property
    def yookassa_configured(self) -> bool:
        return bool(self.yookassa_shop_id) and bool(self.yookassa_shop_api_key)


settings = Settings()
