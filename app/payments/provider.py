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

import ssl
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import aiohttp
import certifi

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

    async def confirmation_url_for(self, provider_payment_id: str) -> str | None:
        """Recover the URL the user must open to finish an *already created*
        payment, without creating a new one.

        Needed because PaymentIntent.extra is in-memory only (the payments
        table has no column for it — see app.database.models.Payment), so a
        confirmation URL is lost the moment the request that created the
        payment ends. When a pending payment is later reused, the URL has to
        be fetched back from the provider rather than re-minted.

        Returns None when the provider has no such concept (Mock button,
        Telegram invoice) or when the URL genuinely cannot be recovered
        (payment already in a terminal state, provider unreachable).
        Implementations MUST NOT create a payment here.
        """
        return None


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

# YooKassa's own docs state they may take up to 30s server-side before
# giving up and returning HTTP 500. This is set comfortably above that
# instead of relying on aiohttp's 5-minute default, so a stuck connection
# fails predictably rather than leaving a Telegram callback hanging.
YOOKASSA_REQUEST_TIMEOUT_SECONDS = 35

# Hard limit documented by the YooKassa API for the `description` field.
YOOKASSA_DESCRIPTION_MAX_LENGTH = 128

# Explicit CA bundle for TLS verification (never disabled — see _request).
#
# aiohttp's default TCPConnector builds its SSL context via
# ssl.create_default_context() with no `cafile`, which falls back to
# OpenSSL's compiled-in default paths (ssl.get_default_verify_paths()).
# On Windows those paths (typically under "Common Files\SSL") frequently
# don't exist or aren't populated, because Windows keeps its trusted roots
# in its own certificate store, not there — unlike curl.exe, which uses
# Windows' native Schannel/WinTrust APIs and so verifies successfully
# against the very same server. Python's `ssl` module has no equivalent
# fallback, so the request fails with SSLCertVerificationError even though
# the server, DNS, and TCP/TLS handshake are all fine. `certifi` ships a
# maintained, platform-independent CA bundle; pointing the context at it
# explicitly (via `cafile=certifi.where()`) fixes this without weakening
# verification in any way — hostname checking and certificate validation
# both stay fully enabled (ssl.create_default_context()'s defaults).
YOOKASSA_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


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

    def _auth_header(self) -> str:
        # aiohttp.BasicAuth (the `auth=` request kwarg) is deprecated as of
        # aiohttp 3.x in favor of building the Authorization header directly.
        return aiohttp.encode_basic_auth(settings.yookassa_shop_id, settings.yookassa_shop_api_key)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        idempotence_key: str | None = None,
    ) -> dict:
        """Perform one HTTP call against the YooKassa API.

        Deliberately makes exactly one attempt — no automatic retry. A POST
        (create_payment) is not safe to blindly retry with a *new*
        Idempotence-Key: per YooKassa's docs, a different key is treated as
        a brand-new operation, which would risk creating a duplicate
        payment. A GET (check_status) is naturally safe to retry, but this
        MVP's "retry" is simply the user tapping "Проверить оплату" again —
        adequate at this request volume, and simpler than building
        backoff/retry logic that has no real payoff here.
        """
        if not settings.yookassa_configured:
            raise RuntimeError(
                "YooKassa is not configured (YOOKASSA_SHOP_ID / YOOKASSA_SHOP_API_KEY)"
            )

        headers = {"Authorization": self._auth_header()}
        if idempotence_key:
            headers["Idempotence-Key"] = idempotence_key
        timeout = aiohttp.ClientTimeout(total=YOOKASSA_REQUEST_TIMEOUT_SECONDS)
        # The connector — not the session — is what actually owns the SSL
        # context; a plain ClientSession(timeout=...) would silently fall
        # back to aiohttp's default TCPConnector and its default (OS-path-
        # dependent) SSL context. Verification stays fully enabled — this
        # passes a real, trusted CA bundle, never a disabled/unverified
        # mode — see YOOKASSA_SSL_CONTEXT above for why it's needed.
        connector = aiohttp.TCPConnector(ssl=YOOKASSA_SSL_CONTEXT)

        try:
            async with (
                aiohttp.ClientSession(timeout=timeout, connector=connector) as http,
                http.request(
                    method,
                    f"{YOOKASSA_API_BASE}{path}",
                    json=json,
                    headers=headers,
                ) as response,
            ):
                data = await response.json()
        except TimeoutError as exc:
            logger.warning("YooKassa API timeout on %s %s", method, path)
            raise RuntimeError("YooKassa API request timed out") from exc
        except aiohttp.ClientError as exc:
            logger.warning("YooKassa API connection error on %s %s: %s", method, path, exc)
            raise RuntimeError("YooKassa API connection error") from exc

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
        # YooKassa hard-rejects descriptions over 128 characters; truncate
        # defensively rather than letting an edge-case input fail the call.
        api_description = description[:YOOKASSA_DESCRIPTION_MAX_LENGTH]

        data = await self._request(
            "POST",
            "/payments",
            idempotence_key=str(uuid.uuid4()),
            json={
                "amount": {"value": f"{amount:.2f}", "currency": currency},
                "confirmation": {"type": "redirect", "return_url": YOOKASSA_RETURN_URL},
                "capture": True,
                "description": api_description,
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

    async def confirmation_url_for(self, provider_payment_id: str) -> str | None:
        """Re-read an existing payment's confirmation URL via GET
        /payments/{id} — a read-only lookup that never creates a payment, so
        reusing a pending payment can never double-charge.

        YooKassa only returns a `confirmation.confirmation_url` while the
        payment is still awaiting the user; for a succeeded/canceled payment
        the block is absent, which correctly yields None (there is nothing
        left to pay). A provider/network failure also yields None rather than
        raising, so the caller can fall back to an honest "link unavailable"
        path instead of failing the whole interaction.
        """
        try:
            data = await self.check_status(provider_payment_id)
        except Exception:  # noqa: BLE001 - recovery is best-effort by contract
            logger.warning("Could not re-read YooKassa payment to recover its confirmation URL")
            return None
        url = data.get("confirmation", {}).get("confirmation_url")
        return url or None

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
