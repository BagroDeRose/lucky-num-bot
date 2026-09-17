"""A real aiogram Dispatcher with the production router tree, a fake
Telegram HTTP session and a test-database session middleware.
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, TelegramObject, Update
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.handlers import build_root_router
from app.database import repositories as repo

TELEGRAM_TEXT_LIMIT = 4096


class FakeSession(BaseSession):
    """Records delivered requests. Mirrors Telegram's own rejection of texts
    over 4096 characters, and can fail sends to chosen chats (`fail_for`).
    Rejected requests go to `failed`, not `requests`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self.failed: list[TelegramMethod[Any]] = []
        self.fail_for: dict[int, str] = {}
        self._ids = itertools.count(100)

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        if isinstance(method, SendMessage | EditMessageText):
            if len(method.text) > TELEGRAM_TEXT_LIMIT:
                self.failed.append(method)
                raise TelegramBadRequest(method, "Bad Request: message is too long")
            if method.chat_id in self.fail_for:
                self.failed.append(method)
                raise TelegramBadRequest(method, self.fail_for[int(method.chat_id)])
        self.requests.append(method)
        if isinstance(method, SendMessage | EditMessageText):
            chat_id = method.chat_id if method.chat_id is not None else 0
            return Message(
                message_id=next(self._ids),
                date=dt.datetime.now(dt.UTC),
                chat=Chat(id=int(chat_id), type="private"),
                text=method.text,
            )
        return True

    async def close(self) -> None:
        return None

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    def texts_to(self, chat_id: int) -> list[str]:
        return [
            m.text
            for m in self.requests
            if isinstance(m, SendMessage | EditMessageText) and m.chat_id == chat_id
        ]

    def alerts(self) -> list[str]:
        return [m.text or "" for m in self.requests if isinstance(m, AnswerCallbackQuery)]


class FakeDbMiddleware(BaseMiddleware):
    """Same contract as DbSessionMiddleware, bound to the test database."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self.factory() as session:
            data["session"] = session
            tg_user = data.get("event_from_user")
            if tg_user is not None:
                data["user"] = await repo.get_or_create_user(
                    session, telegram_id=tg_user.id, username=tg_user.username
                )
                await session.commit()
            return await handler(event, data)


_ROOT: list[Any] = []


def _root_router() -> Any:
    """Handler routers are module singletons and can be attached only once,
    so the production tree is built once and re-parented per test.
    """
    if not _ROOT:
        _ROOT.append(build_root_router())
    _ROOT[0]._parent_router = None
    return _ROOT[0]


class Harness:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.fake = FakeSession()
        self.bot = Bot("123456:TEST", session=self.fake)
        self.dp = Dispatcher()
        self.dp.update.middleware(FakeDbMiddleware(factory))
        self.dp.include_router(_root_router())
        self._update_ids = itertools.count(1)

    def _tg_user(self, telegram_id: int, username: str | None) -> TgUser:
        return TgUser(id=telegram_id, is_bot=False, first_name="T", username=username)

    async def text(self, telegram_id: int, text: str, username: str | None = "tester") -> None:
        message = Message(
            message_id=next(self._update_ids),
            date=dt.datetime.now(dt.UTC),
            chat=Chat(id=telegram_id, type="private"),
            from_user=self._tg_user(telegram_id, username),
            text=text,
        )
        await self.dp.feed_update(self.bot, Update(update_id=next(self._update_ids), message=message))

    async def press(self, telegram_id: int, data: str, username: str | None = "tester") -> None:
        message = Message(
            message_id=next(self._update_ids),
            date=dt.datetime.now(dt.UTC),
            chat=Chat(id=telegram_id, type="private"),
            text="menu",
        )
        callback = CallbackQuery(
            id=str(next(self._update_ids)),
            from_user=self._tg_user(telegram_id, username),
            chat_instance="ci",
            data=data,
            message=message,
        )
        await self.dp.feed_update(
            self.bot, Update(update_id=next(self._update_ids), callback_query=callback)
        )

    def last_to(self, chat_id: int) -> str:
        texts = self.fake.texts_to(chat_id)
        assert texts, f"nothing was sent to {chat_id}"
        return texts[-1]
