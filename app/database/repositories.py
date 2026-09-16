"""Repository functions — the only place raw queries against the ORM live.

Handlers and services call these instead of building SQLAlchemy queries
themselves, keeping persistence concerns out of business/UI logic.
"""

from __future__ import annotations

import datetime as dt
from typing import cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.models import AnalysisResult
from app.database.models import Analysis, Event, Payment, PaymentStatus, User


async def _get_user_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def get_or_create_user(
    session: AsyncSession, telegram_id: int, username: str | None
) -> User:
    """Idempotent creation, hardened against the same class of race as
    create_pending_payment: two Telegram updates from the same user can be
    dispatched concurrently (e.g. a rapid double-tap), each opening its own
    session and both seeing "no existing row" before either commits. Only
    one insert can win the UNIQUE constraint on telegram_id; the other must
    gracefully return the winner's row instead of propagating an
    IntegrityError.
    """
    user = await _get_user_by_telegram_id(session, telegram_id)
    if user is not None:
        if username and user.username != username:
            user.username = username
        return user

    user = User(telegram_id=telegram_id, username=username)
    try:
        async with session.begin_nested():
            session.add(user)
            await session.flush()
    except IntegrityError:
        user = await _get_user_by_telegram_id(session, telegram_id)
        if user is not None:
            if username and user.username != username:
                user.username = username
            return user
        raise
    return user


async def set_user_birth_date(session: AsyncSession, user: User, birth_date: dt.date) -> None:
    """Remember a user's birth date so it is asked once, not per banknote.

    Overwriting it never rewrites past analyses: each analysis stores the
    derived numbers it was computed with in its own payload.
    """
    user.birth_date = birth_date
    await session.flush()


async def create_analysis(session: AsyncSession, user_id: int, result: AnalysisResult) -> Analysis:
    analysis = Analysis(
        user_id=user_id,
        number=result.normalized_number,
        digit_sum=result.digit_sum,
        final_number=result.reduced_number,
        money_score=result.money_score,
        luck_score=result.luck_score,
        growth_score=result.growth_score,
        stability_score=result.stability_score,
        overall_score=result.overall_score,
        algorithm_version=result.algorithm_version,
        analysis_payload=result.model_dump_public(),
        paid=False,
        report=None,
    )
    session.add(analysis)
    await session.flush()
    return analysis


async def get_analysis(session: AsyncSession, analysis_id: int, user_id: int) -> Analysis | None:
    """Fetch an analysis, scoped to the owning user to prevent cross-user access."""
    result = await session.execute(
        select(Analysis).where(Analysis.id == analysis_id, Analysis.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def list_recent_analyses(
    session: AsyncSession, user_id: int, limit: int = 10
) -> list[Analysis]:
    result = await session.execute(
        select(Analysis)
        .where(Analysis.user_id == user_id)
        .order_by(Analysis.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def mark_analysis_paid(session: AsyncSession, analysis: Analysis) -> None:
    analysis.paid = True
    await session.flush()


async def save_report(session: AsyncSession, analysis: Analysis, report: str) -> None:
    analysis.report = report
    await session.flush()


async def get_payment_by_provider_id(
    session: AsyncSession, provider_payment_id: str
) -> Payment | None:
    result = await session.execute(
        select(Payment).where(Payment.provider_payment_id == provider_payment_id)
    )
    return result.scalar_one_or_none()


async def create_pending_payment(
    session: AsyncSession,
    user_id: int,
    analysis_id: int,
    amount: int,
    currency: str,
    provider: str,
    provider_payment_id: str,
) -> Payment:
    """Idempotent creation: if a payment with this provider_payment_id already
    exists, return it instead of creating a duplicate.

    The check-then-insert above is not atomic by itself — two concurrent
    callers could both see "no existing row" and both attempt to insert.
    The database's UNIQUE constraint on provider_payment_id is the real
    guarantee; the SAVEPOINT here just makes losing that race a graceful
    "return the winner's row" instead of an unhandled IntegrityError, without
    discarding whatever else the caller's session was mid-transaction on.
    """
    existing = await get_payment_by_provider_id(session, provider_payment_id)
    if existing is not None:
        return existing

    payment = Payment(
        user_id=user_id,
        analysis_id=analysis_id,
        amount=amount,
        currency=currency,
        provider=provider,
        provider_payment_id=provider_payment_id,
        status=PaymentStatus.PENDING,
    )
    try:
        async with session.begin_nested():
            session.add(payment)
            await session.flush()
    except IntegrityError:
        existing = await get_payment_by_provider_id(session, provider_payment_id)
        if existing is not None:
            return existing
        raise
    return payment


async def get_latest_pending_payment(
    session: AsyncSession, analysis_id: int, user_id: int
) -> Payment | None:
    result = await session.execute(
        select(Payment)
        .where(
            Payment.analysis_id == analysis_id,
            Payment.user_id == user_id,
            Payment.status == PaymentStatus.PENDING,
        )
        .order_by(Payment.created_at.desc())
    )
    return result.scalars().first()


async def mark_payment_paid(session: AsyncSession, payment: Payment) -> bool:
    """Transition a payment to PAID exactly once, even if two callers race on
    the same payment (e.g. a rapid double-tap on "Я оплатил, проверить
    статус", each dispatched as a separate update with its own session). A
    Python-side "if not already PAID" check followed by a flush is not
    atomic — both concurrent callers could read PENDING before either
    commits, and both would then treat the transition as theirs, double-
    logging payment_success and potentially double-delivering the report.
    The UPDATE ... WHERE status != PAID below is a single atomic statement:
    only the caller whose UPDATE actually matches the row (rowcount > 0) is
    considered to have performed the transition.
    """
    result = cast(
        CursorResult,
        await session.execute(
            update(Payment)
            .where(Payment.id == payment.id, Payment.status != PaymentStatus.PAID)
            .values(status=PaymentStatus.PAID)
        ),
    )
    await session.flush()
    if result.rowcount == 0:
        return False
    payment.status = PaymentStatus.PAID
    return True


async def mark_payment_failed(session: AsyncSession, payment: Payment) -> None:
    if payment.status == PaymentStatus.PAID:
        return
    payment.status = PaymentStatus.FAILED
    await session.flush()


async def get_payment_for_analysis(
    session: AsyncSession, analysis_id: int
) -> Payment | None:
    result = await session.execute(
        select(Payment)
        .where(Payment.analysis_id == analysis_id, Payment.status == PaymentStatus.PAID)
        .order_by(Payment.created_at.desc())
    )
    return result.scalars().first()


async def log_event(
    session: AsyncSession, user_id: int, name: str, payload: dict | None = None
) -> Event:
    event = Event(user_id=user_id, name=name, payload=payload)
    session.add(event)
    await session.flush()
    return event


async def funnel_stats(session: AsyncSession) -> dict[str, int]:
    """Simple event-name -> count breakdown for basic funnel analysis."""
    result = await session.execute(select(Event.name, func.count(Event.id)).group_by(Event.name))
    return {name: count for name, count in result.all()}
