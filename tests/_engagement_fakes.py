"""Shared helpers for leaderboard / promo / weekly automation tests.

Telegram is always faked; nothing here talks to a network.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.analysis.engine import analyze
from app.database import repositories as repo
from app.database.models import Analysis, Base, User
from app.database.session import _configure_sqlite_connection

MSK = dt.timezone(dt.timedelta(hours=3))


def msk(year: int, month: int, day: int, hour: int = 12, minute: int = 0, second: int = 0) -> dt.datetime:
    """A Moscow wall-clock moment as an aware UTC datetime."""
    return dt.datetime(year, month, day, hour, minute, second, tzinfo=MSK).astimezone(dt.UTC)


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """File-backed SQLite with the production pragmas (WAL, busy_timeout),
    so separate sessions really are separate connections.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'engagement.db').as_posix()}")
    event.listens_for(engine.sync_engine, "connect")(_configure_sqlite_connection)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def make_user(session: AsyncSession, telegram_id: int, username: str | None = None) -> User:
    user = await repo.get_or_create_user(session, telegram_id=telegram_id, username=username)
    await session.commit()
    return user


async def complete_report(session: AsyncSession, user: User, at: dt.datetime) -> Analysis:
    """A paid analysis whose report was saved at `at` (the canonical
    "completed report" the leaderboard counts).
    """
    analysis = await repo.create_analysis(session, user_id=user.id, result=analyze("2200373"))
    analysis.paid = True
    await repo.save_report(session, analysis, "report")
    analysis.report_completed_at = at
    await session.commit()
    return analysis


async def unfinished_analysis(session: AsyncSession, user: User) -> Analysis:
    analysis = await repo.create_analysis(session, user_id=user.id, result=analyze("2200373"))
    await session.commit()
    return analysis


@dataclass
class SentMessage:
    chat_id: int | str
    text: str


@dataclass
class FakeMessage:
    message_id: int


@dataclass
class FakeBot:
    """Records sends. `fail_for` maps chat_id -> exception to raise."""

    sent: list[SentMessage] = field(default_factory=list)
    fail_for: dict[int | str, BaseException] = field(default_factory=dict)

    async def send_message(self, chat_id: int | str, text: str, **_: object) -> FakeMessage:
        if chat_id in self.fail_for:
            raise self.fail_for[chat_id]
        self.sent.append(SentMessage(chat_id, text))
        return FakeMessage(message_id=1000 + len(self.sent))

    def to(self, chat_id: int | str) -> list[str]:
        return [m.text for m in self.sent if m.chat_id == chat_id]
