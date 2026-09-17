"""Admin authorization.

Admins are the Telegram IDs in ADMIN_TELEGRAM_ID (comma-separated). The check
runs on the server for every admin message and callback — hiding the admin
button from other users is only cosmetic.
"""

from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

from app.config import settings


def is_admin_id(telegram_id: int | None) -> bool:
    return telegram_id is not None and telegram_id in settings.admin_telegram_ids


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        from_user = event.from_user
        return is_admin_id(from_user.id if from_user is not None else None)
