"""Application-level payment flow: idempotent, provider-agnostic.

Handlers call into this service; the service is the only place that decides
when a payment is considered paid and an analysis unlocked.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import repositories as repo
from app.database.models import Analysis, Payment
from app.logging import get_logger
from app.payments.provider import PaymentIntent, get_payment_provider

logger = get_logger(__name__)

# Serializes the "reuse an existing pending payment, else create one" decision
# per analysis. That decision is a check-then-act: two concurrent taps on
# "Открыть полный разбор" can both see "no pending payment" and both call
# provider.create_payment(), which mints a *fresh* provider_payment_id each
# time — so the unique constraint cannot catch it, and the shop ends up with
# two real YooKassa payments for one analysis (one of them untracked by us,
# which would take a paying user's money without unlocking anything).
# Demonstrated with a concurrency regression test; under SQLite the write
# lock happens to hide it, but that is an accident of the storage engine and
# would disappear on PostgreSQL (the documented production target).
# In-memory like the report-generation lock in app.bot.handlers.payment —
# the bot runs as a single long-polling process (see README "Deployment").
_payment_creation_locks: dict[int, asyncio.Lock] = {}


def _lock_for_analysis(analysis_id: int) -> asyncio.Lock:
    lock = _payment_creation_locks.get(analysis_id)
    if lock is None:
        lock = asyncio.Lock()
        _payment_creation_locks[analysis_id] = lock
    return lock


class PaymentService:
    def __init__(self) -> None:
        self.provider = get_payment_provider(settings.payment_provider)

    async def start_payment(
        self, session: AsyncSession, *, user_id: int, analysis: Analysis
    ) -> tuple[Payment, PaymentIntent]:
        """Create a pending payment for an analysis and return the intent the
        handler should present to the user (mock button or Telegram invoice).

        Reuses an already-pending payment for the same analysis instead of
        minting a new provider_payment_id every time — otherwise repeatedly
        tapping "get full report" before paying would pile up an unbounded
        number of orphaned pending Payment rows, one per tap.
        """
        async with _lock_for_analysis(analysis.id):
            return await self._start_payment_locked(
                session, user_id=user_id, analysis=analysis
            )

    async def _start_payment_locked(
        self, session: AsyncSession, *, user_id: int, analysis: Analysis
    ) -> tuple[Payment, PaymentIntent]:
        description = f"LuckyNum: полный отчёт по номеру {analysis.number}"

        existing = await repo.get_latest_pending_payment(session, analysis.id, user_id)
        if existing is not None:
            # The reused row carries no confirmation URL: PaymentIntent.extra
            # is in-memory only and is never persisted, so the URL minted when
            # this payment was first created is long gone. Ask the provider to
            # re-read it (a read-only lookup — see confirmation_url_for), which
            # keeps this an actual *reuse* instead of minting a second payment
            # for the same analysis. Recovery is best-effort: when it returns
            # None the caller presents a recovery path rather than paying twice.
            extra: dict = {"payload": existing.provider_payment_id}
            recovered_url = await self.provider.confirmation_url_for(
                existing.provider_payment_id
            )
            if recovered_url:
                extra["confirmation_url"] = recovered_url
            intent = PaymentIntent(
                provider_payment_id=existing.provider_payment_id,
                amount=existing.amount,
                currency=existing.currency,
                description=description,
                extra=extra,
            )
            return existing, intent

        intent = await self.provider.create_payment(
            amount=settings.price_rub,
            currency=settings.currency,
            description=description,
        )
        payment = await repo.create_pending_payment(
            session,
            user_id=user_id,
            analysis_id=analysis.id,
            amount=intent.amount,
            currency=intent.currency,
            provider=self.provider.name,
            provider_payment_id=intent.provider_payment_id,
        )
        # Commit before releasing the per-analysis lock. The payment now
        # exists at the provider, so it must be durably recorded here: a
        # later failure rolling this row back would strand a real, payable
        # YooKassa payment that we no longer recognise (a user paying it
        # would get nothing). Committing inside the lock is also what makes
        # the next concurrent caller actually *see* this pending payment and
        # reuse it instead of minting a second one.
        await session.commit()
        return payment, intent

    async def confirm_payment(
        self, session: AsyncSession, callback_payload: dict
    ) -> Payment | None:
        """Process an incoming payment confirmation event.

        Returns the Payment if it was (idempotently) marked paid, or None if
        the payment could not be found or verification failed. Safe to call
        multiple times with the same event (duplicate Telegram retries) —
        will not double-unlock or double-process.
        """
        result = self.provider.parse_callback(callback_payload)
        if not result.succeeded:
            return None

        verified = await self.provider.verify_payment(result.provider_payment_id)
        if not verified:
            logger.warning("Payment verification failed for %s", result.provider_payment_id)
            return None

        payment = await repo.get_payment_by_provider_id(session, result.provider_payment_id)
        if payment is None:
            logger.warning("Unknown payment callback: %s", result.provider_payment_id)
            return None

        # Cross-check the callback's amount/currency against what we stored
        # server-side when the payment was created. The provider-reported
        # values are never used to *set* anything — only to confirm they
        # match our own trusted record — so a callback claiming a different
        # amount than what was actually offered cannot sneak past.
        if result.amount != payment.amount or result.currency != payment.currency:
            logger.warning(
                "Payment amount/currency mismatch for %s: callback=%s %s, expected=%s %s",
                result.provider_payment_id,
                result.amount,
                result.currency,
                payment.amount,
                payment.currency,
            )
            return None

        transitioned = await repo.mark_payment_paid(session, payment)
        if transitioned:
            logger.info("Payment %s marked paid (analysis_id=%s)", payment.id, payment.analysis_id)
        else:
            logger.info("Duplicate payment confirmation ignored: %s", payment.id)

        return payment
