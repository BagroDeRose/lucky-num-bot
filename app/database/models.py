"""SQLAlchemy ORM models."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
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
