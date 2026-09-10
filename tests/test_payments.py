from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.engine import analyze
from app.database import repositories as repo
from app.payments.provider import MockPaymentProvider
from app.payments.service import PaymentService


async def _make_analysis(session: AsyncSession, telegram_id: int):
    user = await repo.get_or_create_user(session, telegram_id=telegram_id, username="u")
    await session.flush()
    result = analyze("2200373")
    analysis = await repo.create_analysis(session, user_id=user.id, result=result)
    await session.commit()
    return user, analysis


async def test_start_payment_creates_pending_payment(session: AsyncSession) -> None:
    user, analysis = await _make_analysis(session, telegram_id=100)
    service = PaymentService()
    service.provider = MockPaymentProvider()

    payment, intent = await service.start_payment(session, user_id=user.id, analysis=analysis)
    await session.commit()

    assert payment.provider_payment_id == intent.provider_payment_id
    assert payment.status == "pending"


async def test_start_payment_reuses_existing_pending_payment(session: AsyncSession) -> None:
    """Repeatedly tapping "get full report" before paying must not create a
    new pending payment row (and a new provider_payment_id) every time.
    """
    user, analysis = await _make_analysis(session, telegram_id=104)
    service = PaymentService()
    service.provider = MockPaymentProvider()

    payment1, intent1 = await service.start_payment(session, user_id=user.id, analysis=analysis)
    await session.commit()
    payment2, intent2 = await service.start_payment(session, user_id=user.id, analysis=analysis)
    await session.commit()

    assert payment1.id == payment2.id
    assert intent1.provider_payment_id == intent2.provider_payment_id


async def test_confirm_payment_is_idempotent_against_duplicate_callbacks(
    session: AsyncSession,
) -> None:
    user, analysis = await _make_analysis(session, telegram_id=101)
    service = PaymentService()
    service.provider = MockPaymentProvider()

    payment, intent = await service.start_payment(session, user_id=user.id, analysis=analysis)
    await session.commit()

    callback_payload = {
        "provider_payment_id": intent.provider_payment_id,
        "amount": intent.amount,
        "currency": intent.currency,
    }

    first = await service.confirm_payment(session, callback_payload)
    second = await service.confirm_payment(session, callback_payload)
    await session.commit()

    assert first is not None
    assert first.status == "paid"
    assert second is not None
    assert second.id == first.id
    assert second.status == "paid"


async def test_confirm_payment_returns_none_for_unknown_id(session: AsyncSession) -> None:
    service = PaymentService()
    service.provider = MockPaymentProvider()

    result = await service.confirm_payment(
        session, {"provider_payment_id": "does-not-exist", "amount": 99, "currency": "RUB"}
    )
    assert result is None


async def test_confirm_payment_rejects_amount_mismatch(session: AsyncSession) -> None:
    """A callback claiming a different amount than what was actually offered
    must never unlock the payment, even if the provider_payment_id matches.
    """
    user, analysis = await _make_analysis(session, telegram_id=102)
    service = PaymentService()
    service.provider = MockPaymentProvider()

    payment, intent = await service.start_payment(session, user_id=user.id, analysis=analysis)
    await session.commit()

    tampered_payload = {
        "provider_payment_id": intent.provider_payment_id,
        "amount": 1,  # far below the real price
        "currency": intent.currency,
    }

    result = await service.confirm_payment(session, tampered_payload)
    await session.commit()

    assert result is None
    await session.refresh(payment)
    assert payment.status == "pending"


async def test_confirm_payment_rejects_currency_mismatch(session: AsyncSession) -> None:
    user, analysis = await _make_analysis(session, telegram_id=103)
    service = PaymentService()
    service.provider = MockPaymentProvider()

    payment, intent = await service.start_payment(session, user_id=user.id, analysis=analysis)
    await session.commit()

    tampered_payload = {
        "provider_payment_id": intent.provider_payment_id,
        "amount": intent.amount,
        "currency": "USD",
    }

    result = await service.confirm_payment(session, tampered_payload)
    await session.commit()

    assert result is None
    await session.refresh(payment)
    assert payment.status == "pending"
