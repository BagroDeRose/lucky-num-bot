"""User and admin bot flows driven through a real aiogram Dispatcher and the
production router tree. Only the Telegram HTTP session is fake.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pytest
from _bot_harness import Harness
from _engagement_fakes import complete_report, make_user, session_factory  # noqa: F401
from aiogram.methods import SendMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.admin_access import IsAdmin, is_admin_id
from app.bot.handlers import admin as admin_handlers
from app.bot.keyboards.main import main_menu_kb
from app.config import settings
from app.database.models import PromoCode, PromoType, WeeklyLeaderboard
from app.engagement import messages
from app.engagement import promo as P
from app.engagement.periods import utcnow

ADMIN_ID = 900001
USER_ID = 700001
OTHER_ID = 700002


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch, session_factory: async_sessionmaker[AsyncSession]) -> None:  # noqa: F811
    monkeypatch.setattr(settings, "admin_telegram_ids_raw", str(ADMIN_ID))
    monkeypatch.setattr(settings, "app_timezone", "+03:00")
    monkeypatch.setattr(settings, "promo_channel_id_raw", "")
    monkeypatch.setattr(admin_handlers, "session_factory", session_factory)


@pytest.fixture
def harness(session_factory: async_sessionmaker[AsyncSession]) -> Harness:  # noqa: F811
    return Harness(session_factory)


async def _live_promo(factory: async_sessionmaker[AsyncSession], code: str, **kwargs: Any) -> PromoCode:
    params: dict[str, Any] = {
        "promo_type": PromoType.WEEKLY_PUBLIC,
        "discount_percent": 25,
        "max_activations": 100,
        "valid_from": utcnow() - dt.timedelta(days=1),
        "valid_until": utcnow() + dt.timedelta(days=6),
    } | kwargs
    async with factory() as s:
        promo = await P.create_promo(s, code=code, **params)
        await s.commit()
        return promo


# --- admin authorization -------------------------------------------------------


def test_admin_ids_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_telegram_ids_raw", " 11, 22 ,abc,, -5")
    assert settings.admin_telegram_ids == frozenset({11, 22, -5})
    assert is_admin_id(22) and not is_admin_id(33) and not is_admin_id(None)
    monkeypatch.setattr(settings, "admin_telegram_ids_raw", "")
    assert settings.admin_telegram_ids == frozenset()


def test_admin_router_is_filtered_server_side() -> None:
    assert any(isinstance(f.callback, IsAdmin) for f in admin_handlers.router.message._handler.filters or [])
    assert any(isinstance(f.callback, IsAdmin) for f in admin_handlers.router.callback_query._handler.filters or [])


def test_admin_button_only_for_admins() -> None:
    def datas(markup: Any) -> list[str]:
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    assert "adm:menu" in datas(main_menu_kb(is_admin=True))
    assert "adm:menu" not in datas(main_menu_kb(is_admin=False))
    assert {"my_stats", "top", "promo"} <= set(datas(main_menu_kb()))


async def test_non_admin_admin_command_denied(harness: Harness) -> None:
    await harness.text(USER_ID, "/admin")
    assert harness.last_to(USER_ID) == admin_handlers.ACCESS_DENIED


async def test_admin_command_opens_panel(harness: Harness) -> None:
    await harness.text(ADMIN_ID, "/admin")
    assert harness.last_to(ADMIN_ID) == admin_handlers.ADMIN_MENU


async def test_non_admin_cannot_run_weekly_job_with_forged_callback(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_: Any) -> Any:
        raise AssertionError("weekly job must not be reachable by a non-admin")

    monkeypatch.setattr(admin_handlers, "automation_service", forbidden)
    for data in ("adm:run_go", "adm:pd:1", "adm:rw:1", "adm:stats", "adm:msg_go"):
        await harness.press(USER_ID, data)
    assert harness.fake.alerts().count(admin_handlers.ACCESS_DENIED) == 5
    assert harness.fake.texts_to(USER_ID) == []


async def test_former_admin_mid_flow_is_denied(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    await harness.press(ADMIN_ID, "adm:new")
    monkeypatch.setattr(settings, "admin_telegram_ids_raw", "")  # access revoked
    await harness.text(ADMIN_ID, "HACK-CODE")
    assert harness.last_to(ADMIN_ID) == admin_handlers.ACCESS_DENIED


async def test_admin_unrecognized_input_mid_dialog_is_not_denied(harness: Harness) -> None:
    await harness.press(ADMIN_ID, "adm:new")
    await harness.text(ADMIN_ID, "/unknown")
    assert harness.last_to(ADMIN_ID) != admin_handlers.ACCESS_DENIED


# --- admin features ---------------------------------------------------------------


async def test_admin_creates_promo_through_dialog(
    harness: Harness, session_factory: async_sessionmaker[AsyncSession]  # noqa: F811
) -> None:
    await harness.press(ADMIN_ID, "adm:new")
    await harness.text(ADMIN_ID, "spring-sale")
    await harness.text(ADMIN_ID, "150")  # invalid percent is re-asked
    assert "от 1 до 99" in harness.last_to(ADMIN_ID)
    await harness.text(ADMIN_ID, "30")
    await harness.text(ADMIN_ID, "20")
    await harness.text(ADMIN_ID, "10")
    assert "Промокод создан" in harness.last_to(ADMIN_ID)

    async with session_factory() as s:
        promo = (await s.execute(select(PromoCode))).scalar_one()
    assert (promo.code, promo.promo_type, promo.discount_percent, promo.max_activations) == (
        "SPRING-SALE", PromoType.ADMIN_MANUAL, 30, 20,
    )
    assert promo.created_by_telegram_id == ADMIN_ID


async def test_admin_grants_personal_code_and_user_receives_it(
    harness: Harness, session_factory: async_sessionmaker[AsyncSession]  # noqa: F811
) -> None:
    async with session_factory() as s:
        await make_user(s, USER_ID, "Lucky_User")
    await harness.press(ADMIN_ID, "adm:grant")
    await harness.text(ADMIN_ID, "@lucky_user")  # case-insensitive lookup
    await harness.text(ADMIN_ID, "40")
    await harness.text(ADMIN_ID, "5")

    dm = harness.last_to(USER_ID)
    async with session_factory() as s:
        promo = (await s.execute(select(PromoCode))).scalar_one()
    assert promo.code in dm and "40%" in dm
    assert promo.max_activations == 1 and promo.target_user_id is not None
    assert "получил сообщение" in harness.last_to(ADMIN_ID)

    await harness.text(OTHER_ID, "/promo", username="someone")
    await harness.text(OTHER_ID, promo.code, username="someone")
    assert harness.last_to(OTHER_ID) == messages.PROMO_NOT_FOR_YOU


async def test_grant_dm_failure_keeps_code_and_reports_reason(
    harness: Harness, session_factory: async_sessionmaker[AsyncSession]  # noqa: F811
) -> None:
    async with session_factory() as s:
        await make_user(s, USER_ID, "blocked_user")
    harness.fake.fail_for[USER_ID] = "Bad Request: chat not found"
    await harness.press(ADMIN_ID, "adm:grant")
    await harness.text(ADMIN_ID, "@blocked_user")
    await harness.text(ADMIN_ID, "40")
    await harness.text(ADMIN_ID, "5")

    report = harness.last_to(ADMIN_ID)
    assert "не доставлено" in report and "chat not found" in report
    async with session_factory() as s:
        promo = (await s.execute(select(PromoCode))).scalar_one()
    assert promo.code in report and promo.is_active


async def test_admin_deactivates_promo(
    harness: Harness, session_factory: async_sessionmaker[AsyncSession]  # noqa: F811
) -> None:
    promo = await _live_promo(session_factory, "OFF-SOON")
    await harness.press(ADMIN_ID, f"adm:pd:{promo.id}")
    async with session_factory() as s:
        assert (await s.get(PromoCode, promo.id)).is_active is False  # type: ignore[union-attr]


async def test_admin_statistics(
    harness: Harness, session_factory: async_sessionmaker[AsyncSession]  # noqa: F811
) -> None:
    async with session_factory() as s:
        user = await make_user(s, USER_ID, "a")
        await complete_report(s, user, utcnow() - dt.timedelta(minutes=5))
    await harness.press(ADMIN_ID, "adm:stats")
    text = harness.last_to(ADMIN_ID)
    assert "Пользователей: 2" in text  # the user + the admin who pressed
    assert "Полных разборов: 1" in text


async def test_admin_manual_weekly_run_uses_same_service(
    harness: Harness, session_factory: async_sessionmaker[AsyncSession]  # noqa: F811
) -> None:
    async with session_factory() as s:
        user = await make_user(s, USER_ID, "winner")
        await complete_report(s, user, utcnow() - dt.timedelta(days=7))
    await harness.press(ADMIN_ID, "adm:run_go")
    assert "Недельная задача" in harness.last_to(ADMIN_ID)
    async with session_factory() as s:
        assert (await s.execute(select(WeeklyLeaderboard))).scalar_one() is not None

    await harness.press(ADMIN_ID, "adm:hist:0")
    history = harness.last_to(ADMIN_ID)
    assert "Награды TOP-5" in history and "@winner" in history


async def _send_admin_message(
    harness: Harness, factory: async_sessionmaker[AsyncSession], body: str
) -> None:
    async with factory() as s:
        await make_user(s, USER_ID, "target")
    await harness.press(ADMIN_ID, "adm:msg")
    await harness.text(ADMIN_ID, str(USER_ID))
    await harness.text(ADMIN_ID, body)
    await harness.press(ADMIN_ID, "adm:msg_go")


def _sends_to(harness: Harness, chat_id: int) -> list[SendMessage]:
    return [m for m in harness.fake.requests if isinstance(m, SendMessage) and m.chat_id == chat_id]


async def test_admin_message_is_delivered_as_plain_text_without_markup(
    harness: Harness, session_factory: async_sessionmaker[AsyncSession]  # noqa: F811
) -> None:
    await _send_admin_message(harness, session_factory, "<b>hi</b> & bye")
    (sent,) = _sends_to(harness, USER_ID)
    assert sent.text == "<b>hi</b> & bye"
    assert sent.parse_mode is None  # no HTML parsing: markup cannot be injected
    assert harness.last_to(ADMIN_ID).startswith("✅")


async def test_admin_message_at_length_limit_with_html_characters_is_delivered(
    harness: Harness, session_factory: async_sessionmaker[AsyncSession]  # noqa: F811
) -> None:
    """Regression: the text used to be validated at <=3500 chars but sent
    HTML-escaped ("<" -> "&lt;"), so it could exceed Telegram's 4096 limit and
    fail with TelegramBadRequest.
    """
    body = ("Q&A: 5 < 6 > 4. " * 400)[: admin_handlers.MAX_MESSAGE_LENGTH].strip()
    await _send_admin_message(harness, session_factory, body)
    assert harness.fake.failed == []
    (sent,) = _sends_to(harness, USER_ID)
    assert sent.text == body
    assert harness.last_to(ADMIN_ID) == "✅ Отправлено."


async def test_admin_message_over_limit_is_rejected_before_sending(
    harness: Harness, session_factory: async_sessionmaker[AsyncSession]  # noqa: F811
) -> None:
    await _send_admin_message(harness, session_factory, "x" * (admin_handlers.MAX_MESSAGE_LENGTH + 1))
    assert _sends_to(harness, USER_ID) == []
    assert harness.fake.failed == []
    assert any("символов" in text for text in harness.fake.texts_to(ADMIN_ID))


async def test_admin_message_failure_reports_telegram_reason(
    harness: Harness,
    session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness.fake.fail_for[USER_ID] = "Bad Request: chat not found"
    with caplog.at_level(logging.INFO, logger="app.bot.handlers.admin"):
        await _send_admin_message(harness, session_factory, "hello")
    assert "chat not found" in harness.last_to(ADMIN_ID)
    assert "chat not found" in caplog.text
    assert "messaged user_id" not in caplog.text  # no success log after a failure


# --- user screens -------------------------------------------------------------------


async def test_user_stats_and_top(
    harness: Harness, session_factory: async_sessionmaker[AsyncSession]  # noqa: F811
) -> None:
    async with session_factory() as s:
        me = await make_user(s, USER_ID, "me")
        other = await make_user(s, OTHER_ID, None)
        await complete_report(s, me, utcnow() - dt.timedelta(minutes=2))
        await complete_report(s, other, utcnow() - dt.timedelta(minutes=3))
        await complete_report(s, other, utcnow() - dt.timedelta(minutes=1))

    await harness.text(USER_ID, "/stats", username="me")
    assert "Разборов всего: 1" in harness.last_to(USER_ID)

    await harness.press(USER_ID, "top", username="me")
    top = harness.last_to(USER_ID)
    assert messages.ANONYMOUS_RESEARCHER in top
    assert "Ваша позиция: #2" in top
    assert str(OTHER_ID) not in top


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("live", "активирован"),
        ("expired", messages.PROMO_INVALID),
        ("exhausted", messages.PROMO_EXHAUSTED),
    ],
)
async def test_user_promo_entry_outcomes(
    harness: Harness,
    session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
    setup: str,
    expected: str,
) -> None:
    if setup == "live":
        await _live_promo(session_factory, "LUCKY-7K2P")
    elif setup == "expired":
        await _live_promo(
            session_factory, "LUCKY-7K2P",
            valid_from=utcnow() - dt.timedelta(days=9), valid_until=utcnow() - dt.timedelta(days=2),
        )
    else:
        await _live_promo(session_factory, "LUCKY-7K2P", max_activations=1)
        async with session_factory() as s:
            first = await make_user(s, OTHER_ID, "first")
            await P.activate(s, first, "LUCKY-7K2P")
            await s.commit()

    await harness.press(USER_ID, "promo")
    assert harness.last_to(USER_ID) == messages.ASK_PROMO_CODE
    await harness.text(USER_ID, "lucky-7k2p")
    assert expected in harness.last_to(USER_ID)


async def test_user_promo_already_used_and_retry_after_typo(
    harness: Harness, session_factory: async_sessionmaker[AsyncSession]  # noqa: F811
) -> None:
    await _live_promo(session_factory, "LUCKY-7K2P")
    await harness.text(USER_ID, "/promo")
    await harness.text(USER_ID, "LUCKY-TYPO")
    assert harness.last_to(USER_ID) == messages.PROMO_INVALID
    await harness.text(USER_ID, "LUCKY-7K2P")  # still in input state after a typo
    assert "Скидка 25%" in harness.last_to(USER_ID)
    await harness.text(USER_ID, "/promo")
    await harness.text(USER_ID, "LUCKY-7K2P")
    assert harness.last_to(USER_ID) == messages.PROMO_ALREADY_USED


async def test_start_escapes_promo_input_state(harness: Harness) -> None:
    await harness.text(USER_ID, "/promo")
    await harness.text(USER_ID, "/start")
    await harness.text(USER_ID, "hello")
    assert harness.last_to(USER_ID) != messages.PROMO_INVALID
