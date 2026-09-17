"""The Monday weekly job. One service, used by both the scheduler and the
admin "run now" button, so there is exactly one implementation to trust.

PHASES (every phase is idempotent through database state)

1. Finalize the previous week (UNIQUE week_start; never recomputed).
2. Build the frozen ranking (entries are written with the snapshot).
3. Pick the TOP-5 (first five stored ranks).
4. Generate personalized reward codes (UNIQUE leaderboard/user, savepoint).
5. Deliver rewards (conditional-UPDATE claim before each send).
6. Deactivate previous weekly public codes.
7. Generate this week's public code (claimed onto the leaderboard row with
   "WHERE public_promo_id IS NULL", so there is only ever one).
8. Publish the channel post (claimed the same way; failures are recorded and
   retried without regenerating the code).
9. Log the new active week.

Running the job twice — a restart, a second process, an admin click during
the scheduled run — finds the finished work in the database and does nothing
again. An in-process lock additionally serializes runs inside one process so
two runs do not interleave their claims needlessly.

Failures are isolated: a failed reward delivery does not block the public
code, and a failed channel post does not undo anything that succeeded.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import (
    DeliveryStatus,
    PromoCode,
    PromoType,
    WeeklyLeaderboard,
    WeeklyReward,
)
from app.engagement import promo as promo_service
from app.engagement.leaderboard import finalize_week, get_entries, period_of
from app.engagement.messages import RankedName, render_channel_post
from app.engagement.periods import as_utc, previous_week, utcnow, week_containing
from app.engagement.rewards import (
    MAX_AUTO_DELIVERY_ATTEMPTS,
    TOP_REWARD_COUNT,
    RewardSummary,
    deliver_rewards,
    describe_error,
    ensure_rewards,
)
from app.logging import get_logger

logger = get_logger(__name__)

PUBLIC_PROMO_PREFIX = "LUCKY"
PUBLIC_PROMO_CODE_LENGTH = 4
MAX_AUTO_CHANNEL_ATTEMPTS = 3

SessionFactory = Callable[[], AsyncSession]


class BotLike(Protocol):
    async def send_message(self, chat_id: int | str, text: str) -> object: ...


# Serializes weekly runs and admin retries inside this process.
_run_lock = asyncio.Lock()


def is_running() -> bool:
    return _run_lock.locked()


@dataclass
class WeeklyRunReport:
    week_label: str = ""
    leaderboard_id: int | None = None
    finalized_now: bool = False
    entries: int = 0
    rewards_generated: int = 0
    rewards: RewardSummary = field(default_factory=RewardSummary)
    public_promo_code: str | None = None
    public_promo_created: bool = False
    deactivated_promos: int = 0
    channel_status: str | None = None
    channel_error: str | None = None
    # Deliveries (rewards or the channel post) that failed but still have
    # automatic attempts left. The scheduler retries these soon: waiting for
    # the next Monday would move on to a different week and never retry them.
    pending_retries: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def needs_retry(self) -> bool:
        return bool(self.errors) or self.pending_retries > 0


# --- public promo -----------------------------------------------------------


def public_promo_window(
    leaderboard: WeeklyLeaderboard, now: dt.datetime
) -> tuple[dt.datetime, dt.datetime] | None:
    """The week right after the finalized one. None if that week is already
    over (a very late retry) — issuing an already-expired code is pointless.
    """
    following = week_containing(as_utc(leaderboard.week_end))
    if following.end <= now:
        return None
    return following.start, following.end


async def ensure_public_promo(
    session: AsyncSession, leaderboard: WeeklyLeaderboard, now: dt.datetime
) -> tuple[PromoCode | None, bool]:
    """Return (promo, created). Exactly one public code per leaderboard."""
    if leaderboard.public_promo_id is not None:
        return await promo_service.get_promo(session, leaderboard.public_promo_id), False

    window = public_promo_window(leaderboard, now)
    if window is None:
        return None, False
    valid_from, valid_until = window

    class _LostClaim(Exception):
        pass

    try:
        async with session.begin_nested():
            promo = await promo_service.create_generated_promo(
                session,
                prefix=PUBLIC_PROMO_PREFIX,
                length=PUBLIC_PROMO_CODE_LENGTH,
                promo_type=PromoType.WEEKLY_PUBLIC,
                discount_percent=settings.weekly_promo_discount_percent,
                max_activations=settings.weekly_promo_max_activations,
                valid_from=valid_from,
                valid_until=valid_until,
            )
            claimed = cast(
                CursorResult,
                await session.execute(
                    update(WeeklyLeaderboard)
                    .where(
                        WeeklyLeaderboard.id == leaderboard.id,
                        WeeklyLeaderboard.public_promo_id.is_(None),
                    )
                    .values(public_promo_id=promo.id)
                    .execution_options(synchronize_session=False)
                ),
            )
            if claimed.rowcount == 0:
                raise _LostClaim()
    except _LostClaim:
        # Another run attached its code first; ours was rolled back.
        await session.refresh(leaderboard)
        if leaderboard.public_promo_id is None:
            return None, False
        return await promo_service.get_promo(session, leaderboard.public_promo_id), False

    await session.refresh(leaderboard)
    logger.info(
        "Weekly public promo created (leaderboard_id=%s, promo_id=%s, discount=%s%%, limit=%s)",
        leaderboard.id,
        promo.id,
        promo.discount_percent,
        promo.max_activations,
    )
    return promo, True


async def deactivate_previous_public_promos(session: AsyncSession, keep_promo_id: int) -> int:
    result = cast(
        CursorResult,
        await session.execute(
            update(PromoCode)
            .where(
                PromoCode.promo_type == PromoType.WEEKLY_PUBLIC,
                PromoCode.is_active.is_(True),
                PromoCode.id != keep_promo_id,
            )
            .values(is_active=False, updated_at=utcnow())
            .execution_options(synchronize_session=False)
        ),
    )
    return int(result.rowcount or 0)


# --- channel ----------------------------------------------------------------


async def _set_channel(session: AsyncSession, leaderboard_id: int, **values: object) -> None:
    await session.execute(
        update(WeeklyLeaderboard)
        .where(WeeklyLeaderboard.id == leaderboard_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def build_channel_post(session: AsyncSession, leaderboard: WeeklyLeaderboard) -> str | None:
    if leaderboard.public_promo_id is None:
        return None
    promo = await promo_service.get_promo(session, leaderboard.public_promo_id)
    if promo is None:
        return None
    entries = await get_entries(session, leaderboard.id, limit=TOP_REWARD_COUNT)
    top = [RankedName(e.rank, e.username_snapshot, e.report_count) for e in entries]
    return render_channel_post(period=period_of(leaderboard), top=top, promo=promo)


async def publish_channel_post(
    session: AsyncSession, bot: BotLike, leaderboard: WeeklyLeaderboard, *, manual: bool = False
) -> str:
    """Post the weekly summary to PROMO_CHANNEL_ID. Returns the final status."""
    await session.refresh(leaderboard)
    if leaderboard.channel_status == DeliveryStatus.SENT:
        return DeliveryStatus.SENT

    channel_id = settings.promo_channel_id
    text = await build_channel_post(session, leaderboard)
    if channel_id is None or text is None:
        reason = "PROMO_CHANNEL_ID is not configured" if channel_id is None else "no public promo"
        # Conditional: another run may be sending (or have sent) the post
        # meanwhile. Overwriting its SENDING/SENT with SKIPPED would make a
        # later run post the same summary a second time.
        await session.execute(
            update(WeeklyLeaderboard)
            .where(
                WeeklyLeaderboard.id == leaderboard.id,
                WeeklyLeaderboard.channel_status.in_([DeliveryStatus.PENDING, DeliveryStatus.FAILED]),
            )
            .values(channel_status=DeliveryStatus.SKIPPED, channel_error=reason)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        await session.refresh(leaderboard)
        return leaderboard.channel_status

    eligible = [DeliveryStatus.PENDING, DeliveryStatus.SKIPPED, DeliveryStatus.FAILED]
    if manual:
        eligible.append(DeliveryStatus.SENDING)
    conditions = [
        WeeklyLeaderboard.id == leaderboard.id,
        WeeklyLeaderboard.channel_status.in_(eligible),
    ]
    if not manual:
        conditions.append(WeeklyLeaderboard.channel_attempts < MAX_AUTO_CHANNEL_ATTEMPTS)
    claimed = cast(
        CursorResult,
        await session.execute(
            update(WeeklyLeaderboard)
            .where(*conditions)
            .values(
                channel_status=DeliveryStatus.SENDING,
                channel_attempts=WeeklyLeaderboard.channel_attempts + 1,
            )
            .execution_options(synchronize_session=False)
        ),
    )
    await session.commit()
    if claimed.rowcount == 0:
        await session.refresh(leaderboard)
        return leaderboard.channel_status

    try:
        message = await bot.send_message(channel_id, text)
    except Exception as exc:  # recorded and retryable; never breaks the job
        reason = describe_error(exc)
        await _set_channel(
            session,
            leaderboard.id,
            channel_status=DeliveryStatus.FAILED,
            channel_error=reason,
        )
        logger.warning(
            "Weekly channel post failed (leaderboard_id=%s, channel=%s): %s",
            leaderboard.id,
            channel_id,
            reason,
        )
        return DeliveryStatus.FAILED

    message_id = getattr(message, "message_id", None)
    await _set_channel(
        session,
        leaderboard.id,
        channel_status=DeliveryStatus.SENT,
        channel_message_id=message_id if isinstance(message_id, int) else None,
        channel_error=None,
        channel_posted_at=utcnow(),
    )
    logger.info("Weekly channel post published (leaderboard_id=%s)", leaderboard.id)
    return DeliveryStatus.SENT


async def pending_automatic_retries(session: AsyncSession, leaderboard: WeeklyLeaderboard) -> int:
    """Failed deliveries of this week that an automatic run would still retry."""
    rewards = await session.execute(
        select(func.count(WeeklyReward.id)).where(
            WeeklyReward.leaderboard_id == leaderboard.id,
            WeeklyReward.delivery_status.in_([DeliveryStatus.PENDING, DeliveryStatus.FAILED]),
            WeeklyReward.delivery_attempts < MAX_AUTO_DELIVERY_ATTEMPTS,
        )
    )
    count = int(rewards.scalar_one())
    if (
        leaderboard.channel_status in (DeliveryStatus.PENDING, DeliveryStatus.FAILED)
        and leaderboard.channel_attempts < MAX_AUTO_CHANNEL_ATTEMPTS
        and settings.promo_channel_id is not None
    ):
        count += 1
    return count


# --- orchestration ------------------------------------------------------------


class WeeklyAutomationService:
    def __init__(self, session_factory: SessionFactory, bot: BotLike) -> None:
        self._session_factory = session_factory
        self._bot = bot

    async def run(self, now: dt.datetime | None = None, *, manual: bool = False) -> WeeklyRunReport:
        async with _run_lock:
            return await self._run_locked(as_utc(now or utcnow()), manual=manual)

    async def _run_locked(self, now: dt.datetime, *, manual: bool) -> WeeklyRunReport:
        report = WeeklyRunReport()
        period = previous_week(now)
        report.week_label = period.label
        logger.info("Weekly job started for week %s (manual=%s)", period.label, manual)

        async with self._session_factory() as session:
            # Phases 1-3: frozen snapshot with stored ranks.
            try:
                leaderboard, created = await finalize_week(session, period, now)
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logger.exception("Weekly job: finalization failed for week %s", period.label)
                report.errors.append(f"finalize: {type(exc).__name__}")
                return report
            # Plain id: after a failed phase's rollback the ORM object is
            # expired, and touching its attributes synchronously would fail.
            leaderboard_id = leaderboard.id
            report.leaderboard_id = leaderboard_id
            report.finalized_now = created
            report.entries = len(await get_entries(session, leaderboard_id))

            # Phases 4-5: personal TOP-5 rewards.
            try:
                report.rewards_generated = await ensure_rewards(session, leaderboard, now)
                await session.commit()
                report.rewards = await deliver_rewards(session, self._bot, leaderboard, manual=manual)
                report.rewards.generated = report.rewards_generated
            except Exception as exc:
                await self._recover(session, leaderboard, "rewards", exc, report)

            # Phases 6-7: public promo of the new week.
            try:
                promo, promo_created = await ensure_public_promo(session, leaderboard, now)
                if promo is not None:
                    report.public_promo_code = promo.code
                    report.public_promo_created = promo_created
                    report.deactivated_promos = await deactivate_previous_public_promos(
                        session, promo.id
                    )
                await session.commit()
            except Exception as exc:
                await self._recover(session, leaderboard, "public_promo", exc, report)

            # Phase 8: channel post.
            try:
                report.channel_status = await publish_channel_post(
                    session, self._bot, leaderboard, manual=manual
                )
            except Exception as exc:
                await self._recover(session, leaderboard, "channel", exc, report)

            try:
                await session.refresh(leaderboard)
                if leaderboard.channel_status == DeliveryStatus.FAILED:
                    report.channel_error = leaderboard.channel_error
                report.pending_retries = await pending_automatic_retries(session, leaderboard)
            except Exception as exc:
                await self._recover(session, leaderboard, "status", exc, report)

        # Phase 9.
        current = week_containing(now)
        logger.info(
            "Weekly job finished for week %s: entries=%s, rewards generated=%s sent=%s failed=%s, "
            "public promo created=%s, deactivated=%s, channel=%s, pending retries=%s, errors=%s; "
            "active week is %s",
            period.label,
            report.entries,
            report.rewards_generated,
            report.rewards.sent,
            report.rewards.failed,
            report.public_promo_created,
            report.deactivated_promos,
            report.channel_status,
            report.pending_retries,
            report.errors,
            current.label,
        )
        return report

    @staticmethod
    async def _recover(
        session: AsyncSession,
        leaderboard: WeeklyLeaderboard,
        phase: str,
        exc: Exception,
        report: WeeklyRunReport,
    ) -> None:
        """Isolate a failed phase: roll back, record, and reload the snapshot
        so later phases can still run. Whatever this phase left undone is
        picked up by the next run (every phase is idempotent). On SQLite a
        competing writer (e.g. a second process) surfaces here as
        "database is locked" — deferred work, never duplicated work.
        """
        await session.rollback()
        logger.exception("Weekly job: %s phase failed (leaderboard_id=%s)", phase, report.leaderboard_id)
        report.errors.append(f"{phase}: {type(exc).__name__}")
        await session.refresh(leaderboard)

    async def retry_rewards(self, leaderboard_id: int) -> RewardSummary | None:
        """Admin: (re)generate missing rewards and resend undelivered ones."""
        async with _run_lock, self._session_factory() as session:
            leaderboard = await session.get(WeeklyLeaderboard, leaderboard_id)
            if leaderboard is None:
                return None
            generated = await ensure_rewards(session, leaderboard)
            await session.commit()
            summary = await deliver_rewards(session, self._bot, leaderboard, manual=True)
            summary.generated = generated
            return summary

    async def retry_channel(self, leaderboard_id: int) -> str | None:
        """Admin: republish a failed/skipped channel post with the same code."""
        async with _run_lock, self._session_factory() as session:
            leaderboard = await session.get(WeeklyLeaderboard, leaderboard_id)
            if leaderboard is None:
                return None
            if leaderboard.public_promo_id is None:
                await ensure_public_promo(session, leaderboard, utcnow())
                await session.commit()
            return await publish_channel_post(session, self._bot, leaderboard, manual=True)


__all__ = [
    "MAX_AUTO_CHANNEL_ATTEMPTS",
    "MAX_AUTO_DELIVERY_ATTEMPTS",
    "WeeklyAutomationService",
    "WeeklyRunReport",
    "build_channel_post",
    "deactivate_previous_public_promos",
    "ensure_public_promo",
    "is_running",
    "publish_channel_post",
]
