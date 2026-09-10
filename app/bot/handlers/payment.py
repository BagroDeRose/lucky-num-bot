from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.report_generator import ReportGenerationError, generate_report
from app.analysis.models import AnalysisResult
from app.bot import texts
from app.bot.keyboards.main import after_report_kb, mock_payment_kb, retry_report_kb
from app.bot.utils import cb_answer, require_callback_data
from app.config import settings
from app.database import repositories as repo
from app.database.models import Analysis, User
from app.logging import get_logger
from app.payments.service import PaymentService

logger = get_logger(__name__)
router = Router(name="payment")

payment_service = PaymentService()

Sender = Callable[[str, InlineKeyboardMarkup | None], Awaitable[None]]


def _message_sender(message: Message) -> Sender:
    async def send(text: str, kb: InlineKeyboardMarkup | None) -> None:
        await message.answer(text, reply_markup=kb)

    return send


def _callback_sender(callback: CallbackQuery) -> Sender:
    async def send(text: str, kb: InlineKeyboardMarkup | None) -> None:
        await cb_answer(callback, text, reply_markup=kb)

    return send


async def _deliver_report(send: Sender, session: AsyncSession, analysis: Analysis, user: User) -> None:
    """Send the report for a paid analysis, generating it if not cached yet.

    Cached (already-generated) reports never trigger a new OpenAI call, which
    keeps duplicate Telegram updates/retries from causing extra AI spend.
    """
    if analysis.report:
        await send(analysis.report, after_report_kb())
        return

    await repo.log_event(session, user.id, "report_generation_started", {"analysis_id": analysis.id})
    await session.commit()

    try:
        result = AnalysisResult.model_validate(analysis.analysis_payload)
        report_text = await generate_report(result)
    except ReportGenerationError as exc:
        logger.warning("Report generation failed for analysis %s: %s", analysis.id, exc)
        await repo.log_event(
            session, user.id, "report_generation_failed", {"analysis_id": analysis.id}
        )
        await session.commit()
        await send(texts.REPORT_GENERATION_FAILED, retry_report_kb(analysis.id))
        return

    await repo.save_report(session, analysis, report_text)
    await repo.log_event(session, user.id, "report_generation_success", {"analysis_id": analysis.id})
    await session.commit()

    await send(report_text, after_report_kb())


@router.callback_query(F.data.startswith("get_report:"))
async def cb_get_report(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    data = require_callback_data(callback)
    analysis_id = int(data.split(":", 1)[1])
    analysis = await repo.get_analysis(session, analysis_id, user.id)
    if analysis is None:
        await callback.answer("Анализ не найден.", show_alert=True)
        return

    await repo.log_event(session, user.id, "payment_clicked", {"analysis_id": analysis.id})

    if analysis.paid:
        await session.commit()
        await _deliver_report(_callback_sender(callback), session, analysis, user)
        await callback.answer()
        return

    payment, intent = await payment_service.start_payment(session, user_id=user.id, analysis=analysis)
    await repo.log_event(
        session, user.id, "payment_created", {"analysis_id": analysis.id, "payment_id": payment.id}
    )
    await session.commit()

    if (
        settings.payment_provider == "telegram"
        and callback.message is not None
        and callback.bot is not None
    ):
        await callback.bot.send_invoice(
            chat_id=callback.message.chat.id,
            title="LuckyNum: полный отчёт",
            description=intent.description,
            payload=intent.extra["payload"],
            provider_token=settings.payment_token,
            currency=intent.currency,
            prices=[LabeledPrice(label="Полный отчёт", amount=intent.amount * 100)],
        )
    else:
        await cb_answer(
            callback,
            texts.payment_intro_text(),
            reply_markup=mock_payment_kb(analysis.id, intent.amount, intent.currency),
        )

    await callback.answer()


@router.callback_query(F.data.startswith("mock_pay:"))
async def cb_mock_pay(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    data = require_callback_data(callback)
    analysis_id = int(data.split(":", 1)[1])
    analysis = await repo.get_analysis(session, analysis_id, user.id)
    if analysis is None:
        await callback.answer("Анализ не найден.", show_alert=True)
        return

    pending = await repo.get_latest_pending_payment(session, analysis_id, user.id)
    if pending is None:
        await callback.answer("Платёж не найден, попробуйте ещё раз.", show_alert=True)
        return

    payment = await payment_service.confirm_payment(
        session,
        {
            "provider_payment_id": pending.provider_payment_id,
            "amount": pending.amount,
            "currency": pending.currency,
        },
    )
    if payment is None:
        await callback.answer("Не удалось подтвердить оплату.", show_alert=True)
        return

    await repo.mark_analysis_paid(session, analysis)
    await repo.log_event(session, user.id, "payment_success", {"analysis_id": analysis.id})
    await session.commit()

    send = _callback_sender(callback)
    await send(texts.PAYMENT_SUCCESS, None)
    await _deliver_report(send, session, analysis, user)
    await callback.answer()


@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
    # We generated this invoice ourselves; no further verification needed
    # beyond what Telegram already guarantees.
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message, session: AsyncSession, user: User) -> None:
    sp = message.successful_payment
    assert sp is not None

    payment = await payment_service.confirm_payment(
        session,
        {
            "invoice_payload": sp.invoice_payload,
            "total_amount": sp.total_amount // 100,
            "currency": sp.currency,
        },
    )
    if payment is None:
        logger.warning("Received successful_payment with unknown payload: %s", sp.invoice_payload)
        return

    analysis = await repo.get_analysis(session, payment.analysis_id, user.id)
    if analysis is None:
        return

    await repo.mark_analysis_paid(session, analysis)
    await repo.log_event(session, user.id, "payment_success", {"analysis_id": analysis.id})
    await session.commit()

    await message.answer(texts.PAYMENT_SUCCESS)
    await _deliver_report(_message_sender(message), session, analysis, user)


@router.callback_query(F.data.startswith("retry_report:"))
async def cb_retry_report(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    data = require_callback_data(callback)
    analysis_id = int(data.split(":", 1)[1])
    analysis = await repo.get_analysis(session, analysis_id, user.id)
    if analysis is None or not analysis.paid:
        await callback.answer("Анализ не найден.", show_alert=True)
        return

    await _deliver_report(_callback_sender(callback), session, analysis, user)
    await callback.answer()
