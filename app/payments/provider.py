"""Payment provider abstraction.

Business logic never talks to a concrete payment gateway directly — it goes
through this interface. This keeps the door open to swapping in a real
Russian payment provider later without touching handlers or services.

Three implementations ship with the MVP:

* `MockPaymentProvider` — instant, local-only "payment" for development and
  tests. No real money moves.
* `TelegramPaymentProvider` — uses native Telegram Payments (bot.send_invoice
  / successful_payment), which Telegram itself connects to a real payment
  provider configured via BotFather (PAYMENT_TOKEN).
* `YooKassaPaymentProvider` — real integration against the YooKassa REST API
  (PAYMENT_PROVIDER=yookassa). See its docstring for the confirmation/
  verification flow.

A different third-party gateway can be added later by implementing this same
interface — see the class docstrings below for exactly which methods to
fill in.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import aiohttp

from app.config import settings
from app.logging import get_logger

logger = get_logger(__name__)


@dataclass
class PaymentIntent:
    """Everything needed to present a payment to the user.

    `provider_payment_id` is generated up-front by our own system (not the
    gateway) so it can be stored and matched idempotently *before* the actual
    payment completes — this is what `payments.provider_payment_id` stores.
    """

    provider_payment_id: str
    amount: int
    currency: str
    description: str
    extra: dict = field(default_factory=dict)


@dataclass
class PaymentCallbackResult:
    provider_payment_id: str
    amount: int
    currency: str
    succeeded: bool


class PaymentProvider(ABC):
    name: str

    @abstractmethod
    async def create_payment(
        self, *, amount: int, currency: str, description: str
    ) -> PaymentIntent:
        """Create a payment intent the user can act on (pay button / invoice)."""

    @abstractmethod
    async def verify_payment(self, provider_payment_id: str) -> bool:
        """Extra server-side verification hook before unlocking content.

        For providers with a signed webhook, real verification (signature
        check / status lookup) belongs here. Native Telegram Payments already
        verifies via pre_checkout_query, so this defaults to True.
        """

    @abstractmethod
    def parse_callback(self, payload: dict) -> PaymentCallbackResult:
        """Normalize a raw provider event (webhook body, successful_payment,
        or mock button click) into a PaymentCallbackResult.
        """


class MockPaymentProvider(PaymentProvider):
    """Local/dev provider: "payment" succeeds immediately when the user taps
    the confirmation button. No real money, no external calls. Useful for
    testing the full flow before real payment credentials are available.
    """

    name = "mock"

    async def create_payment(
        self, *, amount: int, currency: str, description: str
    ) -> PaymentIntent:
        provider_payment_id = f"mock_{uuid.uuid4().hex}"
        return PaymentIntent(
            provider_payment_id=provider_payment_id,
            amount=amount,
            currency=currency,
            description=description,
        )

    async def verify_payment(self, provider_payment_id: str) -> bool:
        return True

    def parse_callback(self, payload: dict) -> PaymentCallbackResult:
        return PaymentCallbackResult(
            provider_payment_id=payload["provider_payment_id"],
            amount=payload["amount"],
            currency=payload["currency"],
            succeeded=True,
        )


class TelegramPaymentProvider(PaymentProvider):
    """Native Telegram Payments.

    `create_payment` only prepares the intent; the actual invoice is sent by
    the bot handler via `bot.send_invoice(...)` using `extra["payload"]` as
    the Telegram invoice payload (== our provider_payment_id, so the
    subsequent `successful_payment` update can be matched back idempotently).

    To go live: set PAYMENT_PROVIDER=telegram and PAYMENT_TOKEN to the
    provider token issued by BotFather (Payments -> connect a real Russian
    provider, e.g. a bank/acquiring partner supported by Telegram in your
    region). No code changes are required beyond configuration.
    """

    name = "telegram"

    async def create_payment(
        self, *, amount: int, currency: str, description: str
    ) -> PaymentIntent:
        provider_payment_id = f"tg_{uuid.uuid4().hex}"
        return PaymentIntent(
            provider_payment_id=provider_payment_id,
            amount=amount,
            currency=currency,
            description=description,
            extra={"payload": provider_payment_id},
        )

    async def verify_payment(self, provider_payment_id: str) -> bool:
        # Telegram already verifies via pre_checkout_query before charging.
        return True

    def parse_callback(self, payload: dict) -> PaymentCallbackResult:
        # `payload` is expected to carry the aiogram SuccessfulPayment fields.
        return PaymentCallbackResult(
            provider_payment_id=payload["invoice_payload"],
            amount=payload["total_amount"],
            currency=payload["currency"],
            succeeded=True,
        )


YOOKASSA_API_BASE = "https://api.yookassa.ru/v3"
# YooKassa's "redirect" confirmation type requires *some* absolute HTTPS URL
# to send the user back to after they pay on YooKassa's hosted page. Since
# this bot has no web frontend of its own, that's just Telegram itself — the
# user taps back to the chat and presses "Проверить оплату" there. The
# return trip is not what drives our logic; polling verify_payment is.
YOOKASSA_RETURN_URL = "https://t.me/"


class YooKassaPaymentProvider(PaymentProvider):
    """Real integration against the YooKassa REST API (api.yookassa.ru/v3).

    Uses the *shop* credentials (YOOKASSA_SHOP_ID / YOOKASSA_SHOP_API_KEY) —
    the merchant-facing "accept a payment" API — since that is the only
    thing this product does (charge the user for a report). YooKassa's
    separate *agent* credentials authenticate a payouts API (sending money
    out to a sub-merchant) that has no corresponding feature in this MVP;
    they are accepted as configuration (see app.config.Settings) but
    deliberately not used here, to avoid wiring credentials into a flow they
    don't belong to.

    Confirmation type is "redirect": create_payment() returns a
    confirmation_url the user opens to enter card details. This bot has no
    public HTTPS endpoint for YooKassa's webhook, so status is verified by
    *polling* GET /payments/{id} when the user taps "Я оплатил" in Telegram
    — the authoritative status always comes from that response, never from
    the client or from Telegram. A webhook receiver can be added later
    without touching parse_callback, since a webhook delivers the same
    Payment JSON shape this already parses.
    """

    name = "yookassa"

    def _auth(self) -> aiohttp.BasicAuth:
        return aiohttp.BasicAuth(settings.yookassa_shop_id, settings.yookassa_shop_api_key)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        idempotence_key: str | None = None,
    ) -> dict:
        if not settings.yookassa_configured:
            raise RuntimeError(
                "YooKassa is not configured (YOOKASSA_SHOP_ID / YOOKASSA_SHOP_API_KEY)"
            )

        headers = {"Idempotence-Key": idempotence_key} if idempotence_key else {}

        async with (
            aiohttp.ClientSession() as http,
            http.request(
                method,
                f"{YOOKASSA_API_BASE}{path}",
                auth=self._auth(),
                json=json,
                headers=headers,
            ) as response,
        ):
            data = await response.json()
            if response.status >= 400:
                logger.warning(
                    "YooKassa API error %s on %s %s: %s",
                    response.status,
                    method,
                    path,
                    data.get("description") or data.get("type") or "unknown error",
                )
                raise RuntimeError(f"YooKassa API error {response.status}")
            return data

    async def create_payment(
        self, *, amount: int, currency: str, description: str
    ) -> PaymentIntent:
        data = await self._request(
            "POST",
            "/payments",
            idempotence_key=str(uuid.uuid4()),
            json={
                "amount": {"value": f"{amount:.2f}", "currency": currency},
                "confirmation": {"type": "redirect", "return_url": YOOKASSA_RETURN_URL},
                "capture": True,
                "description": description,
            },
        )
        return PaymentIntent(
            provider_payment_id=data["id"],
            amount=amount,
            currency=currency,
            description=description,
            extra={"confirmation_url": data.get("confirmation", {}).get("confirmation_url", "")},
        )

    async def check_status(self, provider_payment_id: str) -> dict:
        """YooKassa-specific: fetch the current authoritative payment state
        from the gateway. Used by the "Проверить оплату" button, and as the
        input to parse_callback (see class docstring — no webhook receiver
        exists in this MVP, so this is how confirmation actually happens).
        """
        return await self._request("GET", f"/payments/{provider_payment_id}")

    async def verify_payment(self, provider_payment_id: str) -> bool:
        data = await self.check_status(provider_payment_id)
        return data.get("status") == "succeeded" and bool(data.get("paid"))

    def parse_callback(self, payload: dict) -> PaymentCallbackResult:
        """`payload` is a YooKassa Payment object (from check_status(), or a
        future webhook body — both share this exact JSON shape).
        """
        amount_block = payload.get("amount", {})
        return PaymentCallbackResult(
            provider_payment_id=payload["id"],
            amount=int(round(float(amount_block.get("value", 0)))),
            currency=amount_block.get("currency", ""),
            succeeded=payload.get("status") == "succeeded" and bool(payload.get("paid")),
        )


def get_payment_provider(provider_name: str) -> PaymentProvider:
    if provider_name == "telegram":
        return TelegramPaymentProvider()
    if provider_name == "yookassa":
        return YooKassaPaymentProvider()
    return MockPaymentProvider()
