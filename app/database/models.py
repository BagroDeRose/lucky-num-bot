"""SQLAlchemy ORM models."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class PaymentStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # BigInteger: modern Telegram user IDs already exceed the 32-bit signed
    # range that a plain Integer would map to under a future Postgres move.
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Optional birth date for personalized analyses. Nullable because it is
    # genuinely optional: users can decline and still get the full
    # serial-number product, and every user who existed before this column
    # was added simply has NULL. Stored on the user (not per analysis) so it
    # is asked once and reused across banknotes; the *derived* numbers each
    # analysis was computed with live in that analysis's own payload, so
    # changing this date never rewrites past results.
    birth_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    analyses: Mapped[list[Analysis]] = relationship(back_populates="user")
    payments: Mapped[list[Payment]] = relationship(back_populates="user")
    events: Mapped[list[Event]] = relationship(back_populates="user")


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)

    number: Mapped[str] = mapped_column(String(32), nullable=False)
    digit_sum: Mapped[int] = mapped_column(Integer, nullable=False)
    final_number: Mapped[int] = mapped_column(Integer, nullable=False)

    money_score: Mapped[int] = mapped_column(Integer, nullable=False)
    luck_score: Mapped[int] = mapped_column(Integer, nullable=False)
    growth_score: Mapped[int] = mapped_column(Integer, nullable=False)
    stability_score: Mapped[int] = mapped_column(Integer, nullable=False)
    overall_score: Mapped[int] = mapped_column(Integer, nullable=False)

    algorithm_version: Mapped[str] = mapped_column(String(16), nullable=False)
    analysis_payload: Mapped[dict] = mapped_column(JSON, nullable=False)

    paid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    report: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When the paid report was actually saved (repositories.save_report) —
    # the canonical "completed report" instant the leaderboard counts. One
    # analysis holds at most one report and a stored report is never
    # regenerated, so each completed report is counted exactly once.
    report_completed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    user: Mapped[User] = relationship(back_populates="analyses")
    payments: Mapped[list[Payment]] = relationship(back_populates="analysis")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    analysis_id: Mapped[int] = mapped_column(
        ForeignKey("analyses.id"), nullable=False, index=True
    )

    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_payment_id: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default=PaymentStatus.PENDING, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    user: Mapped[User] = relationship(back_populates="payments")
    analysis: Mapped[Analysis] = relationship(back_populates="payments")


class Event(Base):
    """Lightweight funnel analytics event."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )

    user: Mapped[User] = relationship(back_populates="events")


# --- Researcher leaderboard, promo codes, weekly rewards ------------------


class PromoType(StrEnum):
    WEEKLY_PUBLIC = "weekly_public"
    TOP_REWARD = "top_reward"
    ADMIN_MANUAL = "admin_manual"


class RedemptionStatus(StrEnum):
    # Activated by the user; waits for their next purchase.
    APPLIED = "applied"
    # Bound to one payment (claimed before the provider payment is created,
    # so a single activation can never discount two payments).
    RESERVED = "reserved"
    # That payment succeeded — the discount has been used.
    CONSUMED = "consumed"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    # Claimed by a sender. Guards against two concurrent runs sending twice.
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    # Nothing to send to (e.g. no channel configured).
    SKIPPED = "skipped"


class PromoCode(Base):
    __tablename__ = "promo_codes"
    __table_args__ = (
        CheckConstraint(
            "discount_percent >= 1 AND discount_percent <= 99", name="ck_promo_discount_range"
        ),
        CheckConstraint("max_activations >= 1", name="ck_promo_max_activations_positive"),
        # Database-level backstop for the activation limit: even if two
        # activations raced past application checks, the database itself
        # refuses a count above the limit.
        CheckConstraint(
            "activations_count >= 0 AND activations_count <= max_activations",
            name="ck_promo_activations_within_limit",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Stored normalized (upper-case, trimmed) so lookups are case-insensitive.
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    promo_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    discount_percent: Mapped[int] = mapped_column(Integer, nullable=False)
    max_activations: Mapped[int] = mapped_column(Integer, nullable=False)
    activations_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_from: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Exclusive: the code works strictly before this instant.
    valid_until: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Personalized codes (TOP rewards, admin grants) can only be used by this user.
    target_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    created_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class PromoRedemption(Base):
    __tablename__ = "promo_code_redemptions"
    __table_args__ = (
        UniqueConstraint("promo_code_id", "user_id", name="uq_redemption_promo_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    promo_code_id: Mapped[int] = mapped_column(
        ForeignKey("promo_codes.id"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RedemptionStatus.APPLIED, index=True
    )
    # One activation discounts at most one payment, and vice versa.
    payment_id: Mapped[int | None] = mapped_column(
        ForeignKey("payments.id"), nullable=True, unique=True
    )
    redeemed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    reserved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WeeklyLeaderboard(Base):
    """A finalized, immutable weekly snapshot, plus the state of the Monday
    automation that follows it (public promo, channel post).
    """

    __tablename__ = "weekly_leaderboards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # UTC instants of local Monday 00:00 and the next Monday 00:00 (exclusive).
    week_start: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, unique=True
    )
    week_end: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="finalized")
    finalized_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # The weekly public promo issued for the week that follows this one.
    public_promo_id: Mapped[int | None] = mapped_column(
        ForeignKey("promo_codes.id"), nullable=True, unique=True
    )
    channel_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DeliveryStatus.PENDING
    )
    channel_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    channel_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    channel_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    channel_posted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class WeeklyLeaderboardEntry(Base):
    __tablename__ = "weekly_leaderboard_entries"
    __table_args__ = (
        UniqueConstraint("leaderboard_id", "user_id", name="uq_entry_leaderboard_user"),
        UniqueConstraint("leaderboard_id", "rank", name="uq_entry_leaderboard_rank"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    leaderboard_id: Mapped[int] = mapped_column(
        ForeignKey("weekly_leaderboards.id"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    report_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # The tie-break input, stored so the finalized order stays auditable.
    last_report_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Public name as it was at finalization, so history does not change when
    # a user later renames or removes their username.
    username_snapshot: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class WeeklyReward(Base):
    __tablename__ = "weekly_rewards"
    __table_args__ = (
        UniqueConstraint("leaderboard_id", "user_id", name="uq_reward_leaderboard_user"),
        UniqueConstraint("leaderboard_id", "rank", name="uq_reward_leaderboard_rank"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    leaderboard_id: Mapped[int] = mapped_column(
        ForeignKey("weekly_leaderboards.id"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    report_count: Mapped[int] = mapped_column(Integer, nullable=False)
    promo_code_id: Mapped[int] = mapped_column(
        ForeignKey("promo_codes.id"), nullable=False, unique=True
    )
    delivery_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DeliveryStatus.PENDING
    )
    delivery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delivery_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    delivered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
