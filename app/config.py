"""Application configuration loaded from environment variables / .env file."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

# The directory holding the `app` package, i.e. the project directory. Paths
# are anchored here rather than to the process working directory: a service
# unit without WorkingDirectory, a scheduled task or an IDE run configuration
# would otherwise read no .env at all and open (and, via the startup schema
# gate, create) a different, empty SQLite database next to wherever it was
# started from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    bot_token: str = Field(default="", alias="BOT_TOKEN")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-3.5-turbo", alias="OPENAI_MODEL")

    # Every OpenAI call already requires a confirmed real payment (see
    # app.bot.handlers.payment._deliver_report — every call site requires
    # analysis.paid=True), so routine volume is inherently gated by revenue.
    # The one uncapped vector is the "🔄 Попробовать ещё раз" button: once an
    # analysis is paid, nothing previously limited how many times a user
    # could re-trigger a real (billed) OpenAI attempt on that SAME analysis
    # after repeated failures. This bounds worst-case AI spend per analysis
    # to a small, predictable multiple of one report's cost, well under the
    # price the user already paid for it.
    ai_max_generation_attempts_per_analysis: int = Field(
        default=5, alias="AI_MAX_GENERATION_ATTEMPTS_PER_ANALYSIS"
    )

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

    @field_validator("database_url")
    @classmethod
    def _anchor_relative_sqlite_path(cls, value: str) -> str:
        """Make a relative SQLite file path absolute against PROJECT_ROOT.

        Only relative SQLite *file* paths change: ":memory:" (the test suite),
        absolute paths and every other backend (PostgreSQL in production) are
        returned untouched, so an explicitly configured location always wins.
        Alembic reads the same setting, so migrations and the bot agree on the
        file no matter which directory either is run from.
        """
        url = make_url(value)
        if not url.drivername.startswith("sqlite"):
            return value
        database = url.database
        if not database or database == ":memory:" or Path(database).is_absolute():
            return value
        anchored = (PROJECT_ROOT / database).resolve()
        return url.set(database=anchored.as_posix()).render_as_string(hide_password=False)

    price_rub: int = Field(default=99, alias="PRICE_RUB")
    currency: str = Field(default="RUB", alias="CURRENCY")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # --- Researcher leaderboard, promo codes, weekly automation -------------
    # Comma-separated Telegram user IDs allowed into the admin panel. Empty
    # means nobody is an admin. Checked server-side on every admin action.
    admin_telegram_ids_raw: str = Field(default="", alias="ADMIN_TELEGRAM_ID")
    # Numeric Telegram channel ID (e.g. -1001234567890) for the weekly post.
    # Empty disables channel publication (recorded as skipped, never crashes).
    promo_channel_id_raw: str = Field(default="", alias="PROMO_CHANNEL_ID")
    # Timezone that defines the Monday-Sunday leaderboard week. Accepts a
    # fixed UTC offset ("+03:00") or an IANA name ("Europe/Moscow"). IANA names
    # need the tz database; Windows Python has none unless `tzdata` is
    # installed, so the default is the offset Moscow has used since 2014.
    app_timezone: str = Field(default="+03:00", alias="APP_TIMEZONE")
    # Local time (HH:MM) on Monday when the weekly job runs. A few minutes
    # after midnight keeps it clear of reports landing exactly on the boundary.
    weekly_schedule_time: str = Field(default="00:05", alias="WEEKLY_SCHEDULE_TIME")
    weekly_promo_discount_percent: int = Field(default=25, alias="WEEKLY_PROMO_DISCOUNT_PERCENT")
    weekly_promo_max_activations: int = Field(default=100, alias="WEEKLY_PROMO_MAX_ACTIVATIONS")
    top_reward_discount_percent: int = Field(default=50, alias="TOP_REWARD_DISCOUNT_PERCENT")
    top_reward_valid_days: int = Field(default=7, alias="TOP_REWARD_VALID_DAYS")
    weekly_automation_enabled: bool = Field(default=True, alias="WEEKLY_AUTOMATION_ENABLED")

    @property
    def admin_telegram_ids(self) -> frozenset[int]:
        """Parsed admin IDs. Malformed entries are ignored rather than
        granting access — an unparseable value can never become an admin.
        """
        ids: set[int] = set()
        for part in self.admin_telegram_ids_raw.split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                ids.add(int(part))
        return frozenset(ids)

    @property
    def promo_channel_id(self) -> int | str | None:
        """Numeric chat ID (e.g. -1001234567890) or a public @channelname."""
        value = self.promo_channel_id_raw.strip()
        if value.lstrip("-").isdigit():
            return int(value)
        if value.startswith("@") and len(value) > 1:
            return value
        return None

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
