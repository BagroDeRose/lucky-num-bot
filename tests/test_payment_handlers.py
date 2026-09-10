"""Tests for the Telegram-facing payment handlers, focused on the
pre_checkout_query validation gap: Telegram asks "should I charge this user"
before any money moves, and approving blindly risks charging for an invoice
we don't recognize or whose amount was tampered with.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.engine import analyze
from app.bot.handlers import payment as payment_module
from app.bot.handlers.payment import cb_get_report, cb_yookassa_check, process_pre_checkout
from app.database import repositories as repo
from app.database.models import PaymentStatus
from app.payments.provider import MockPaymentProvider, YooKassaPaymentProvider
from app.payments.service import PaymentService


async def _make_pending_payment(session: AsyncSession, telegram_id: int):
    user = await repo.get_or_create_user(session, telegram_id=telegram_id, username="u")
    await session.flush()
    result = analyze("2200373")
    analysis = await repo.create_analysis(session, user_id=user.id, result=result)
    await session.commit()

    service = PaymentService()
    service.provider = MockPaymentProvider()
    payment, intent = await service.start_payment(session, user_id=user.id, analysis=analysis)
    await session.commit()
    return payment, intent


async def test_pre_checkout_approves_known_matching_invoice(session: AsyncSession) -> None:
    payment, intent = await _make_pending_payment(session, telegram_id=200)

    query = AsyncMock()
    query.invoice_payload = intent.provider_payment_id
    query.total_amount = intent.amount * 100

    await process_pre_checkout(query, session)

    query.answer.assert_awaited_once_with(ok=True)


async def test_pre_checkout_rejects_unknown_invoice(session: AsyncSession) -> None:
    query = AsyncMock()
    query.invoice_payload = "tg_does-not-exist"
    query.total_amount = 9900

    await process_pre_checkout(query, session)

    assert query.answer.await_args.kwargs["ok"] is False


async def test_pre_checkout_rejects_amount_mismatch_and_marks_failed(session: AsyncSession) -> None:
    payment, intent = await _make_pending_payment(session, telegram_id=201)

    query = AsyncMock()
    query.invoice_payload = intent.provider_payment_id
    query.total_amount = 1  # tampered: far below the real price

    await process_pre_checkout(query, session)

    assert query.answer.await_args.kwargs["ok"] is False
    await session.refresh(payment)
    assert payment.status == PaymentStatus.FAILED


async def test_pre_checkout_rejects_already_paid_invoice(session: AsyncSession) -> None:
    """A replayed pre_checkout_query for an invoice that was already paid
    must never be re-approved (would double-charge the user).
    """
    payment, intent = await _make_pending_payment(session, telegram_id=202)
    service = PaymentService()
    service.provider = MockPaymentProvider()
    await service.confirm_payment(
        session,
        {
            "provider_payment_id": intent.provider_payment_id,
            "amount": intent.amount,
            "currency": intent.currency,
        },
    )
    await session.commit()

    query = AsyncMock()
    query.invoice_payload = intent.provider_payment_id
    query.total_amount = intent.amount * 100

    await process_pre_checkout(query, session)

    assert query.answer.await_args.kwargs["ok"] is False


async def _make_yookassa_pending_payment(session: AsyncSession, telegram_id: int, monkeypatch):
    from app.payments.provider import PaymentIntent

    user = await repo.get_or_create_user(session, telegram_id=telegram_id, username="u")
    await session.flush()
    result = analyze("2200373")
    analysis = await repo.create_analysis(session, user_id=user.id, result=result)
    await session.commit()

    provider = YooKassaPaymentProvider()

    async def fake_create(*, amount, currency, description):
        return PaymentIntent(
            provider_payment_id=f"yk-{telegram_id}",
            amount=amount,
            currency=currency,
            description=description,
            extra={"confirmation_url": "https://yoomoney.ru/checkout/pay"},
        )

    monkeypatch.setattr(provider, "create_payment", fake_create)
    monkeypatch.setattr(payment_module.payment_service, "provider", provider)

    payment, intent = await payment_module.payment_service.start_payment(
        session, user_id=user.id, analysis=analysis
    )
    await session.commit()
    return user, analysis, payment, intent, provider


def _make_callback(callback_data: str) -> AsyncMock:
    callback = AsyncMock()
    callback.data = callback_data
    return callback


async def test_yookassa_check_reports_not_confirmed_while_pending(
    session: AsyncSession, monkeypatch
) -> None:
    user, analysis, payment, intent, provider = await _make_yookassa_pending_payment(
        session, 400, monkeypatch
    )

    async def fake_check_status(provider_payment_id):
        return {
            "id": intent.provider_payment_id,
            "status": "pending",
            "paid": False,
            "amount": {"value": "99.00", "currency": "RUB"},
        }

    monkeypatch.setattr(provider, "check_status", fake_check_status)

    callback = _make_callback(f"yookassa_check:{analysis.id}")
    await cb_yookassa_check(callback, session, user)

    await session.refresh(payment)
    assert payment.status == "pending"
    await session.refresh(analysis)
    assert analysis.paid is False
    # a "not confirmed yet" message was sent, not a report
    sent_texts = [c.args[1] for c in callback.bot.send_message.await_args_list]
    assert any("не подтверждена" in t for t in sent_texts)


async def test_yookassa_check_unlocks_report_when_succeeded(
    session: AsyncSession, monkeypatch
) -> None:
    user, analysis, payment, intent, provider = await _make_yookassa_pending_payment(
        session, 401, monkeypatch
    )

    async def fake_check_status(provider_payment_id):
        return {
            "id": intent.provider_payment_id,
            "status": "succeeded",
            "paid": True,
            "amount": {"value": f"{intent.amount:.2f}", "currency": intent.currency},
        }

    monkeypatch.setattr(provider, "check_status", fake_check_status)

    callback = _make_callback(f"yookassa_check:{analysis.id}")
    await cb_yookassa_check(callback, session, user)

    await session.refresh(payment)
    assert payment.status == "paid"
    await session.refresh(analysis)
    assert analysis.paid is True


async def test_yookassa_check_cannot_access_another_users_analysis(
    session: AsyncSession, monkeypatch
) -> None:
    """Ownership must hold even for the YooKassa-specific callback."""
    owner, analysis, payment, intent, provider = await _make_yookassa_pending_payment(
        session, 402, monkeypatch
    )
    attacker = await repo.get_or_create_user(session, telegram_id=403, username="attacker")
    await session.commit()

    callback = _make_callback(f"yookassa_check:{analysis.id}")
    await cb_yookassa_check(callback, session, attacker)

    callback.answer.assert_awaited_once_with("Анализ не найден.", show_alert=True)
    await session.refresh(payment)
    assert payment.status == "pending"


async def test_yookassa_check_marks_canceled_payment_as_failed(
    session: AsyncSession, monkeypatch
) -> None:
    """"canceled" is a final YooKassa state that will never become
    "succeeded" — it must transition our payment to FAILED, not leave it
    pending forever with a misleading "try again" message.
    """
    user, analysis, payment, intent, provider = await _make_yookassa_pending_payment(
        session, 404, monkeypatch
    )

    async def fake_check_status(provider_payment_id):
        return {
            "id": intent.provider_payment_id,
            "status": "canceled",
            "paid": False,
            "amount": {"value": f"{intent.amount:.2f}", "currency": intent.currency},
        }

    monkeypatch.setattr(provider, "check_status", fake_check_status)

    callback = _make_callback(f"yookassa_check:{analysis.id}")
    await cb_yookassa_check(callback, session, user)

    await session.refresh(payment)
    assert payment.status == PaymentStatus.FAILED
    sent_texts = [c.args[1] for c in callback.bot.send_message.await_args_list]
    assert any("отменён" in t for t in sent_texts)


async def test_get_report_shows_friendly_error_when_payment_creation_fails(
    session: AsyncSession, monkeypatch
) -> None:
    """If YooKassa is unreachable when the user taps "get full report", the
    handler must degrade to a friendly message, not crash or hang.
    """
    user = await repo.get_or_create_user(session, telegram_id=405, username="u")
    await session.flush()
    result = analyze("2200373")
    analysis = await repo.create_analysis(session, user_id=user.id, result=result)
    await session.commit()

    provider = YooKassaPaymentProvider()

    async def failing_create(*, amount, currency, description):
        raise RuntimeError("YooKassa API connection error")

    monkeypatch.setattr(provider, "create_payment", failing_create)
    monkeypatch.setattr(payment_module.payment_service, "provider", provider)
    monkeypatch.setattr(payment_module.settings, "payment_provider", "yookassa")

    callback = _make_callback(f"get_report:{analysis.id}")
    await cb_get_report(callback, session, user)

    sent_texts = [c.args[1] for c in callback.bot.send_message.await_args_list]
    assert any("технические неполадки" in t for t in sent_texts)

    # No payment row should have been left behind by the failed attempt.
    pending = await repo.get_latest_pending_payment(session, analysis.id, user.id)
    assert pending is None
