"""Tests for the YooKassa payment provider. All HTTP calls are mocked — no
real network requests are made against the YooKassa API in the test suite.
"""

from __future__ import annotations

import inspect
import ssl
from pathlib import Path
from unittest.mock import patch

import aiohttp
import certifi
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.engine import analyze
from app.config import settings
from app.database import repositories as repo
from app.payments import provider as provider_module
from app.payments.provider import (
    YOOKASSA_DESCRIPTION_MAX_LENGTH,
    YOOKASSA_SSL_CONTEXT,
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


async def test_request_normalizes_timeout_into_runtime_error() -> None:
    """A stuck/slow connection must fail predictably (and quickly, relative
    to aiohttp's 5-minute default) rather than hang a Telegram callback.
    """
    provider = YooKassaPaymentProvider()

    with (
        patch.object(aiohttp.ClientSession, "request", side_effect=TimeoutError("timed out")),
        pytest.raises(RuntimeError, match="timed out"),
    ):
        await provider._request("GET", "/payments/x")  # noqa: SLF001


async def test_request_normalizes_connection_error_into_runtime_error() -> None:
    provider = YooKassaPaymentProvider()

    with (
        patch.object(
            aiohttp.ClientSession,
            "request",
            side_effect=aiohttp.ClientConnectionError("connection refused"),
        ),
        pytest.raises(RuntimeError, match="connection error"),
    ):
        await provider._request("GET", "/payments/x")  # noqa: SLF001


async def test_create_payment_truncates_overlong_description(monkeypatch) -> None:
    """YooKassa hard-rejects descriptions over 128 characters."""
    provider = YooKassaPaymentProvider()
    captured: dict = {}

    async def fake_request(method, path, *, json=None, idempotence_key=None):
        captured["description"] = json["description"]
        return {"id": "pay-1", "status": "pending", "confirmation": {}}

    monkeypatch.setattr(provider, "_request", fake_request)

    long_description = "x" * 500
    await provider.create_payment(amount=99, currency="RUB", description=long_description)

    assert len(captured["description"]) == YOOKASSA_DESCRIPTION_MAX_LENGTH


async def test_create_payment_leaves_short_description_untouched(monkeypatch) -> None:
    provider = YooKassaPaymentProvider()
    captured: dict = {}

    async def fake_request(method, path, *, json=None, idempotence_key=None):
        captured["description"] = json["description"]
        return {"id": "pay-1", "status": "pending", "confirmation": {}}

    monkeypatch.setattr(provider, "_request", fake_request)

    await provider.create_payment(amount=99, currency="RUB", description="LuckyNum report")

    assert captured["description"] == "LuckyNum report"


# --- TLS / certificate verification -----------------------------------------
#
# Regression coverage for a real production failure: on Windows, aiohttp's
# default TCPConnector builds its SSL context with no explicit `cafile`,
# which falls back to OpenSSL's compiled-in default cert paths — paths that
# often don't exist there, unlike curl.exe (which uses the OS cert store via
# Schannel). The fix is an explicit ssl.SSLContext built from certifi's CA
# bundle, never a weakened/disabled verification mode.


def test_yookassa_ssl_context_has_verification_enabled() -> None:
    """The context must require and validate certificates — this is the
    exact opposite of `ssl=False` / CERT_NONE.
    """
    assert YOOKASSA_SSL_CONTEXT.verify_mode == ssl.CERT_REQUIRED
    assert YOOKASSA_SSL_CONTEXT.check_hostname is True


def test_yookassa_ssl_context_uses_the_certifi_bundle() -> None:
    """Confirms the CA bundle is specifically certifi's — not an empty
    context, not the platform default paths that caused the original bug.
    """
    loaded_certs = YOOKASSA_SSL_CONTEXT.get_ca_certs()
    assert len(loaded_certs) > 0

    reference = ssl.create_default_context(cafile=certifi.where())
    assert len(loaded_certs) == len(reference.get_ca_certs())

    # The bundle file certifi reports must actually exist and be non-empty —
    # guards against a broken/uninstalled certifi silently no-op'ing.
    bundle_path = Path(certifi.where())
    assert bundle_path.is_file()
    assert bundle_path.stat().st_size > 0


async def test_request_uses_the_shared_ssl_context_via_tcp_connector(monkeypatch) -> None:
    """Verifies the actual wiring: _request() must construct its connector
    with `ssl=YOOKASSA_SSL_CONTEXT`, not rely on aiohttp's default connector
    (which is what caused the original certificate verification failure).
    """
    provider = YooKassaPaymentProvider()
    captured_kwargs: dict = {}
    real_init = aiohttp.TCPConnector.__init__

    def spying_init(self, *args, **kwargs):
        captured_kwargs.update(kwargs)
        return real_init(self, *args, **kwargs)

    with (
        patch.object(aiohttp.TCPConnector, "__init__", spying_init),
        patch.object(aiohttp.ClientSession, "request", side_effect=TimeoutError("t")),
        pytest.raises(RuntimeError),
    ):
        await provider._request("GET", "/payments/x")  # noqa: SLF001

    assert "ssl" in captured_kwargs
    assert captured_kwargs["ssl"] is YOOKASSA_SSL_CONTEXT


def test_no_disabled_or_weakened_tls_verification_in_source() -> None:
    """Static guardrail: the provider module must never introduce
    `ssl=False`, `verify_ssl=False`, or an unverified SSL context — any of
    which would silently defeat certificate verification. This test reads
    the actual source of app/payments/provider.py, so it fails if such a
    change is ever reintroduced, even by an edit that doesn't touch the
    functions tested above.
    """
    source = inspect.getsource(provider_module)
    forbidden_patterns = [
        "ssl=False",
        "verify_ssl=False",
        "CERT_NONE",
        "check_hostname = False",
        "check_hostname=False",
        "_create_unverified_context",
    ]
    for pattern in forbidden_patterns:
        assert pattern not in source, f"found disabled TLS verification pattern: {pattern!r}"
