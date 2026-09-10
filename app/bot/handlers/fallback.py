"""Catch-all for messages that no other handler matched.

Must be included LAST in the router tree (see build_root_router) so it only
fires once every more specific handler (commands, FSM-scoped input, etc.)
has had a chance to match.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.types import Message

from app.bot import texts

router = Router(name="fallback")


@router.message()
async def handle_unrecognized_message(message: Message) -> None:
    await message.answer(texts.UNKNOWN_MESSAGE)
