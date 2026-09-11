"""Tests for the Telegram-facing payment handlers, focused on the
pre_checkout_query validation gap: Telegram asks "should I charge this user"
before any money moves, and approving blindly risks charging for an invoice
we don't recognize or whose amount was tampered with.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.engine import analyze
from app.bot import texts
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
    assert texts.YOOKASSA_PAYMENT_NOT_CONFIRMED in sent_texts


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

    # Callback is acknowledged immediately (bare, no alert) regardless of
    # ownership outcome; the actual "not found" feedback goes out as a
    # regular message via cb_answer (see cb_get_report's docstring for why).
    callback.answer.assert_awaited_once_with()
    sent_texts = [c.args[1] for c in callback.bot.send_message.await_args_list]
    assert texts.ANALYSIS_NOT_FOUND in sent_texts
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
    assert texts.YOOKASSA_PAYMENT_ERROR in sent_texts

    # No payment row should have been left behind by the failed attempt.
    pending = await repo.get_latest_pending_payment(session, analysis.id, user.id)
    assert pending is None


async def test_get_report_recovers_a_paid_analysis_with_no_report_yet(
    session: AsyncSession, monkeypatch
) -> None:
    """Covers "bot restart/crash between payment confirmation and report
    delivery": the user's only recourse is re-tapping the same "Открыть
    полный разбор" button they already have in their chat history, which
    routes through this exact handler. An analysis that is already paid but
    has no report (analysis.paid=True, analysis.report=None — exactly what
    persists if the process died right after marking payment paid and
    before/while generating) must not be treated as "needs a new payment" —
    it must go straight to report generation.
    """
    user = await repo.get_or_create_user(session, telegram_id=406, username="u")
    await session.flush()
    result = analyze("2200373")
    analysis = await repo.create_analysis(session, user_id=user.id, result=result)
    await repo.mark_analysis_paid(session, analysis)
    await session.commit()

    monkeypatch.setattr(payment_module.settings, "openai_api_key", "test-key")

    from app.ai import report_generator

    async def fake_complete_chat(system_prompt: str, user_prompt: str) -> str:
        return "Восстановленный отчёт."

    monkeypatch.setattr(report_generator, "complete_chat", fake_complete_chat)

    callback = _make_callback(f"get_report:{analysis.id}")
    await cb_get_report(callback, session, user)

    sent_texts = [c.args[1] for c in callback.bot.send_message.await_args_list]
    assert "Восстановленный отчёт." in sent_texts

    # No new payment was created — the existing paid state was honored, not re-charged.
    assert await repo.get_latest_pending_payment(session, analysis.id, user.id) is None
    await session.refresh(analysis)
    assert analysis.report == "Восстановленный отчёт."


async def test_get_report_acknowledges_callback_before_slow_yookassa_call(
    session: AsyncSession, monkeypatch
) -> None:
    """Regression test for a real production failure: Telegram invalidates
    a callback query if answerCallbackQuery() isn't called within roughly
    10-15s, but the YooKassa call is allowed up to 35s. If the handler
    answers the callback *after* create_payment(), a slow (or merely
    on-the-edge) YooKassa response causes
    "query is too old and response timeout expired or query ID is invalid".
    This proves the ordering: callback.answer() must complete before the
    slow provider call is even started.
    """
    user = await repo.get_or_create_user(session, telegram_id=406, username="u")
    await session.flush()
    result = analyze("2200373")
    analysis = await repo.create_analysis(session, user_id=user.id, result=result)
    await session.commit()

    provider = YooKassaPaymentProvider()
    call_order: list[str] = []

    async def slow_create(*, amount, currency, description):
        call_order.append("create_payment_started")
        from app.payments.provider import PaymentIntent

        return PaymentIntent(
            provider_payment_id="yk-406",
            amount=amount,
            currency=currency,
            description=description,
            extra={"confirmation_url": "https://yoomoney.ru/checkout/pay"},
        )

    monkeypatch.setattr(provider, "create_payment", slow_create)
    monkeypatch.setattr(payment_module.payment_service, "provider", provider)
    monkeypatch.setattr(payment_module.settings, "payment_provider", "yookassa")

    callback = _make_callback(f"get_report:{analysis.id}")

    async def recording_answer(*args, **kwargs):
        call_order.append("callback_answered")

    callback.answer = AsyncMock(side_effect=recording_answer)

    await cb_get_report(callback, session, user)

    assert call_order == ["callback_answered", "create_payment_started"]


async def test_yookassa_check_acknowledges_callback_before_slow_status_check(
    session: AsyncSession, monkeypatch
) -> None:
    """Same ordering guarantee for the "Проверить оплату" poll button."""
    user, analysis, payment, intent, provider = await _make_yookassa_pending_payment(
        session, 407, monkeypatch
    )
    call_order: list[str] = []

    async def slow_check_status(provider_payment_id):
        call_order.append("check_status_started")
        return {
            "id": intent.provider_payment_id,
            "status": "pending",
            "paid": False,
            "amount": {"value": "99.00", "currency": "RUB"},
        }

    monkeypatch.setattr(provider, "check_status", slow_check_status)

    callback = _make_callback(f"yookassa_check:{analysis.id}")

    async def recording_answer(*args, **kwargs):
        call_order.append("callback_answered")

    callback.answer = AsyncMock(side_effect=recording_answer)

    await cb_yookassa_check(callback, session, user)

    assert call_order == ["callback_answered", "check_status_started"]


async def test_get_report_handles_timeout_without_leaving_stale_payment(
    session: AsyncSession, monkeypatch
) -> None:
    """The exact failure from the production log: create_payment() times
    out after 35s. Must not mark anything paid, must not leave a phantom
    payment row blocking a future retry, and must still promptly answer the
    callback with a friendly (not "invalid credentials") message.
    """
    user = await repo.get_or_create_user(session, telegram_id=408, username="u")
    await session.flush()
    result = analyze("2200373")
    analysis = await repo.create_analysis(session, user_id=user.id, result=result)
    await session.commit()

    provider = YooKassaPaymentProvider()

    async def timing_out_create(*, amount, currency, description):
        raise RuntimeError("YooKassa API request timed out")

    monkeypatch.setattr(provider, "create_payment", timing_out_create)
    monkeypatch.setattr(payment_module.payment_service, "provider", provider)
    monkeypatch.setattr(payment_module.settings, "payment_provider", "yookassa")

    callback = _make_callback(f"get_report:{analysis.id}")
    await cb_get_report(callback, session, user)

    callback.answer.assert_awaited_once_with()  # acknowledged despite the timeout

    sent_texts = [c.args[1] for c in callback.bot.send_message.await_args_list]
    assert texts.YOOKASSA_PAYMENT_ERROR in sent_texts
    # Must never imply the credentials themselves are wrong — this was a
    # timeout, not an authentication failure.
    assert not any("credential" in t.lower() or "ключ" in t.lower() for t in sent_texts)

    await session.refresh(analysis)
    assert analysis.paid is False
    assert await repo.get_latest_pending_payment(session, analysis.id, user.id) is None

    # A subsequent retry must be able to proceed cleanly (no leftover state
    # blocking it) — simulate the user tapping the button again.
    async def succeeding_create(*, amount, currency, description):
        from app.payments.provider import PaymentIntent

        return PaymentIntent(
            provider_payment_id="yk-408-retry",
            amount=amount,
            currency=currency,
            description=description,
            extra={"confirmation_url": "https://yoomoney.ru/checkout/pay"},
        )

    monkeypatch.setattr(provider, "create_payment", succeeding_create)
    retry_callback = _make_callback(f"get_report:{analysis.id}")
    await cb_get_report(retry_callback, session, user)

    pending = await repo.get_latest_pending_payment(session, analysis.id, user.id)
    assert pending is not None
    assert pending.provider_payment_id == "yk-408-retry"


async def test_report_reuse_does_not_depend_on_in_memory_lock_surviving(
    session: AsyncSession, monkeypatch
) -> None:
    """The per-analysis asyncio.Lock in app.bot.handlers.payment only exists
    to serialize *simultaneous, in-process* delivery attempts — it is not
    what makes report reuse correct. That guarantee comes entirely from the
    DB-persisted analysis.report column. A real bot restart wipes all
    in-memory state (the lock dict included) automatically; this test
    proxies that by clearing the lock dict explicitly between two delivery
    attempts and proves the second attempt still reuses the stored report
    with zero additional OpenAI calls, rather than accidentally depending on
    a lock object surviving.
    """
    from app.ai import report_generator

    monkeypatch.setattr(payment_module.settings, "openai_api_key", "test-key")

    call_count = {"n": 0}

    async def fake_complete_chat(system_prompt: str, user_prompt: str) -> str:
        call_count["n"] += 1
        return "Стабильный отчёт."

    monkeypatch.setattr(report_generator, "complete_chat", fake_complete_chat)

    user = await repo.get_or_create_user(session, telegram_id=999, username="u")
    await session.flush()
    result = analyze("2200373")
    analysis = await repo.create_analysis(session, user_id=user.id, result=result)
    await repo.mark_analysis_paid(session, analysis)
    await session.commit()

    sent: list[str] = []

    async def send(text: str, kb: object) -> None:
        sent.append(text)

    # 1. Generate + 2. save the report for real (no cache yet).
    await payment_module._deliver_report(send, session, analysis, user)
    assert call_count["n"] == 1
    assert sent == ["Стабильный отчёт."]

    # 3. Release process-level state — the restart proxy.
    payment_module._report_generation_locks.clear()

    # 4. Request the report again.
    await session.refresh(analysis)
    await payment_module._deliver_report(send, session, analysis, user)

    assert call_count["n"] == 1, "a second delivery must not call OpenAI again"
    assert sent == ["Стабильный отчёт.", "Стабильный отчёт."]
