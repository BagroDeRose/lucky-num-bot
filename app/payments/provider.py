"""Payment provider abstraction.

Business logic never talks to a concrete payment gateway directly — it goes
through this interface. This keeps the door open to swapping in a real
Russian payment provider later without touching handlers or services.

Two implementations ship with the MVP:

* `MockPaymentProvider` — instant, local-only "payment" for development and
  tests. No real money moves.
* `TelegramPaymentProvider` — uses native Telegram Payments (bot.send_invoice
  / successful_payment), which Telegram itself connects to a real payment
  provider configured via BotFather (PAYMENT_TOKEN). This is the cleanest
  path to a real provider without adding a bespoke HTTP integration.

A real third-party gateway (e.g. a Russian acquiring provider) can be added
later by implementing this same interface — see the class docstrings below
for exactly which methods to fill in.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


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


def get_payment_provider(provider_name: str) -> PaymentProvider:
    if provider_name == "telegram":
        return TelegramPaymentProvider()
    return MockPaymentProvider()
