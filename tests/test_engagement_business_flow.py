"""Whole business cycle through the real bot handlers:

public code -> discounted purchase -> report delivered and counted ->
stats/TOP -> Monday job -> TOP-5 reward DM -> new public code, old one
deactivated -> channel post -> reward used on the next purchase.

Telegram, the payment provider and the AI model are faked.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest
from _bot_harness import Harness
from _engagement_fakes import make_user, session_factory, unfinished_analysis  # noqa: F401
from _report_fakes import valid_report_json
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import report_generator
from app.bot.handlers import payment as payment_module
from app.config import settings
from app.database.models import (
    Analysis,
    Payment,
    PromoCode,
    PromoRedemption,
    PromoType,
    RedemptionStatus,
    WeeklyLeaderboard,
)
from app.engagement import messages
from app.engagement import promo as P
from app.engagement.automation import WeeklyAutomationService
from app.engagement.periods import utcnow, week_containing
from app.payments.provider import MockPaymentProvider

USER_ID = 710001
CHANNEL = -100777


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_timezone", "+03:00")
    monkeypatch.setattr(settings, "payment_provider", "mock")
    monkeypatch.setattr(settings, "price_rub", 99)
    monkeypatch.setattr(settings, "currency", "RUB")
    monkeypatch.setattr(settings, "promo_channel_id_raw", str(CHANNEL))
    monkeypatch.setattr(settings, "admin_telegram_ids_raw", "")
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(payment_module.payment_service, "provider", MockPaymentProvider())

    async def fake_complete_chat(system_prompt: str, user_prompt: str, **kwargs: object) -> str:
        return valid_report_json("Персональный разбор.")

    monkeypatch.setattr(report_generator, "complete_chat", fake_complete_chat)


async def _buy_report(harness: Harness, factory: async_sessionmaker[AsyncSession], analysis_id: int) -> str:
    await harness.press(USER_ID, f"get_report:{analysis_id}", username="researcher")
    intro = harness.last_to(USER_ID)
    await harness.press(USER_ID, f"mock_pay:{analysis_id}", username="researcher")
    return intro


async def test_weekly_business_cycle(session_factory: async_sessionmaker[AsyncSession]) -> None:  # noqa: F811
    harness = Harness(session_factory)
    now = utcnow()
    async with session_factory() as s:
        user = await make_user(s, USER_ID, "researcher")
        first_analysis = await unfinished_analysis(s, user)
        second_analysis = await unfinished_analysis(s, user)
        public = await P.create_promo(
            s,
            code="LUCKY-OLD1",
            promo_type=PromoType.WEEKLY_PUBLIC,
            discount_percent=25,
            max_activations=100,
            valid_from=now - dt.timedelta(days=1),
            valid_until=now + dt.timedelta(days=7),
        )
        await s.commit()

    # 1. Public code -> discounted purchase -> report delivered.
    await harness.text(USER_ID, "/promo", username="researcher")
    await harness.text(USER_ID, "lucky-old1", username="researcher")
    intro = await _buy_report(harness, session_factory, first_analysis.id)
    assert "74 RUB" in intro and "<s>99</s>" in intro
    assert "Персональный разбор." in harness.last_to(USER_ID)

    async with session_factory() as s:
        payment = (await s.execute(select(Payment).where(Payment.analysis_id == first_analysis.id))).scalar_one()
        assert (payment.amount, payment.status) == (74, "paid")
        redemption = (await s.execute(select(PromoRedemption))).scalar_one()
        assert redemption.status == RedemptionStatus.CONSUMED
        counted = await s.get(Analysis, first_analysis.id)
        assert counted is not None and counted.report_completed_at is not None

    # 2. Counted in statistics and the live TOP.
    await harness.text(USER_ID, "/stats", username="researcher")
    assert "Разборов всего: 1" in harness.last_to(USER_ID)
    await harness.text(USER_ID, "/top", username="researcher")
    assert "1. 🥇 @researcher — 1" in harness.last_to(USER_ID)

    # 3. A week passes: move the purchase into last week (instead of moving
    #    the clock, because code validity is checked against the real clock),
    #    then the scheduled job finalizes that week.
    async with session_factory() as s:
        counted = await s.get(Analysis, first_analysis.id)
        assert counted is not None and counted.report_completed_at is not None
        counted.report_completed_at = week_containing(now).start - dt.timedelta(days=3)
        await s.commit()
    monday = utcnow()
    report = await WeeklyAutomationService(session_factory, harness.bot).run(monday)
    assert report.ok, report.errors
    assert report.entries == 1 and report.rewards.sent == 1

    # 4. The winner receives a personal 50% code.
    reward_dm = harness.last_to(USER_ID)
    assert "TOP-5" in reward_dm
    reward_code = re.search(r"TOP50-[A-Z0-9]+", reward_dm)
    assert reward_code is not None

    # 5. New public code; the old one is deactivated; channel post generated.
    async with session_factory() as s:
        old = await s.get(PromoCode, public.id)
        assert old is not None and old.is_active is False
        new_public = (
            await s.execute(select(PromoCode).where(PromoCode.promo_type == PromoType.WEEKLY_PUBLIC, PromoCode.is_active.is_(True)))
        ).scalar_one()
        assert new_public.code == report.public_promo_code
        leaderboard = (await s.execute(select(WeeklyLeaderboard))).scalar_one()
        assert leaderboard.channel_status == "sent"
    post = harness.last_to(CHANNEL)
    assert new_public.code in post and "@researcher" in post

    # 6. The old public code no longer works; the reward code does.
    await harness.text(USER_ID + 1, "/promo", username="newcomer")
    await harness.text(USER_ID + 1, "LUCKY-OLD1", username="newcomer")
    assert harness.last_to(USER_ID + 1) == messages.PROMO_INVALID
    await harness.text(USER_ID, "/promo", username="researcher")
    await harness.text(USER_ID, reward_code.group(0).lower(), username="researcher")
    assert "Скидка 50%" in harness.last_to(USER_ID)
    intro = await _buy_report(harness, session_factory, second_analysis.id)
    assert "49 RUB" in intro

    # 7. The finalized snapshot stays as it was after new reports.
    report_again = await WeeklyAutomationService(session_factory, harness.bot).run(monday + dt.timedelta(hours=1))
    assert not report_again.finalized_now and report_again.rewards.sent == 0
