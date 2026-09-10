"""End-to-end product flow, without the Telegram transport layer.

Exercises: user creation -> analysis -> free teaser -> payment (mock
provider) -> idempotent confirmation -> paid report generation (mocked
OpenAI) -> cached retrieval. This is the closest thing to a full smoke test
that doesn't require a live bot/token.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import report_generator
from app.analysis.engine import analyze
from app.analysis.interpreter import render_teaser
from app.analysis.models import AnalysisResult
from app.config import settings
from app.database import repositories as repo
from app.payments.provider import MockPaymentProvider
from app.payments.service import PaymentService


async def test_full_free_to_paid_flow(session: AsyncSession, monkeypatch) -> None:
    # 1. User opens the bot.
    user = await repo.get_or_create_user(session, telegram_id=777, username="ivan")
    await repo.log_event(session, user.id, "start")

    # 2. User submits a serial number; deterministic engine analyzes it.
    result = analyze("2200373")
    analysis = await repo.create_analysis(session, user_id=user.id, result=result)
    await repo.log_event(session, user.id, "analysis_completed", {"analysis_id": analysis.id})
    await session.commit()

    # 3. Free teaser is shown and must not leak the full report.
    teaser = render_teaser(result)
    assert str(result.overall_score) in teaser
    assert "полную персональную интерпретацию" in teaser

    # 4. User clicks "get full report" -> payment intent created.
    service = PaymentService()
    service.provider = MockPaymentProvider()
    payment, intent = await service.start_payment(session, user_id=user.id, analysis=analysis)
    await repo.log_event(session, user.id, "payment_clicked", {"analysis_id": analysis.id})
    await repo.log_event(session, user.id, "payment_created", {"payment_id": payment.id})
    await session.commit()
    assert payment.status == "pending"

    # 5. User "pays" (mock). Duplicate confirmations must not double-process.
    callback = {
        "provider_payment_id": intent.provider_payment_id,
        "amount": intent.amount,
        "currency": intent.currency,
    }
    confirmed_once = await service.confirm_payment(session, callback)
    confirmed_twice = await service.confirm_payment(session, callback)
    assert confirmed_once is not None and confirmed_once.status == "paid"
    assert confirmed_twice is not None and confirmed_twice.id == confirmed_once.id

    await repo.mark_analysis_paid(session, analysis)
    await repo.log_event(session, user.id, "payment_success", {"analysis_id": analysis.id})
    await session.commit()
    assert analysis.paid is True

    # 6. Paid report is generated from the *same* structured data (mocked AI).
    monkeypatch.setattr(settings, "openai_api_key", "test-key")

    calls = {"count": 0}

    async def fake_complete_chat(system_prompt: str, user_prompt: str) -> str:
        calls["count"] += 1
        assert result.normalized_number in user_prompt
        return "Ваш персональный отчёт готов."

    monkeypatch.setattr(report_generator, "complete_chat", fake_complete_chat)

    stored_result = AnalysisResult.model_validate(analysis.analysis_payload)
    report_text = await report_generator.generate_report(stored_result)
    await repo.save_report(session, analysis, report_text)
    await repo.log_event(session, user.id, "report_generation_success", {"analysis_id": analysis.id})
    await session.commit()

    assert analysis.report == "Ваш персональный отчёт готов."
    assert calls["count"] == 1

    # 7. Re-opening the (now cached) report must NOT call OpenAI again.
    fetched = await repo.get_analysis(session, analysis.id, user.id)
    assert fetched is not None
    assert fetched.report == report_text
    assert calls["count"] == 1  # unchanged: no duplicate AI call for cached report

    # 8. Basic funnel stats reflect the journey.
    stats = await repo.funnel_stats(session)
    assert stats["start"] == 1
    assert stats["payment_success"] == 1
    assert stats["report_generation_success"] == 1
