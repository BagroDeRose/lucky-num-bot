"""Startup check of PROMO_CHANNEL_ID (read-only Telegram calls, never posts).

A wrong channel ID, or a bot that is not an administrator of the channel,
used to surface only when the weekly post failed with "Bad Request: chat not
found". Checking once at startup turns that into an immediate, explicit
warning. The bot still starts: the channel post is optional and everything
else works without it.
"""

from __future__ import annotations

from typing import Any, Protocol

from aiogram.enums import ChatMemberStatus, ChatType

from app.config import settings
from app.logging import get_logger

logger = get_logger(__name__)


class ChannelBot(Protocol):
    async def get_me(self) -> Any: ...
    async def get_chat(self, chat_id: int | str) -> Any: ...
    async def get_chat_member(self, chat_id: int | str, user_id: int) -> Any: ...


async def check_promo_channel(bot: ChannelBot) -> str | None:
    """Return None if the bot can post to PROMO_CHANNEL_ID (or no channel is
    configured), otherwise a human-readable problem, which is also logged.
    """
    raw = settings.promo_channel_id_raw.strip()
    channel_id = settings.promo_channel_id
    if not raw:
        return None
    problem: str | None
    if channel_id is None:
        problem = (
            f"PROMO_CHANNEL_ID={raw!r} is not a numeric chat ID (e.g. -1001234567890) "
            "or a public @channelname"
        )
    else:
        problem = await _probe(bot, channel_id)
    if problem:
        logger.warning("Weekly channel post will fail: %s", problem)
    return problem


async def _probe(bot: ChannelBot, channel_id: int | str) -> str | None:
    try:
        me = await bot.get_me()
        chat = await bot.get_chat(channel_id)
    except Exception as exc:
        return (
            f"Telegram cannot access PROMO_CHANNEL_ID={channel_id} ({type(exc).__name__}: {exc}). "
            "Check the ID and add the bot to the channel as an administrator "
            'with the "Post messages" right.'
        )
    if chat.type != ChatType.CHANNEL:
        return f"PROMO_CHANNEL_ID={channel_id} is a {chat.type}, not a channel"
    try:
        member = await bot.get_chat_member(channel_id, me.id)
    except Exception as exc:
        return f"cannot read the bot's rights in PROMO_CHANNEL_ID={channel_id} ({type(exc).__name__}: {exc})"
    if member.status == ChatMemberStatus.CREATOR:
        return None
    if member.status != ChatMemberStatus.ADMINISTRATOR or not getattr(member, "can_post_messages", False):
        return (
            f"the bot is {member.status} in PROMO_CHANNEL_ID={channel_id} without the "
            '"Post messages" right; make it an administrator with that right'
        )
    return None
