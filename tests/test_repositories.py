from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.engine import analyze
from app.database import repositories as repo
from app.database.models import PaymentStatus


async def test_get_or_create_user_is_idempotent(session: AsyncSession) -> None:
    u1 = await repo.get_or_create_user(session, telegram_id=42, username="alice")
    u2 = await repo.get_or_create_user(session, telegram_id=42, username="alice")
    await session.commit()
    assert u1.id == u2.id


async def test_create_and_fetch_analysis_scoped_to_user(session: AsyncSession) -> None:
    user_a = await repo.get_or_create_user(session, telegram_id=1, username="a")
    user_b = await repo.get_or_create_user(session, telegram_id=2, username="b")
    await session.flush()

    result = analyze("2200373")
    analysis = await repo.create_analysis(session, user_id=user_a.id, result=result)
    await session.commit()

    fetched = await repo.get_analysis(session, analysis.id, user_a.id)
    assert fetched is not None
    assert fetched.number == "2200373"

    # A different user must never be able to fetch someone else's analysis.
    forbidden = await repo.get_analysis(session, analysis.id, user_b.id)
    assert forbidden is None


async def test_list_recent_analyses_ordered_and_scoped(session: AsyncSession) -> None:
    user = await repo.get_or_create_user(session, telegram_id=10, username="x")
    await session.flush()

    for number in ("2200373", "3144401", "5005069"):
        result = analyze(number)
        await repo.create_analysis(session, user_id=user.id, result=result)
    await session.commit()

    recent = await repo.list_recent_analyses(session, user.id, limit=10)
    assert len(recent) == 3
    assert recent[0].number == "5005069"  # most recent first


async def test_create_pending_payment_is_idempotent(session: AsyncSession) -> None:
    user = await repo.get_or_create_user(session, telegram_id=5, username="p")
    await session.flush()
    result = analyze("2200373")
    analysis = await repo.create_analysis(session, user_id=user.id, result=result)
    await session.commit()

    p1 = await repo.create_pending_payment(
        session,
        user_id=user.id,
        analysis_id=analysis.id,
        amount=99,
        currency="RUB",
        provider="mock",
        provider_payment_id="same-id",
    )
    p2 = await repo.create_pending_payment(
        session,
        user_id=user.id,
        analysis_id=analysis.id,
        amount=99,
        currency="RUB",
        provider="mock",
        provider_payment_id="same-id",
    )
    await session.commit()

    assert p1.id == p2.id


async def test_mark_payment_paid_is_idempotent(session: AsyncSession) -> None:
    user = await repo.get_or_create_user(session, telegram_id=6, username="q")
    await session.flush()
    result = analyze("2200373")
    analysis = await repo.create_analysis(session, user_id=user.id, result=result)
    await session.commit()

    payment = await repo.create_pending_payment(
        session,
        user_id=user.id,
        analysis_id=analysis.id,
        amount=99,
        currency="RUB",
        provider="mock",
        provider_payment_id="pay-1",
    )
    await session.commit()

    first = await repo.mark_payment_paid(session, payment)
    second = await repo.mark_payment_paid(session, payment)
    await session.commit()

    assert first is True  # transitioned pending -> paid
    assert second is False  # already paid, no-op
    assert payment.status == PaymentStatus.PAID
