"""Injects a per-update AsyncSession and the current User into handler data."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from aiogram.types import User as TgUser

from app.database import repositories as repo
from app.database.session import async_session_factory


class DbSessionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with async_session_factory() as session:
            data["session"] = session

            tg_user: TgUser | None = data.get("event_from_user")
            if tg_user is not None:
                user = await repo.get_or_create_user(
                    session, telegram_id=tg_user.id, username=tg_user.username
                )
                await session.commit()
                data["user"] = user

            return await handler(event, data)
