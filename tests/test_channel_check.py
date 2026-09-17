"""Startup check of PROMO_CHANNEL_ID (the production channel post failed with
"Bad Request: chat not found"). Telegram is faked."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChat

from app.config import settings
from app.engagement.channel_check import check_promo_channel

BOT_ID = 8829820499


class FakeBot:
    def __init__(self, chat: Any = None, member: Any = None, error: Exception | None = None) -> None:
        self.chat, self.member, self.error = chat, member, error
        self.calls: list[str] = []

    async def get_me(self) -> Any:
        return SimpleNamespace(id=BOT_ID)

    async def get_chat(self, chat_id: int | str) -> Any:
        self.calls.append("get_chat")
        if self.error:
            raise self.error
        return self.chat

    async def get_chat_member(self, chat_id: int | str, user_id: int) -> Any:
        assert user_id == BOT_ID
        return self.member


def _channel() -> Any:
    return SimpleNamespace(type="channel")


@pytest.fixture(autouse=True)
def _channel_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "promo_channel_id_raw", "-1004466147961")


async def test_chat_not_found_is_reported_at_startup(caplog: pytest.LogCaptureFixture) -> None:
    error = TelegramBadRequest(GetChat(chat_id=-1004466147961), "Bad Request: chat not found")
    with caplog.at_level(logging.WARNING):
        problem = await check_promo_channel(FakeBot(error=error))
    assert problem is not None and "chat not found" in problem and "administrator" in problem
    assert "Weekly channel post will fail" in caplog.text


async def test_admin_with_post_right_is_ok() -> None:
    member = SimpleNamespace(status="administrator", can_post_messages=True)
    assert await check_promo_channel(FakeBot(_channel(), member)) is None


@pytest.mark.parametrize(
    "member",
    [
        SimpleNamespace(status="administrator", can_post_messages=False),
        SimpleNamespace(status="member"),
        SimpleNamespace(status="left"),
    ],
)
async def test_bot_without_post_right_is_reported(member: Any) -> None:
    problem = await check_promo_channel(FakeBot(_channel(), member))
    assert problem is not None and "Post messages" in problem


async def test_group_instead_of_channel_is_reported() -> None:
    problem = await check_promo_channel(FakeBot(SimpleNamespace(type="group"), None))
    assert problem is not None and "not a channel" in problem


async def test_not_configured_makes_no_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "promo_channel_id_raw", "")
    bot = FakeBot()
    assert await check_promo_channel(bot) is None
    assert bot.calls == []


async def test_malformed_id_is_reported_without_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "promo_channel_id_raw", "my channel")
    bot = FakeBot()
    problem = await check_promo_channel(bot)
    assert problem is not None and "not a numeric chat ID" in problem
    assert bot.calls == []
