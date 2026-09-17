"""Promo codes: creation, atomic activation, pricing and the payment lifecycle.

LIFECYCLE OF AN ACTIVATION (promo_code_redemptions.status)

    applied   the user entered a valid code; it waits for their next purchase
    reserved  bound to exactly one payment, claimed *before* the provider
              payment is created, so one activation can never discount two
              payments
    consumed  that payment succeeded

A failed/cancelled payment hands the activation back (reserved -> applied,
see repositories.mark_payment_failed). Entering a code counts toward its
activation limit immediately — that is what "activation" means for the
public weekly code — and each user can activate a given code once.

ATOMICITY

Activation increments activations_count with a single conditional UPDATE
("... WHERE activations_count < max_activations") and inserts the
redemption (UNIQUE promo_code_id, user_id) inside one SAVEPOINT. Two users
racing for the last slot cannot both succeed: on SQLite the second writer
waits for the first transaction and then matches zero rows; on PostgreSQL
the row lock re-evaluates the WHERE clause. A CHECK constraint on the table
is the database-level backstop.

PRICING

Money is integer RUB throughout this project (payments.amount). The
discounted price is floor(price * (100 - percent) / 100) in integer maths —
rounding down so the user never pays more than the advertised discount — with
a floor of 1 RUB, because payment providers reject zero-amount payments.
99 RUB -> 25% = 74 RUB, 50% = 49 RUB. Discounts never stack: a purchase uses
the single best applied activation.
"""

from __future__ import annotations

import datetime as dt
import re
import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    PromoCode,
    PromoRedemption,
    PromoType,
    RedemptionStatus,
    User,
)
from app.engagement.periods import as_utc, utcnow
from app.logging import get_logger

logger = get_logger(__name__)

MIN_DISCOUNT_PERCENT = 1
# 100% would price a report at 0 RUB, which no payment provider accepts and
# for which no free-delivery path exists.
MAX_DISCOUNT_PERCENT = 99
MIN_CHARGE_RUB = 1
MAX_CODE_LENGTH = 32

# No 0/O or 1/I: codes are read from screenshots and typed by hand.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_RE = re.compile(r"^[A-Z0-9](?:[A-Z0-9-]{1,30}[A-Z0-9])$")

# A reservation that never got a payment attached (process crashed between
# claiming it and creating the payment) is released after this long.
STALE_RESERVATION_AFTER = dt.timedelta(minutes=15)


def normalize_code(raw: str | None) -> str | None:
    """Upper-case, trim, drop inner spaces; None if it cannot be a code."""
    if raw is None:
        return None
    code = re.sub(r"\s+", "", raw).upper()
    if len(code) > MAX_CODE_LENGTH or not _CODE_RE.match(code):
        return None
    return code


def generate_code(prefix: str, length: int) -> str:
    """Random code from a cryptographic source — never derived from user IDs."""
    body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))
    return f"{prefix}-{body}"


def discounted_price(base_price: int, discount_percent: int | None) -> int:
    if not discount_percent:
        return base_price
    return max(MIN_CHARGE_RUB, base_price * (100 - discount_percent) // 100)


class PromoValidationError(ValueError):
    """Invalid admin input for a new promo code; `message` is user-facing."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


async def get_by_code(session: AsyncSession, code: str) -> PromoCode | None:
    result = await session.execute(select(PromoCode).where(PromoCode.code == code))
    return result.scalar_one_or_none()


async def create_promo(
    session: AsyncSession,
    *,
    code: str,
    promo_type: PromoType,
    discount_percent: int,
    max_activations: int,
    valid_from: dt.datetime,
    valid_until: dt.datetime,
    target_user_id: int | None = None,
    created_by_telegram_id: int | None = None,
) -> PromoCode:
    """Validate and insert. Raises PromoValidationError for bad input,
    including a code that already exists (codes are globally unique so a
    typed code can never be ambiguous).
    """
    normalized = normalize_code(code)
    if normalized is None:
        raise PromoValidationError(
            "Код должен быть 3–32 символа: латинские буквы, цифры и дефис внутри."
        )
    if not MIN_DISCOUNT_PERCENT <= discount_percent <= MAX_DISCOUNT_PERCENT:
        raise PromoValidationError(
            f"Скидка должна быть от {MIN_DISCOUNT_PERCENT} до {MAX_DISCOUNT_PERCENT}%."
        )
    if max_activations < 1:
        raise PromoValidationError("Лимит активаций должен быть не меньше 1.")
    if as_utc(valid_until) <= as_utc(valid_from):
        raise PromoValidationError("Дата окончания должна быть позже даты начала.")

    promo = PromoCode(
        code=normalized,
        promo_type=promo_type,
        discount_percent=discount_percent,
        max_activations=max_activations,
        activations_count=0,
        valid_from=as_utc(valid_from),
        valid_until=as_utc(valid_until),
        is_active=True,
        target_user_id=target_user_id,
        created_by_telegram_id=created_by_telegram_id,
    )
    try:
        async with session.begin_nested():
            session.add(promo)
            await session.flush()
    except IntegrityError as exc:
        raise PromoValidationError("Такой промокод уже существует.") from exc
    return promo


async def create_generated_promo(
    session: AsyncSession,
    *,
    prefix: str,
    length: int,
    promo_type: PromoType,
    discount_percent: int,
    max_activations: int,
    valid_from: dt.datetime,
    valid_until: dt.datetime,
    target_user_id: int | None = None,
    created_by_telegram_id: int | None = None,
) -> PromoCode:
    """Create with a random code, retrying on the (astronomically rare)
    collision with an existing code.
    """
    for _ in range(10):
        code = generate_code(prefix, length)
        if await get_by_code(session, code) is not None:
            continue
        return await create_promo(
            session,
            code=code,
            promo_type=promo_type,
            discount_percent=discount_percent,
            max_activations=max_activations,
            valid_from=valid_from,
            valid_until=valid_until,
            target_user_id=target_user_id,
            created_by_telegram_id=created_by_telegram_id,
        )
    raise RuntimeError("could not generate a unique promo code")


class ActivationOutcome(StrEnum):
    ACTIVATED = "activated"
    INVALID = "invalid"  # unknown, inactive, not started or expired
    ALREADY_USED = "already_used"
    EXHAUSTED = "exhausted"
    NOT_FOR_YOU = "not_for_you"


@dataclass(frozen=True)
class ActivationResult:
    outcome: ActivationOutcome
    promo: PromoCode | None = None


class _NoCapacity(Exception):
    pass


def _is_live(promo: PromoCode, now: dt.datetime) -> bool:
    return (
        promo.is_active
        and as_utc(promo.valid_from) <= now < as_utc(promo.valid_until)
    )


async def activate(
    session: AsyncSession, user: User, raw_code: str, now: dt.datetime | None = None
) -> ActivationResult:
    """Activate a code for `user`. Caller commits."""
    now = as_utc(now or utcnow())
    code = normalize_code(raw_code)
    if code is None:
        return ActivationResult(ActivationOutcome.INVALID)

    promo = await get_by_code(session, code)
    if promo is None or not _is_live(promo, now):
        return ActivationResult(ActivationOutcome.INVALID)
    if promo.target_user_id is not None and promo.target_user_id != user.id:
        return ActivationResult(ActivationOutcome.NOT_FOR_YOU)

    already = await session.execute(
        select(PromoRedemption.id).where(
            PromoRedemption.promo_code_id == promo.id, PromoRedemption.user_id == user.id
        )
    )
    if already.scalar_one_or_none() is not None:
        return ActivationResult(ActivationOutcome.ALREADY_USED, promo)

    try:
        async with session.begin_nested():
            claimed = cast(
                CursorResult,
                await session.execute(
                    update(PromoCode)
                    .where(
                        PromoCode.id == promo.id,
                        PromoCode.is_active.is_(True),
                        PromoCode.activations_count < PromoCode.max_activations,
                        PromoCode.valid_from <= now,
                        PromoCode.valid_until > now,
                    )
                    .values(activations_count=PromoCode.activations_count + 1, updated_at=now)
                    .execution_options(synchronize_session=False)
                ),
            )
            if claimed.rowcount == 0:
                raise _NoCapacity()
            session.add(
                PromoRedemption(
                    promo_code_id=promo.id,
                    user_id=user.id,
                    status=RedemptionStatus.APPLIED,
                    redeemed_at=now,
                )
            )
            await session.flush()
    except IntegrityError:
        # Same user activating the same code concurrently: the savepoint
        # rollback also undid this attempt's increment.
        return ActivationResult(ActivationOutcome.ALREADY_USED, promo)
    except _NoCapacity:
        await session.refresh(promo)
        if _is_live(promo, now):
            return ActivationResult(ActivationOutcome.EXHAUSTED, promo)
        return ActivationResult(ActivationOutcome.INVALID)

    await session.refresh(promo)
    logger.info("Promo %s activated (promo_id=%s, user_id=%s)", promo.promo_type, promo.id, user.id)
    return ActivationResult(ActivationOutcome.ACTIVATED, promo)


# --- payment lifecycle ----------------------------------------------------


@dataclass(frozen=True)
class Reservation:
    redemption_id: int
    discount_percent: int


async def _release_stale_reservations(
    session: AsyncSession, user_id: int, now: dt.datetime
) -> None:
    await session.execute(
        update(PromoRedemption)
        .where(
            PromoRedemption.user_id == user_id,
            PromoRedemption.status == RedemptionStatus.RESERVED,
            PromoRedemption.payment_id.is_(None),
            PromoRedemption.reserved_at < now - STALE_RESERVATION_AFTER,
        )
        .values(status=RedemptionStatus.APPLIED, reserved_at=None)
        .execution_options(synchronize_session=False)
    )


async def reserve_best_discount(
    session: AsyncSession, user_id: int, now: dt.datetime | None = None
) -> Reservation | None:
    """Claim the user's best usable activation for a payment about to be
    created. The claim is a conditional UPDATE, so two payments being
    created at once can never both take the same activation.
    """
    now = as_utc(now or utcnow())
    await _release_stale_reservations(session, user_id, now)
    candidates = await session.execute(
        select(PromoRedemption.id, PromoCode.discount_percent)
        .join(PromoCode, PromoCode.id == PromoRedemption.promo_code_id)
        .where(
            PromoRedemption.user_id == user_id,
            PromoRedemption.status == RedemptionStatus.APPLIED,
            PromoCode.is_active.is_(True),
            PromoCode.valid_until > now,
        )
        .order_by(
            PromoCode.discount_percent.desc(),
            PromoCode.valid_until.asc(),
            PromoRedemption.id.asc(),
        )
    )
    for redemption_id, discount_percent in candidates.all():
        claimed = cast(
            CursorResult,
            await session.execute(
                update(PromoRedemption)
                .where(
                    PromoRedemption.id == redemption_id,
                    PromoRedemption.status == RedemptionStatus.APPLIED,
                )
                .values(status=RedemptionStatus.RESERVED, reserved_at=now)
                .execution_options(synchronize_session=False)
            ),
        )
        if claimed.rowcount == 1:
            return Reservation(redemption_id=redemption_id, discount_percent=discount_percent)
    return None


async def attach_reservation(session: AsyncSession, reservation: Reservation, payment_id: int) -> None:
    await session.execute(
        update(PromoRedemption)
        .where(PromoRedemption.id == reservation.redemption_id)
        .values(payment_id=payment_id)
        .execution_options(synchronize_session=False)
    )


async def release_reservation(session: AsyncSession, reservation: Reservation) -> None:
    """Undo a claim whose payment was never created (provider error)."""
    await session.execute(
        update(PromoRedemption)
        .where(
            PromoRedemption.id == reservation.redemption_id,
            PromoRedemption.status == RedemptionStatus.RESERVED,
            PromoRedemption.payment_id.is_(None),
        )
        .values(status=RedemptionStatus.APPLIED, reserved_at=None)
        .execution_options(synchronize_session=False)
    )


async def consume_for_payment(
    session: AsyncSession, payment_id: int, now: dt.datetime | None = None
) -> None:
    """Mark the activation used once its payment succeeded. Idempotent."""
    await session.execute(
        update(PromoRedemption)
        .where(
            PromoRedemption.payment_id == payment_id,
            PromoRedemption.status == RedemptionStatus.RESERVED,
        )
        .values(status=RedemptionStatus.CONSUMED, consumed_at=as_utc(now or utcnow()))
        .execution_options(synchronize_session=False)
    )


async def discount_for_payment(session: AsyncSession, payment_id: int) -> int | None:
    result = await session.execute(
        select(PromoCode.discount_percent)
        .join(PromoRedemption, PromoRedemption.promo_code_id == PromoCode.id)
        .where(PromoRedemption.payment_id == payment_id)
    )
    return result.scalar_one_or_none()


async def best_unused_discount(
    session: AsyncSession, user_id: int, now: dt.datetime | None = None
) -> int | None:
    """The discount the user's next new payment would receive, if any."""
    now = as_utc(now or utcnow())
    result = await session.execute(
        select(func.max(PromoCode.discount_percent))
        .join(PromoRedemption, PromoRedemption.promo_code_id == PromoCode.id)
        .where(
            PromoRedemption.user_id == user_id,
            PromoRedemption.status == RedemptionStatus.APPLIED,
            PromoCode.is_active.is_(True),
            PromoCode.valid_until > now,
        )
    )
    return result.scalar_one_or_none()


async def deactivate(session: AsyncSession, promo_id: int) -> bool:
    result = cast(
        CursorResult,
        await session.execute(
            update(PromoCode)
            .where(PromoCode.id == promo_id, PromoCode.is_active.is_(True))
            .values(is_active=False, updated_at=utcnow())
            .execution_options(synchronize_session=False)
        ),
    )
    return result.rowcount == 1


async def list_promos(
    session: AsyncSession, *, active: bool, now: dt.datetime | None = None, limit: int = 20
) -> list[PromoCode]:
    now = as_utc(now or utcnow())
    live = (PromoCode.is_active.is_(True)) & (PromoCode.valid_until > now)
    stmt = select(PromoCode).where(live if active else ~live)
    stmt = stmt.order_by(PromoCode.created_at.desc(), PromoCode.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def get_promo(session: AsyncSession, promo_id: int) -> PromoCode | None:
    return await session.get(PromoCode, promo_id)


async def redemption_counts(session: AsyncSession, promo_id: int) -> dict[str, int]:
    rows = await session.execute(
        select(PromoRedemption.status, func.count(PromoRedemption.id))
        .where(PromoRedemption.promo_code_id == promo_id)
        .group_by(PromoRedemption.status)
    )
    return {status: count for status, count in rows.all()}
