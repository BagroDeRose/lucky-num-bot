"""Promo discounts inside the payment flow: price, reservation lifecycle,
no stacking, provider failure, failed payment, reused pending payment.
"""

from __future__ import annotations

import datetime as dt

import pytest
from _engagement_fakes import make_user, unfinished_analysis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.config import settings
from app.database import repositories as repo
from app.database.models import PromoRedemption, PromoType, RedemptionStatus
from app.engagement import promo as P
from app.engagement.periods import utcnow
from app.payments.provider import MockPaymentProvider
from app.payments.service import PaymentService


@pytest.fixture(autouse=True)
def _price(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "price_rub", 99)
    monkeypatch.setattr(settings, "currency", "RUB")
    monkeypatch.setattr(settings, "payment_provider", "mock")


def _service() -> PaymentService:
    service = PaymentService()
    service.provider = MockPaymentProvider()
    return service


async def _activated(session: AsyncSession, user, code: str, percent: int) -> None:  # type: ignore[no-untyped-def]
    now = utcnow()
    await P.create_promo(
        session,
        code=code,
        promo_type=PromoType.ADMIN_MANUAL,
        discount_percent=percent,
        max_activations=10,
        valid_from=now - dt.timedelta(days=1),
        valid_until=now + dt.timedelta(days=5),
    )
    result = await P.activate(session, user, code)
    assert result.outcome is P.ActivationOutcome.ACTIVATED
    await session.commit()


async def _redemptions(session: AsyncSession) -> list[PromoRedemption]:
    return list((await session.execute(select(PromoRedemption).order_by(PromoRedemption.id))).scalars().all())


async def test_no_promo_keeps_base_price(session: AsyncSession) -> None:
    user = await make_user(session, 1, "a")
    analysis = await unfinished_analysis(session, user)
    payment, intent = await _service().start_payment(session, user_id=user.id, analysis=analysis)
    assert payment.amount == intent.amount == 99


async def test_discount_applied_and_consumed_on_success(session: AsyncSession) -> None:
    user = await make_user(session, 1, "a")
    await _activated(session, user, "WEEK-25", 25)
    analysis = await unfinished_analysis(session, user)
    service = _service()

    payment, intent = await service.start_payment(session, user_id=user.id, analysis=analysis)
    assert payment.amount == intent.amount == 74
    assert settings.price_rub == 99  # base price untouched
    assert await P.discount_for_payment(session, payment.id) == 25
    (redemption,) = await _redemptions(session)
    assert (redemption.status, redemption.payment_id) == (RedemptionStatus.RESERVED, payment.id)

    payload = {"provider_payment_id": intent.provider_payment_id, "amount": 74, "currency": "RUB"}
    assert await service.confirm_payment(session, payload) is not None
    assert await service.confirm_payment(session, payload) is not None  # duplicate callback
    await session.commit()
    (redemption,) = await _redemptions(session)
    await session.refresh(redemption)
    assert redemption.status == RedemptionStatus.CONSUMED
    assert redemption.consumed_at is not None


async def test_best_discount_used_and_discounts_never_stack(session: AsyncSession) -> None:
    user = await make_user(session, 1, "a")
    await _activated(session, user, "WEEK-25", 25)
    await _activated(session, user, "TOP-50", 50)
    service = _service()

    first, _ = await service.start_payment(session, user_id=user.id, analysis=await unfinished_analysis(session, user))
    second, _ = await service.start_payment(session, user_id=user.id, analysis=await unfinished_analysis(session, user))
    third, _ = await service.start_payment(session, user_id=user.id, analysis=await unfinished_analysis(session, user))
    assert (first.amount, second.amount, third.amount) == (49, 74, 99)


async def test_failed_payment_returns_discount(session: AsyncSession) -> None:
    user = await make_user(session, 1, "a")
    await _activated(session, user, "WEEK-25", 25)
    service = _service()
    payment, _ = await service.start_payment(session, user_id=user.id, analysis=await unfinished_analysis(session, user))
    await repo.mark_payment_failed(session, payment)
    await session.commit()

    (redemption,) = await _redemptions(session)
    await session.refresh(redemption)
    assert (redemption.status, redemption.payment_id) == (RedemptionStatus.APPLIED, None)
    retry, _ = await service.start_payment(session, user_id=user.id, analysis=await unfinished_analysis(session, user))
    assert retry.amount == 74


async def test_provider_error_releases_reservation(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    user = await make_user(session, 1, "a")
    await _activated(session, user, "WEEK-25", 25)
    service = _service()

    async def broken(**_: object) -> object:
        raise TimeoutError("provider down")

    monkeypatch.setattr(service.provider, "create_payment", broken)
    with pytest.raises(TimeoutError):
        await service.start_payment(session, user_id=user.id, analysis=await unfinished_analysis(session, user))
    (redemption,) = await _redemptions(session)
    await session.refresh(redemption)
    assert redemption.status == RedemptionStatus.APPLIED
    assert await P.best_unused_discount(session, user.id) == 25


async def test_reused_pending_payment_keeps_its_amount(session: AsyncSession) -> None:
    user = await make_user(session, 1, "a")
    analysis = await unfinished_analysis(session, user)
    service = _service()
    first, _ = await service.start_payment(session, user_id=user.id, analysis=analysis)
    await _activated(session, user, "WEEK-25", 25)  # entered after the invoice was issued

    again, intent = await service.start_payment(session, user_id=user.id, analysis=analysis)
    assert again.id == first.id and intent.amount == 99
    assert await P.discount_for_payment(session, again.id) is None
    assert await P.best_unused_discount(session, user.id) == 25  # kept for the next new payment


def test_intro_text_variants() -> None:
    plain = texts.payment_intro_text()
    assert "99 RUB" in plain and "Скидка" not in plain
    discounted = texts.payment_intro_text(74, 25)
    assert "74 RUB" in discounted and "<s>99</s>" in discounted and "25%" in discounted
    deferred = texts.payment_intro_text(99, None, 25)
    assert "99 RUB" in deferred and "следующей новой оплате" in deferred
