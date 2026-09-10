from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.engine import analyze
from app.database import repositories as repo
from app.database.models import Analysis, PaymentStatus


async def test_get_or_create_user_is_idempotent(session: AsyncSession) -> None:
    u1 = await repo.get_or_create_user(session, telegram_id=42, username="alice")
    u2 = await repo.get_or_create_user(session, telegram_id=42, username="alice")
    await session.commit()
    assert u1.id == u2.id


async def test_get_or_create_user_recovers_from_lost_race(
    session: AsyncSession, monkeypatch
) -> None:
    """Two Telegram updates from the same user can be dispatched concurrently
    (e.g. a rapid double-tap), each opening its own session/middleware call.
    Both can see "no existing user" before either commits; only one insert
    wins the UNIQUE constraint on telegram_id. The loser must gracefully
    return the winner's row, not raise. Forced deterministically here (no
    reliance on real concurrency timing) by making the first existence
    check lie.
    """
    winner = await repo.get_or_create_user(session, telegram_id=55, username="original")
    await session.commit()

    real_lookup = repo._get_user_by_telegram_id  # noqa: SLF001
    calls = {"count": 0}

    async def lying_lookup_once(session: AsyncSession, telegram_id: int):
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        return await real_lookup(session, telegram_id)

    monkeypatch.setattr(repo, "_get_user_by_telegram_id", lying_lookup_once)

    loser_result = await repo.get_or_create_user(session, telegram_id=55, username="original")

    assert loser_result.id == winner.id
    # The SAVEPOINT rollback must not have poisoned the outer transaction.
    await repo.log_event(session, winner.id, "start")
    await session.commit()


async def test_foreign_key_enforcement_rejects_orphaned_row(session: AsyncSession) -> None:
    """SQLite ignores FK constraints unless enforcement is turned on per
    connection. Regression test for that being silently off: without the
    PRAGMA, this insert would succeed and leave an orphaned row.
    """
    bogus = Analysis(
        user_id=999999,  # no such user
        number="1234567",
        digit_sum=1,
        final_number=1,
        money_score=1,
        luck_score=1,
        growth_score=1,
        stability_score=1,
        overall_score=1,
        algorithm_version="1.0",
        analysis_payload={},
    )
    session.add(bogus)
    with pytest.raises(IntegrityError):
        await session.flush()


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


async def test_create_pending_payment_recovers_from_lost_race(
    session: AsyncSession, monkeypatch
) -> None:
    """The "check for existing, then insert" pattern in create_pending_payment
    is not atomic: two concurrent callers can both see "no existing row" and
    both attempt to insert the same provider_payment_id. Only one insert can
    win (UNIQUE constraint); the other must gracefully return the winner's
    row instead of propagating an unhandled IntegrityError. This test forces
    that exact race deterministically (no reliance on real concurrency
    timing) by making the very first existence check lie.
    """
    user = await repo.get_or_create_user(session, telegram_id=7, username="r")
    await session.flush()
    result = analyze("2200373")
    analysis = await repo.create_analysis(session, user_id=user.id, result=result)
    await session.commit()

    winner = await repo.create_pending_payment(
        session,
        user_id=user.id,
        analysis_id=analysis.id,
        amount=99,
        currency="RUB",
        provider="mock",
        provider_payment_id="race-id",
    )
    await session.commit()

    real_lookup = repo.get_payment_by_provider_id
    calls = {"count": 0}

    async def lying_lookup_once(session: AsyncSession, provider_payment_id: str):
        calls["count"] += 1
        if calls["count"] == 1:
            return None  # simulate the race window: "winner" not visible yet
        return await real_lookup(session, provider_payment_id)

    monkeypatch.setattr(repo, "get_payment_by_provider_id", lying_lookup_once)

    loser_result = await repo.create_pending_payment(
        session,
        user_id=user.id,
        analysis_id=analysis.id,
        amount=99,
        currency="RUB",
        provider="mock",
        provider_payment_id="race-id",
    )

    assert loser_result.id == winner.id
    # The session must still be usable afterward — the SAVEPOINT rollback
    # must not have poisoned the outer transaction.
    await repo.log_event(session, user.id, "start")
    await session.commit()


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
