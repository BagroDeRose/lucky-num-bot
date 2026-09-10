"""Tests for the YooKassa payment provider. All HTTP calls are mocked — no
real network requests are made against the YooKassa API in the test suite.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.engine import analyze
from app.config import settings
from app.database import repositories as repo
from app.payments.provider import (
    YooKassaPaymentProvider,
    get_payment_provider,
)
from app.payments.service import PaymentService


@pytest.fixture(autouse=True)
def _configure_yookassa(monkeypatch):
    monkeypatch.setattr(settings, "yookassa_shop_id", "test-shop-id")
    monkeypatch.setattr(settings, "yookassa_shop_api_key", "test-shop-secret")


def test_get_payment_provider_returns_yookassa() -> None:
    provider = get_payment_provider("yookassa")
    assert isinstance(provider, YooKassaPaymentProvider)
    assert provider.name == "yookassa"


async def test_create_payment_sends_correct_request_and_builds_intent(monkeypatch) -> None:
    provider = YooKassaPaymentProvider()
    captured: dict = {}

    async def fake_request(method, path, *, json=None, idempotence_key=None):
        captured["method"] = method
        captured["path"] = path
        captured["json"] = json
        captured["idempotence_key"] = idempotence_key
        return {
            "id": "2d3d8b1f-0000-5000-8000-000000000001",
            "status": "pending",
            "confirmation": {"type": "redirect", "confirmation_url": "https://yoomoney.ru/checkout/pay"},
        }

    monkeypatch.setattr(provider, "_request", fake_request)

    intent = await provider.create_payment(amount=99, currency="RUB", description="LuckyNum report")

    assert captured["method"] == "POST"
    assert captured["path"] == "/payments"
    assert captured["idempotence_key"]  # a fresh idempotence key was generated
    assert captured["json"]["amount"] == {"value": "99.00", "currency": "RUB"}
    assert captured["json"]["confirmation"]["type"] == "redirect"

    assert intent.provider_payment_id == "2d3d8b1f-0000-5000-8000-000000000001"
    assert intent.amount == 99
    assert intent.currency == "RUB"
    assert intent.extra["confirmation_url"] == "https://yoomoney.ru/checkout/pay"


async def test_verify_payment_true_when_succeeded_and_paid(monkeypatch) -> None:
    provider = YooKassaPaymentProvider()

    async def fake_check_status(provider_payment_id):
        return {"status": "succeeded", "paid": True}

    monkeypatch.setattr(provider, "check_status", fake_check_status)

    assert await provider.verify_payment("pay-1") is True


@pytest.mark.parametrize(
    "status,paid",
    [
        ("pending", False),
        ("waiting_for_capture", False),
        ("canceled", False),
        ("succeeded", False),  # succeeded but not yet marked paid: don't trust it
    ],
)
async def test_verify_payment_false_when_not_fully_confirmed(monkeypatch, status, paid) -> None:
    provider = YooKassaPaymentProvider()

    async def fake_check_status(provider_payment_id):
        return {"status": status, "paid": paid}

    monkeypatch.setattr(provider, "check_status", fake_check_status)

    assert await provider.verify_payment("pay-1") is False


def test_parse_callback_extracts_fields_from_yookassa_payment_object() -> None:
    provider = YooKassaPaymentProvider()
    payload = {
        "id": "2d3d8b1f-0000-5000-8000-000000000001",
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "99.00", "currency": "RUB"},
    }

    result = provider.parse_callback(payload)

    assert result.provider_payment_id == "2d3d8b1f-0000-5000-8000-000000000001"
    assert result.amount == 99
    assert result.currency == "RUB"
    assert result.succeeded is True


def test_parse_callback_not_succeeded_when_paid_flag_missing() -> None:
    provider = YooKassaPaymentProvider()
    payload = {
        "id": "pay-2",
        "status": "succeeded",
        "paid": False,
        "amount": {"value": "99.00", "currency": "RUB"},
    }

    result = provider.parse_callback(payload)
    assert result.succeeded is False


async def test_request_rejects_when_not_configured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "yookassa_shop_id", "")
    monkeypatch.setattr(settings, "yookassa_shop_api_key", "")
    provider = YooKassaPaymentProvider()

    with pytest.raises(RuntimeError, match="not configured"):
        await provider._request("GET", "/payments/x")  # noqa: SLF001


async def test_payment_service_confirm_is_idempotent_with_yookassa(
    session: AsyncSession, monkeypatch
) -> None:
    """The generic idempotency guarantee in PaymentService must hold for the
    real provider too, not just MockPaymentProvider — a duplicate "Проверить
    оплату" tap (polling twice) must never double-unlock.
    """
    user = await repo.get_or_create_user(session, telegram_id=300, username="u")
    await session.flush()
    result = analyze("2200373")
    analysis = await repo.create_analysis(session, user_id=user.id, result=result)
    await session.commit()

    provider = YooKassaPaymentProvider()

    async def fake_create(*, amount, currency, description):
        from app.payments.provider import PaymentIntent

        return PaymentIntent(
            provider_payment_id="yk-pay-1",
            amount=amount,
            currency=currency,
            description=description,
            extra={"confirmation_url": "https://yoomoney.ru/checkout/pay"},
        )

    monkeypatch.setattr(provider, "create_payment", fake_create)

    service = PaymentService()
    service.provider = provider

    payment, intent = await service.start_payment(session, user_id=user.id, analysis=analysis)
    await session.commit()

    yookassa_payload = {
        "id": intent.provider_payment_id,
        "status": "succeeded",
        "paid": True,
        "amount": {"value": f"{intent.amount:.2f}", "currency": intent.currency},
    }

    async def fake_check_status(provider_payment_id):
        return yookassa_payload

    monkeypatch.setattr(provider, "check_status", fake_check_status)

    first = await service.confirm_payment(session, yookassa_payload)
    second = await service.confirm_payment(session, yookassa_payload)
    await session.commit()

    assert first is not None and first.status == "paid"
    assert second is not None and second.id == first.id and second.status == "paid"
