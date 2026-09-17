"""TOP-5 weekly rewards: personalized one-time promo codes and their delivery.

IDEMPOTENCY

- Generation: weekly_rewards has UNIQUE (leaderboard_id, user_id) and
  UNIQUE (leaderboard_id, rank). Each reward and its promo code are created
  together inside one SAVEPOINT, so a concurrent or repeated run either
  creates both or neither, and never a second code for the same winner.
- Delivery: a reward is claimed with a conditional UPDATE
  (status -> "sending") before the Telegram call. Only the run whose UPDATE
  matched sends; any other run sees the claim and skips. State lives in the
  database, not in memory, so a restart cannot trigger a duplicate send.

FAILURES

One recipient failing never stops the others. A user who blocked the bot
(or deleted their account) is a permanent failure and is not retried
automatically. Transient failures are retried by later automatic runs up to
MAX_AUTO_DELIVERY_ATTEMPTS; beyond that, only an admin can retry.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Protocol, cast

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import case, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import (
    DeliveryStatus,
    PromoCode,
    PromoType,
    User,
    WeeklyLeaderboard,
    WeeklyReward,
)
from app.engagement import promo as promo_service
from app.engagement.leaderboard import get_entries, period_of
from app.engagement.messages import render_reward_message
from app.engagement.periods import app_timezone, as_utc, local_date, utcnow
from app.logging import get_logger

logger = get_logger(__name__)

TOP_REWARD_COUNT = 5
MAX_AUTO_DELIVERY_ATTEMPTS = 3
_ERROR_MAX_LENGTH = 255


class MessageSender(Protocol):
    async def send_message(self, chat_id: int | str, text: str) -> object: ...


def is_permanent_delivery_error(exc: BaseException) -> bool:
    if isinstance(exc, TelegramForbiddenError):
        return True
    return isinstance(exc, TelegramBadRequest) and "chat not found" in str(exc).lower()


def describe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:_ERROR_MAX_LENGTH]


def reward_valid_until(now: dt.datetime) -> dt.datetime:
    """Exclusive end of a reward code issued at `now`: TOP_REWARD_VALID_DAYS
    local calendar days counting the issue day, ending at local midnight
    (APP_TIMEZONE). A code issued on schedule (Monday 00:05) is valid through
    Sunday; one issued late (the bot was down, or a manual run on Wednesday)
    still gets the full period instead of expiring with the calendar week.
    """
    tz = app_timezone()
    days = max(1, settings.top_reward_valid_days)
    end_day = local_date(as_utc(now), tz) + dt.timedelta(days=days)
    return dt.datetime.combine(end_day, dt.time(0), tzinfo=tz).astimezone(dt.UTC)


async def ensure_rewards(
    session: AsyncSession, leaderboard: WeeklyLeaderboard, now: dt.datetime | None = None
) -> int:
    """Create missing TOP-5 rewards for a finalized week. Returns how many
    were created by this call (0 when everything already exists).
    """
    now = as_utc(now or utcnow())
    winners = await get_entries(session, leaderboard.id, limit=TOP_REWARD_COUNT)
    existing = await session.execute(
        select(WeeklyReward.user_id).where(WeeklyReward.leaderboard_id == leaderboard.id)
    )
    rewarded = set(existing.scalars().all())
    percent = settings.top_reward_discount_percent

    created = 0
    for entry in winners:
        if entry.user_id in rewarded or entry.report_count <= 0:
            continue
        try:
            async with session.begin_nested():
                promo = await promo_service.create_generated_promo(
                    session,
                    prefix=f"TOP{percent}",
                    length=6,
                    promo_type=PromoType.TOP_REWARD,
                    discount_percent=percent,
                    max_activations=1,
                    valid_from=now,
                    valid_until=reward_valid_until(now),
                    target_user_id=entry.user_id,
                )
                session.add(
                    WeeklyReward(
                        leaderboard_id=leaderboard.id,
                        user_id=entry.user_id,
                        rank=entry.rank,
                        report_count=entry.report_count,
                        promo_code_id=promo.id,
                        delivery_status=DeliveryStatus.PENDING,
                        delivery_attempts=0,
                    )
                )
                await session.flush()
        except IntegrityError:
            # Another run created this winner's reward first; its promo was
            # rolled back with the savepoint, so no orphan code remains.
            continue
        created += 1
        logger.info(
            "TOP reward generated (leaderboard_id=%s, rank=%s, user_id=%s, promo_id=%s)",
            leaderboard.id,
            entry.rank,
            entry.user_id,
            promo.id,
        )
    return created


@dataclass
class RewardSummary:
    generated: int = 0
    sent: int = 0
    failed: int = 0
    already_sent: int = 0
    not_retried: int = 0
    errors: list[str] = field(default_factory=list)


async def _set_delivery(session: AsyncSession, reward_id: int, **values: object) -> None:
    await session.execute(
        update(WeeklyReward)
        .where(WeeklyReward.id == reward_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def deliver_rewards(
    session: AsyncSession,
    bot: MessageSender,
    leaderboard: WeeklyLeaderboard,
    *,
    manual: bool = False,
) -> RewardSummary:
    """Send every undelivered reward for `leaderboard`, one claim at a time.

    `manual=True` (admin button) also retries rewards whose automatic
    attempts are exhausted, and rewards stuck in "sending" after a crash —
    the admin accepts that such a message may already have been delivered.
    """
    summary = RewardSummary()
    period = period_of(leaderboard)
    rows = await session.execute(
        select(WeeklyReward.id, WeeklyReward.delivery_status)
        .where(WeeklyReward.leaderboard_id == leaderboard.id)
        .order_by(WeeklyReward.rank.asc())
    )
    for reward_id, status in rows.all():
        if status == DeliveryStatus.SENT:
            summary.already_sent += 1
            continue

        eligible = [DeliveryStatus.PENDING, DeliveryStatus.FAILED]
        if manual:
            eligible.append(DeliveryStatus.SENDING)
        conditions = [WeeklyReward.id == reward_id, WeeklyReward.delivery_status.in_(eligible)]
        if not manual:
            conditions.append(WeeklyReward.delivery_attempts < MAX_AUTO_DELIVERY_ATTEMPTS)
        claimed = cast(
            CursorResult,
            await session.execute(
                update(WeeklyReward)
                .where(*conditions)
                .values(
                    delivery_status=DeliveryStatus.SENDING,
                    delivery_attempts=WeeklyReward.delivery_attempts + 1,
                )
                .execution_options(synchronize_session=False)
            ),
        )
        await session.commit()
        if claimed.rowcount == 0:
            summary.not_retried += 1
            continue

        loaded = await session.execute(
            select(WeeklyReward, PromoCode, User)
            .join(PromoCode, PromoCode.id == WeeklyReward.promo_code_id)
            .join(User, User.id == WeeklyReward.user_id)
            .where(WeeklyReward.id == reward_id)
        )
        reward, promo, user = loaded.one()
        text = render_reward_message(
            period=period, rank=reward.rank, report_count=reward.report_count, promo=promo
        )
        try:
            await bot.send_message(user.telegram_id, text)
        except Exception as exc:  # one recipient must not stop the rest
            permanent = is_permanent_delivery_error(exc)
            reason = describe_error(exc)
            values: dict[str, object] = {
                "delivery_status": DeliveryStatus.FAILED,
                "delivery_error": reason,
            }
            if permanent:
                # Use up the automatic attempts, but never lower a count that
                # admin retries have already pushed past the automatic limit.
                values["delivery_attempts"] = case(
                    (
                        WeeklyReward.delivery_attempts < MAX_AUTO_DELIVERY_ATTEMPTS,
                        MAX_AUTO_DELIVERY_ATTEMPTS,
                    ),
                    else_=WeeklyReward.delivery_attempts,
                )
            await _set_delivery(session, reward_id, **values)
            summary.failed += 1
            summary.errors.append(reason)
            logger.warning(
                "TOP reward delivery failed (leaderboard_id=%s, rank=%s, user_id=%s, permanent=%s): %s",
                leaderboard.id,
                reward.rank,
                reward.user_id,
                permanent,
                reason,
            )
            continue

        await _set_delivery(
            session,
            reward_id,
            delivery_status=DeliveryStatus.SENT,
            delivered_at=utcnow(),
            delivery_error=None,
        )
        summary.sent += 1
        logger.info(
            "TOP reward delivered (leaderboard_id=%s, rank=%s, user_id=%s)",
            leaderboard.id,
            reward.rank,
            reward.user_id,
        )
    return summary


@dataclass(frozen=True)
class RewardView:
    rank: int
    username: str | None
    report_count: int
    code: str
    delivery_status: str
    delivery_error: str | None


async def reward_views(session: AsyncSession, leaderboard_id: int) -> list[RewardView]:
    rows = await session.execute(
        select(WeeklyReward, PromoCode.code, User.username)
        .join(PromoCode, PromoCode.id == WeeklyReward.promo_code_id)
        .join(User, User.id == WeeklyReward.user_id)
        .where(WeeklyReward.leaderboard_id == leaderboard_id)
        .order_by(WeeklyReward.rank.asc())
    )
    return [
        RewardView(
            rank=reward.rank,
            username=username,
            report_count=reward.report_count,
            code=code,
            delivery_status=reward.delivery_status,
            delivery_error=reward.delivery_error,
        )
        for reward, code, username in rows.all()
    ]
