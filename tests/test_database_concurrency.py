"""Regression coverage for a real runtime failure: "database is locked"
during normal concurrent Telegram updates (e.g. two handlers each writing
an Event row at nearly the same moment).

SQLite's default rollback-journal mode takes an exclusive lock on the whole
file for the duration of a write, and the default busy_timeout is 0ms, so a
second concurrent writer fails immediately instead of waiting. This test
uses a real file-based SQLite database (not :memory:, since WAL mode and
file locking semantics don't meaningfully apply to :memory: databases) with
the exact same connection setup as app.database.session, and proves that
concurrent writers no longer collide.

This does not "fix" SQLite's single-writer nature — it only makes the local/
test experience match real SQLite behavior more closely. PostgreSQL is the
intended production database (see README "PostgreSQL (production)").
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import repositories as repo
from app.database.models import Analysis, Base, Payment, User
from app.database.session import _configure_sqlite_connection


@pytest_asyncio.fixture
async def file_engine(tmp_path: Path) -> AsyncIterator[object]:
    db_path = tmp_path / "concurrency_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listens_for(engine.sync_engine, "connect")(_configure_sqlite_connection)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


async def test_sqlite_pragmas_are_actually_applied(file_engine) -> None:
    """Verifies the effect of _configure_sqlite_connection directly against
    the database, not just that it runs without error.
    """
    async with file_engine.connect() as conn:
        journal_mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
        foreign_keys = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
        busy_timeout = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()

    assert journal_mode == "wal"
    assert foreign_keys == 1
    assert busy_timeout == 5000


async def test_concurrent_writers_do_not_hit_database_locked(file_engine) -> None:
    """Several handlers writing Event rows for the same user at once — the
    exact shape of the reported failure (start / payment_clicked /
    analyze_again logged in close succession) — must all succeed.
    """
    session_factory = async_sessionmaker(file_engine, expire_on_commit=False)

    async with session_factory() as setup_session:
        user = await repo.get_or_create_user(setup_session, telegram_id=1, username="u")
        await setup_session.commit()
        user_id = user.id

    async def write_event(name: str) -> None:
        async with session_factory() as session:
            await repo.log_event(session, user_id, name)
            await session.commit()

    event_names = ["start", "payment_clicked", "analyze_again"] * 5

    # asyncio.gather schedules these concurrently; each opens its own
    # connection/transaction, reproducing genuinely overlapping writes.
    await asyncio.gather(*(write_event(name) for name in event_names))

    async with session_factory() as verify_session:
        stats = await repo.funnel_stats(verify_session)

    assert stats["start"] == 5
    assert stats["payment_clicked"] == 5
    assert stats["analyze_again"] == 5


async def test_concurrent_writers_across_different_users(file_engine) -> None:
    """Simulates multiple distinct Telegram users interacting at once."""
    session_factory = async_sessionmaker(file_engine, expire_on_commit=False)

    async def onboard_and_log(telegram_id: int) -> None:
        async with session_factory() as session:
            user = await repo.get_or_create_user(
                session, telegram_id=telegram_id, username=f"user{telegram_id}"
            )
            await session.commit()
            await repo.log_event(session, user.id, "start")
            await session.commit()

    await asyncio.gather(*(onboard_and_log(tg_id) for tg_id in range(1, 11)))

    async with session_factory() as verify_session:
        stats = await repo.funnel_stats(verify_session)

    assert stats["start"] == 10


@pytest.mark.parametrize("_", range(3))
async def test_unique_provider_payment_id_still_enforced_under_concurrency(
    file_engine, _
) -> None:
    """Idempotency guarantees (unique provider_payment_id) must hold even
    with WAL mode + busy_timeout enabled — this isn't loosening any
    constraint, just reducing false-positive lock failures.
    """
    session_factory = async_sessionmaker(file_engine, expire_on_commit=False)
    shared_id = f"dup-{uuid.uuid4()}"

    async def try_create() -> object:
        async with session_factory() as session:
            user = await repo.get_or_create_user(session, telegram_id=999, username="u")
            await session.flush()
            from app.analysis.engine import analyze

            analysis = await repo.create_analysis(session, user_id=user.id, result=analyze("2200373"))
            await session.commit()
            payment = await repo.create_pending_payment(
                session,
                user_id=user.id,
                analysis_id=analysis.id,
                amount=99,
                currency="RUB",
                provider="mock",
                provider_payment_id=shared_id,
            )
            await session.commit()
            return payment.id

    ids = await asyncio.gather(*(try_create() for _ in range(3)))
    assert len(set(ids)) == 1  # all three calls resolved to the same row


async def test_concurrent_mark_payment_paid_transitions_exactly_once(file_engine) -> None:
    """Regression test for a real race: two concurrent confirmations of the
    same payment (e.g. a rapid double-tap on "Я оплатил, проверить статус",
    each dispatched as its own update/session) must not both report having
    performed the pending->paid transition — that would double-log
    payment_success and risk double-delivering the paid report. Only one of
    N concurrent callers may see transitioned=True.
    """
    from app.analysis.engine import analyze

    session_factory = async_sessionmaker(file_engine, expire_on_commit=False)

    async with session_factory() as setup_session:
        user = await repo.get_or_create_user(setup_session, telegram_id=777, username="u")
        await setup_session.flush()
        analysis = await repo.create_analysis(setup_session, user_id=user.id, result=analyze("2200373"))
        await setup_session.commit()
        payment = await repo.create_pending_payment(
            setup_session,
            user_id=user.id,
            analysis_id=analysis.id,
            amount=99,
            currency="RUB",
            provider="mock",
            provider_payment_id=f"race-{uuid.uuid4()}",
        )
        await setup_session.commit()
        payment_id = payment.id

    # Pre-load N independent sessions/rows, each still seeing PENDING, before
    # any of them attempts the transition — this is what actually reproduces
    # the race (two handlers that each already loaded the row, then racing
    # to confirm), as opposed to loading-then-confirming one at a time,
    # which lets SQLite's single-writer lock serialize the reads themselves
    # and never exposes the bug.
    loaded: list[tuple[AsyncSession, Payment]] = []
    for _ in range(5):
        session = session_factory()
        row = await session.get(Payment, payment_id)
        assert row is not None
        loaded.append((session, row))

    async def try_confirm(session: AsyncSession, row: Payment) -> bool:
        try:
            transitioned = await repo.mark_payment_paid(session, row)
            await session.commit()
            return transitioned
        finally:
            await session.close()

    outcomes = await asyncio.gather(*(try_confirm(s, r) for s, r in loaded))
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 4


async def test_concurrent_report_delivery_does_not_double_call_openai(file_engine, monkeypatch) -> None:
    """Regression test for a real race distinct from the payment-confirm one
    above: two concurrent deliveries of the report for the same *already
    paid* analysis (e.g. a rapid double-tap on "Открыть полный разбор" on an
    already-paid analysis, or on "Попробовать ещё раз") can each see
    analysis.report as still empty before either commits, and both proceed
    to call OpenAI and save — doubling real AI spend and sending the report
    to the user twice. Only one of N concurrent callers may trigger
    generation; the rest must fall back to the winner's result instead.
    """
    from app.ai import report_generator
    from app.analysis.engine import analyze
    from app.bot.handlers.payment import _deliver_report
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "test-key")

    call_count = {"n": 0}

    async def fake_complete_chat(system_prompt: str, user_prompt: str) -> str:
        call_count["n"] += 1
        await asyncio.sleep(0.05)  # widen the race window past the DB round-trips
        return "Готовый отчёт."

    monkeypatch.setattr(report_generator, "complete_chat", fake_complete_chat)

    session_factory = async_sessionmaker(file_engine, expire_on_commit=False)

    async with session_factory() as setup_session:
        user = await repo.get_or_create_user(setup_session, telegram_id=888, username="u")
        await setup_session.flush()
        analysis = await repo.create_analysis(setup_session, user_id=user.id, result=analyze("2200373"))
        await repo.mark_analysis_paid(setup_session, analysis)
        await setup_session.commit()
        analysis_id = analysis.id
        user_id = user.id

    # Pre-load N independent sessions/rows, each still seeing report=None,
    # before any of them attempts delivery — same technique as the
    # mark_payment_paid race above, required to actually expose the bug
    # rather than let SQLite's single-writer lock serialize the reads.
    loaded: list[tuple[AsyncSession, Analysis, User]] = []
    for _ in range(3):
        session = session_factory()
        row = await session.get(Analysis, analysis_id)
        user_row = await session.get(User, user_id)
        assert row is not None
        assert user_row is not None
        loaded.append((session, row, user_row))

    sent_texts: list[str] = []

    async def try_deliver(session: AsyncSession, row: Analysis, user_row: User) -> None:
        async def send(text: str, kb: object) -> None:
            sent_texts.append(text)

        try:
            await _deliver_report(send, session, row, user_row)
        finally:
            await session.close()

    await asyncio.gather(*(try_deliver(s, r, u) for s, r, u in loaded))

    assert call_count["n"] == 1, f"OpenAI was called {call_count['n']} times for one report"
    assert sent_texts.count("Готовый отчёт.") == len(loaded)
