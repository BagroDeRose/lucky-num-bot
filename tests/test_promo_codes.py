"""Promo code creation, validation, atomic activation and pricing."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from _engagement_fakes import make_user, msk, session_factory  # noqa: F401 - fixture
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import PromoCode, PromoRedemption, PromoType, RedemptionStatus
from app.engagement import promo as P

NOW = msk(2026, 9, 16)


async def _promo(
    session: AsyncSession,
    code: str = "LUCKY-TEST",
    *,
    percent: int = 25,
    limit: int = 100,
    valid_from: dt.datetime | None = None,
    valid_until: dt.datetime | None = None,
    target_user_id: int | None = None,
    promo_type: PromoType = PromoType.WEEKLY_PUBLIC,
) -> PromoCode:
    promo = await P.create_promo(
        session,
        code=code,
        promo_type=promo_type,
        discount_percent=percent,
        max_activations=limit,
        valid_from=valid_from or NOW - dt.timedelta(days=1),
        valid_until=valid_until or NOW + dt.timedelta(days=1),
        target_user_id=target_user_id,
    )
    await session.commit()
    return promo


async def test_create_normalizes_code_to_upper_case(session: AsyncSession) -> None:
    promo = await _promo(session, " lucky-abc ")
    assert promo.code == "LUCKY-ABC"


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"code": "!!"}, "Код"),
        ({"discount_percent": 0}, "Скидка"),
        ({"discount_percent": 100}, "Скидка"),
        ({"max_activations": 0}, "Лимит"),
        ({"valid_until": NOW - dt.timedelta(days=2)}, "окончания"),
    ],
)
async def test_create_rejects_invalid_input(session: AsyncSession, kwargs: dict, fragment: str) -> None:
    params = {
        "code": "GOOD-CODE",
        "promo_type": PromoType.ADMIN_MANUAL,
        "discount_percent": 10,
        "max_activations": 5,
        "valid_from": NOW - dt.timedelta(days=1),
        "valid_until": NOW + dt.timedelta(days=1),
    } | kwargs
    with pytest.raises(P.PromoValidationError) as exc:
        await P.create_promo(session, **params)
    assert fragment in exc.value.message


async def test_duplicate_code_rejected_case_insensitively(session: AsyncSession) -> None:
    await _promo(session, "SAME-CODE")
    with pytest.raises(P.PromoValidationError):
        await _promo(session, "same-code")


def test_generated_codes_are_random_and_valid() -> None:
    codes = {P.generate_code("LUCKY", 4) for _ in range(50)}
    assert len(codes) > 45
    assert all(P.normalize_code(code) == code for code in codes)


async def test_activation_is_case_insensitive(session: AsyncSession) -> None:
    user = await make_user(session, 1, "a")
    await _promo(session, "LUCKY-7K2P")
    result = await P.activate(session, user, "  lucky-7k2p ", NOW)
    assert result.outcome is P.ActivationOutcome.ACTIVATED


async def test_unknown_code_is_invalid(session: AsyncSession) -> None:
    user = await make_user(session, 1, "a")
    assert (await P.activate(session, user, "NOPE-NOPE", NOW)).outcome is P.ActivationOutcome.INVALID
    assert (await P.activate(session, user, "", NOW)).outcome is P.ActivationOutcome.INVALID


async def test_expired_code_is_invalid(session: AsyncSession) -> None:
    user = await make_user(session, 1, "a")
    await _promo(session, valid_from=NOW - dt.timedelta(days=8), valid_until=NOW - dt.timedelta(days=1))
    assert (await P.activate(session, user, "LUCKY-TEST", NOW)).outcome is P.ActivationOutcome.INVALID


async def test_not_yet_started_code_is_invalid(session: AsyncSession) -> None:
    user = await make_user(session, 1, "a")
    await _promo(session, valid_from=NOW + dt.timedelta(hours=1), valid_until=NOW + dt.timedelta(days=7))
    assert (await P.activate(session, user, "LUCKY-TEST", NOW)).outcome is P.ActivationOutcome.INVALID


async def test_inactive_code_is_invalid(session: AsyncSession) -> None:
    user = await make_user(session, 1, "a")
    promo = await _promo(session)
    assert await P.deactivate(session, promo.id) is True
    assert await P.deactivate(session, promo.id) is False
    await session.commit()
    assert (await P.activate(session, user, "LUCKY-TEST", NOW)).outcome is P.ActivationOutcome.INVALID


async def test_same_user_cannot_activate_twice(session: AsyncSession) -> None:
    user = await make_user(session, 1, "a")
    promo = await _promo(session)
    assert (await P.activate(session, user, "LUCKY-TEST", NOW)).outcome is P.ActivationOutcome.ACTIVATED
    await session.commit()
    assert (await P.activate(session, user, "LUCKY-TEST", NOW)).outcome is P.ActivationOutcome.ALREADY_USED
    await session.commit()
    await session.refresh(promo)
    assert promo.activations_count == 1


async def test_single_activation_limit(session: AsyncSession) -> None:
    first = await make_user(session, 1, "a")
    second = await make_user(session, 2, None)
    promo = await _promo(session, limit=1)
    assert (await P.activate(session, first, "LUCKY-TEST", NOW)).outcome is P.ActivationOutcome.ACTIVATED
    await session.commit()
    assert (await P.activate(session, second, "LUCKY-TEST", NOW)).outcome is P.ActivationOutcome.EXHAUSTED
    await session.commit()
    await session.refresh(promo)
    assert promo.activations_count == 1


async def test_hundred_activations_then_exhausted(session: AsyncSession) -> None:
    promo = await _promo(session, limit=100)
    for telegram_id in range(1, 101):
        user = await make_user(session, telegram_id, f"u{telegram_id}")
        result = await P.activate(session, user, "LUCKY-TEST", NOW)
        await session.commit()
        assert result.outcome is P.ActivationOutcome.ACTIVATED
    late = await make_user(session, 101, "late")
    assert (await P.activate(session, late, "LUCKY-TEST", NOW)).outcome is P.ActivationOutcome.EXHAUSTED
    await session.refresh(promo)
    assert promo.activations_count == 100


async def test_personal_code_only_for_target_user(session: AsyncSession) -> None:
    owner = await make_user(session, 1, "owner")
    stranger = await make_user(session, 2, "stranger")
    await _promo(session, "TOP50-ABCDEF", percent=50, limit=1, target_user_id=owner.id,
                 promo_type=PromoType.TOP_REWARD)
    assert (await P.activate(session, stranger, "TOP50-ABCDEF", NOW)).outcome is P.ActivationOutcome.NOT_FOR_YOU
    assert (await P.activate(session, owner, "TOP50-ABCDEF", NOW)).outcome is P.ActivationOutcome.ACTIVATED


async def test_expired_personal_code_is_invalid_even_for_owner(session: AsyncSession) -> None:
    owner = await make_user(session, 1, "owner")
    await _promo(session, "TOP50-OLDOLD", percent=50, limit=1, target_user_id=owner.id,
                 valid_from=NOW - dt.timedelta(days=10), valid_until=NOW - dt.timedelta(days=3))
    assert (await P.activate(session, owner, "TOP50-OLDOLD", NOW)).outcome is P.ActivationOutcome.INVALID


async def test_concurrent_last_activation_only_one_wins(
    session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    """Many users race for the single remaining slot, each in its own
    session/connection. Exactly one may win and the counter never exceeds
    the limit.
    """
    async with session_factory() as s:
        users = [await make_user(s, telegram_id, f"u{telegram_id}") for telegram_id in range(1, 11)]
        await _promo(s, limit=1)

    async def attempt(user_index: int) -> P.ActivationOutcome:
        async with session_factory() as s:
            result = await P.activate(s, users[user_index], "LUCKY-TEST", NOW)
            await s.commit()
            return result.outcome

    outcomes = await asyncio.gather(*(attempt(i) for i in range(10)))
    assert outcomes.count(P.ActivationOutcome.ACTIVATED) == 1
    assert outcomes.count(P.ActivationOutcome.EXHAUSTED) == 9

    async with session_factory() as s:
        promo = (await s.execute(select(PromoCode))).scalar_one()
        assert promo.activations_count == 1
        assert (await s.execute(select(func.count(PromoRedemption.id)))).scalar_one() == 1


async def test_concurrent_duplicate_activation_by_same_user(
    session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    async with session_factory() as s:
        user = await make_user(s, 1, "a")
        await _promo(s, limit=100)

    async def attempt() -> P.ActivationOutcome:
        async with session_factory() as s:
            result = await P.activate(s, user, "LUCKY-TEST", NOW)
            await s.commit()
            return result.outcome

    outcomes = await asyncio.gather(*(attempt() for _ in range(5)))
    assert outcomes.count(P.ActivationOutcome.ACTIVATED) == 1
    async with session_factory() as s:
        promo = (await s.execute(select(PromoCode))).scalar_one()
        assert promo.activations_count == 1


@pytest.mark.parametrize(
    ("percent", "expected"), [(None, 99), (25, 74), (50, 49), (99, 1), (10, 89)]
)
def test_discounted_price_integer_rub(percent: int | None, expected: int) -> None:
    assert P.discounted_price(99, percent) == expected


def test_price_never_below_one_rub() -> None:
    assert P.discounted_price(1, 99) == 1


async def test_reservation_picks_best_discount_and_never_stacks(session: AsyncSession) -> None:
    user = await make_user(session, 1, "a")
    await _promo(session, "WEEK-25", percent=25)
    await _promo(session, "GIFT-50", percent=50, promo_type=PromoType.ADMIN_MANUAL)
    for code in ("WEEK-25", "GIFT-50"):
        await P.activate(session, user, code, NOW)
    await session.commit()

    first = await P.reserve_best_discount(session, user.id, NOW)
    second = await P.reserve_best_discount(session, user.id, NOW)
    third = await P.reserve_best_discount(session, user.id, NOW)
    assert first is not None and first.discount_percent == 50
    assert second is not None and second.discount_percent == 25
    assert third is None


async def test_release_and_consume_lifecycle(session: AsyncSession) -> None:
    user = await make_user(session, 1, "a")
    await _promo(session)
    await P.activate(session, user, "LUCKY-TEST", NOW)
    await session.commit()

    reservation = await P.reserve_best_discount(session, user.id, NOW)
    assert reservation is not None
    await P.release_reservation(session, reservation)
    await session.commit()
    assert await P.best_unused_discount(session, user.id, NOW) == 25

    reservation = await P.reserve_best_discount(session, user.id, NOW)
    assert reservation is not None
    await P.consume_for_payment(session, payment_id=999)  # unrelated payment: no-op
    await session.commit()
    redemption = (await session.execute(select(PromoRedemption))).scalar_one()
    assert redemption.status == RedemptionStatus.RESERVED


async def test_stale_unattached_reservation_is_released(session: AsyncSession) -> None:
    user = await make_user(session, 1, "a")
    await _promo(session, valid_until=NOW + dt.timedelta(days=3))
    await P.activate(session, user, "LUCKY-TEST", NOW)
    await session.commit()
    assert await P.reserve_best_discount(session, user.id, NOW) is not None
    await session.commit()
    later = NOW + P.STALE_RESERVATION_AFTER + dt.timedelta(minutes=1)
    assert await P.reserve_best_discount(session, user.id, later) is not None
