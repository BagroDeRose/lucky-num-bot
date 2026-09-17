"""Weekly researcher leaderboard and per-user statistics.

WHAT COUNTS

A completed report is an analysis whose paid report was saved
(analyses.report_completed_at, stamped once in repositories.save_report).
Unpaid or cancelled payments never produce a saved report; a failed or
rejected AI generation saves nothing; a retry that finally succeeds saves the
single report of that analysis. So every completed report is counted exactly
once, at the instant it was saved.

RANKING (deterministic, no randomness)

1. more completed reports in the week ranks higher;
2. equal counts: whoever reached that count earlier — i.e. whose last
   counted report in the week was saved earlier — ranks higher;
3. still equal (identical timestamps): lower internal user id first, a
   stable last resort so the same data always yields the same order.

Ranks are unique (1, 2, 3, …), so "the TOP-5" is always exactly the first five.

LIVE vs FINALIZED

The current week is computed live from completed reports. A finished week is
frozen into weekly_leaderboards / weekly_leaderboard_entries by
finalize_week(); from then on history is read from that snapshot and never
recomputed, so later data changes cannot rewrite a past ranking.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    Analysis,
    User,
    WeeklyLeaderboard,
    WeeklyLeaderboardEntry,
)
from app.engagement.periods import (
    WeekPeriod,
    as_utc,
    month_start,
    utcnow,
    week_containing,
    week_starting,
)
from app.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class Standing:
    rank: int
    user_id: int
    username: str | None
    report_count: int
    last_report_at: dt.datetime


def rank_standings(
    rows: list[tuple[int, str | None, int, dt.datetime]],
) -> list[Standing]:
    """Apply the documented ordering to (user_id, username, count, last_at)."""
    ordered = sorted(rows, key=lambda row: (-row[2], as_utc(row[3]), row[0]))
    return [
        Standing(
            rank=index,
            user_id=user_id,
            username=username,
            report_count=count,
            last_report_at=as_utc(last_at),
        )
        for index, (user_id, username, count, last_at) in enumerate(ordered, start=1)
    ]


async def compute_standings(session: AsyncSession, period: WeekPeriod) -> list[Standing]:
    result = await session.execute(
        select(
            Analysis.user_id,
            User.username,
            func.count(Analysis.id),
            func.max(Analysis.report_completed_at),
        )
        .join(User, User.id == Analysis.user_id)
        .where(
            Analysis.report_completed_at.is_not(None),
            Analysis.report_completed_at >= period.start,
            Analysis.report_completed_at < period.end,
        )
        .group_by(Analysis.user_id, User.username)
    )
    return rank_standings([(uid, name, int(count), last) for uid, name, count, last in result.all()])


async def count_reports(
    session: AsyncSession,
    *,
    user_id: int | None = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
) -> int:
    stmt = select(func.count(Analysis.id)).where(Analysis.report_completed_at.is_not(None))
    if user_id is not None:
        stmt = stmt.where(Analysis.user_id == user_id)
    if since is not None:
        stmt = stmt.where(Analysis.report_completed_at >= as_utc(since))
    if until is not None:
        stmt = stmt.where(Analysis.report_completed_at < as_utc(until))
    return int((await session.execute(stmt)).scalar_one())


async def weekly_streak(session: AsyncSession, user_id: int, now: dt.datetime | None = None) -> int:
    """Consecutive weeks with at least one completed report, counting back
    from the current week (or from last week if this week has none yet, so a
    streak is not shown as broken on Monday morning).
    """
    now = as_utc(now or utcnow())
    current = week_containing(now)
    result = await session.execute(
        select(Analysis.report_completed_at).where(
            Analysis.user_id == user_id, Analysis.report_completed_at.is_not(None)
        )
    )
    weeks = {week_containing(as_utc(ts), current.tz).start for (ts,) in result.all()}
    if not weeks:
        return 0

    cursor = current
    if cursor.start not in weeks:
        cursor = week_containing(cursor.start - dt.timedelta(microseconds=1), current.tz)
    streak = 0
    while cursor.start in weeks:
        streak += 1
        cursor = week_containing(cursor.start - dt.timedelta(microseconds=1), current.tz)
    return streak


@dataclass(frozen=True)
class UserStats:
    lifetime_reports: int
    week_reports: int
    week_rank: int | None
    weekly_streak: int
    period: WeekPeriod


async def user_stats(session: AsyncSession, user_id: int, now: dt.datetime | None = None) -> UserStats:
    now = as_utc(now or utcnow())
    period = week_containing(now)
    standings = await compute_standings(session, period)
    own = next((s for s in standings if s.user_id == user_id), None)
    return UserStats(
        lifetime_reports=await count_reports(session, user_id=user_id),
        week_reports=own.report_count if own else 0,
        week_rank=own.rank if own else None,
        weekly_streak=await weekly_streak(session, user_id, now),
        period=period,
    )


async def finalize_week(
    session: AsyncSession, period: WeekPeriod, now: dt.datetime | None = None
) -> tuple[WeeklyLeaderboard, bool]:
    """Freeze a finished week. Idempotent: returns (snapshot, created).

    The UNIQUE week_start constraint is the guarantee — if two runs race, one
    insert wins and the other returns the winner's snapshot unchanged.
    """
    now = as_utc(now or utcnow())
    if period.end > now:
        raise ValueError("cannot finalize a week that has not ended yet")

    existing = await get_leaderboard_for_week(session, period)
    if existing is not None:
        return existing, False

    standings = await compute_standings(session, period)
    leaderboard = WeeklyLeaderboard(
        week_start=period.start, week_end=period.end, status="finalized", finalized_at=now
    )
    try:
        async with session.begin_nested():
            session.add(leaderboard)
            await session.flush()
            for standing in standings:
                session.add(
                    WeeklyLeaderboardEntry(
                        leaderboard_id=leaderboard.id,
                        user_id=standing.user_id,
                        rank=standing.rank,
                        report_count=standing.report_count,
                        last_report_at=standing.last_report_at,
                        username_snapshot=standing.username,
                    )
                )
            await session.flush()
    except IntegrityError:
        winner = await get_leaderboard_for_week(session, period)
        if winner is None:
            raise
        return winner, False

    logger.info(
        "Leaderboard finalized for week %s (leaderboard_id=%s, entries=%d)",
        period.label,
        leaderboard.id,
        len(standings),
    )
    return leaderboard, True


async def get_leaderboard_for_week(
    session: AsyncSession, period: WeekPeriod
) -> WeeklyLeaderboard | None:
    result = await session.execute(
        select(WeeklyLeaderboard).where(WeeklyLeaderboard.week_start == period.start)
    )
    return result.scalar_one_or_none()


async def get_entries(
    session: AsyncSession, leaderboard_id: int, limit: int | None = None
) -> list[WeeklyLeaderboardEntry]:
    stmt = (
        select(WeeklyLeaderboardEntry)
        .where(WeeklyLeaderboardEntry.leaderboard_id == leaderboard_id)
        .order_by(WeeklyLeaderboardEntry.rank.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def list_leaderboards(
    session: AsyncSession, offset: int = 0, limit: int = 1
) -> list[WeeklyLeaderboard]:
    result = await session.execute(
        select(WeeklyLeaderboard)
        .order_by(WeeklyLeaderboard.week_start.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def count_leaderboards(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count(WeeklyLeaderboard.id)))).scalar_one())


def period_of(leaderboard: WeeklyLeaderboard) -> WeekPeriod:
    return week_starting(leaderboard.week_start)


def month_period_start(now: dt.datetime | None = None) -> dt.datetime:
    return month_start(as_utc(now or utcnow()))
