"""Report aggregation, deterministic ranking, snapshots and user stats."""

from __future__ import annotations

import datetime as dt

import pytest
from _engagement_fakes import complete_report, make_user, msk, unfinished_analysis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import repositories as repo
from app.database.models import WeeklyLeaderboard, WeeklyLeaderboardEntry
from app.engagement import messages
from app.engagement.leaderboard import (
    compute_standings,
    count_reports,
    finalize_week,
    get_entries,
    rank_standings,
    user_stats,
)
from app.engagement.periods import week_containing

WEEK = week_containing(msk(2026, 9, 16))
AFTER_WEEK = msk(2026, 9, 21, 0, 5)


@pytest.fixture(autouse=True)
def _moscow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_timezone", "+03:00")


async def test_save_report_stamps_completion_once(session: AsyncSession) -> None:
    user = await make_user(session, 1, "a")
    analysis = await unfinished_analysis(session, user)
    assert analysis.report_completed_at is None
    await repo.save_report(session, analysis, "text")
    stamped = analysis.report_completed_at
    assert stamped is not None
    await repo.save_report(session, analysis, "text again")
    assert analysis.report_completed_at == stamped


async def test_only_completed_reports_count(session: AsyncSession) -> None:
    user = await make_user(session, 1, "a")
    await unfinished_analysis(session, user)  # unpaid / failed generation: no report
    await complete_report(session, user, msk(2026, 9, 15))
    assert await count_reports(session, user_id=user.id) == 1
    standings = await compute_standings(session, WEEK)
    assert [(s.user_id, s.report_count) for s in standings] == [(user.id, 1)]


async def test_zero_reports_user_absent_from_ranking(session: AsyncSession) -> None:
    await make_user(session, 1, "idle")
    assert await compute_standings(session, WEEK) == []


async def test_weekly_vs_lifetime(session: AsyncSession) -> None:
    user = await make_user(session, 1, "a")
    await complete_report(session, user, msk(2026, 9, 1))  # an earlier week
    await complete_report(session, user, msk(2026, 9, 14, 0, 0))  # Monday 00:00 — this week
    await complete_report(session, user, msk(2026, 9, 20, 23, 59, 59))  # Sunday end — this week
    await complete_report(session, user, msk(2026, 9, 21, 0, 0))  # next Monday — next week

    stats = await user_stats(session, user.id, now=msk(2026, 9, 18))
    assert stats.lifetime_reports == 4
    assert stats.week_reports == 2
    assert stats.week_rank == 1


def test_ranking_order_and_tie_breaks() -> None:
    t = msk(2026, 9, 15)
    rows = [
        (10, "late", 3, t + dt.timedelta(hours=5)),
        (11, "early", 3, t + dt.timedelta(hours=1)),
        (5, "most", 7, t + dt.timedelta(hours=9)),
        (20, "same_time_hi_id", 2, t),
        (12, "same_time_lo_id", 2, t),
    ]
    ranked = rank_standings(rows)
    assert [(s.rank, s.username) for s in ranked] == [
        (1, "most"),
        (2, "early"),
        (3, "late"),
        (4, "same_time_lo_id"),
        (5, "same_time_hi_id"),
    ]


def test_ranking_is_deterministic_regardless_of_input_order() -> None:
    t = msk(2026, 9, 15)
    rows = [(i, f"u{i}", i % 3, t + dt.timedelta(minutes=i % 2)) for i in range(1, 30)]
    assert rank_standings(rows) == rank_standings(list(reversed(rows)))


async def test_large_counts(session: AsyncSession) -> None:
    user = await make_user(session, 1, "power")
    for minute in range(120):
        await complete_report(session, user, msk(2026, 9, 15, 10) + dt.timedelta(minutes=minute))
    standings = await compute_standings(session, WEEK)
    assert standings[0].report_count == 120


async def test_finalize_is_idempotent_and_snapshot_immutable(session: AsyncSession) -> None:
    a = await make_user(session, 1, "alpha")
    b = await make_user(session, 2, None)
    await complete_report(session, a, msk(2026, 9, 15))
    await complete_report(session, b, msk(2026, 9, 15, 13))
    await complete_report(session, b, msk(2026, 9, 16))

    leaderboard, created = await finalize_week(session, WEEK, AFTER_WEEK)
    await session.commit()
    assert created is True
    entries = await get_entries(session, leaderboard.id)
    assert [(e.rank, e.user_id, e.report_count) for e in entries] == [(1, b.id, 2), (2, a.id, 1)]

    # Late data for that week (e.g. clock skew) must not rewrite history.
    await complete_report(session, a, msk(2026, 9, 17))
    await complete_report(session, a, msk(2026, 9, 18))
    again, created_again = await finalize_week(session, WEEK, AFTER_WEEK + dt.timedelta(days=1))
    await session.commit()
    assert created_again is False
    assert again.id == leaderboard.id
    entries = await get_entries(session, leaderboard.id)
    assert [(e.rank, e.user_id, e.report_count) for e in entries] == [(1, b.id, 2), (2, a.id, 1)]
    assert (await session.execute(select(func.count(WeeklyLeaderboard.id)))).scalar_one() == 1
    assert (await session.execute(select(func.count(WeeklyLeaderboardEntry.id)))).scalar_one() == 2


async def test_cannot_finalize_running_week(session: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await finalize_week(session, WEEK, msk(2026, 9, 20, 23))


async def test_username_snapshot_survives_rename(session: AsyncSession) -> None:
    user = await make_user(session, 1, "old_name")
    await complete_report(session, user, msk(2026, 9, 15))
    leaderboard, _ = await finalize_week(session, WEEK, AFTER_WEEK)
    await session.commit()
    await repo.get_or_create_user(session, telegram_id=1, username="new_name")
    await session.commit()
    entries = await get_entries(session, leaderboard.id)
    assert entries[0].username_snapshot == "old_name"


async def test_removed_username_is_synced(session: AsyncSession) -> None:
    await make_user(session, 1, "had_name")
    user = await repo.get_or_create_user(session, telegram_id=1, username=None)
    assert user.username is None


def test_top_rendering_privacy_and_viewer_position() -> None:
    t = msk(2026, 9, 15)
    rows = [(i, None if i == 2 else f"user{i}", 30 - i, t) for i in range(1, 16)]
    standings = rank_standings(rows)
    text = messages.render_top(standings, WEEK, viewer_user_id=14)
    assert "@user1" in text
    assert messages.ANONYMOUS_RESEARCHER in text
    assert "Ваша позиция: #14" in text
    assert "@user14" not in text  # not in the top-10 list, only as "your position"
    for telegram_like in ("id", "telegram"):
        assert telegram_like not in text.lower()


def test_top_rendering_empty_and_not_ranked_viewer() -> None:
    text = messages.render_top([], WEEK, viewer_user_id=1)
    assert "первое место свободно" in text
    assert "Вас пока нет в рейтинге" in text


def test_usernames_are_html_escaped() -> None:
    assert messages.display_name("a<b>") == "@a&lt;b&gt;"


async def test_stats_rendering_zero_reports(session: AsyncSession) -> None:
    user = await make_user(session, 1, "a")
    text = messages.render_user_stats(await user_stats(session, user.id, now=msk(2026, 9, 16)))
    assert "Разборов всего: 0" in text
    assert "Место в рейтинге" not in text


async def test_weekly_streak(session: AsyncSession) -> None:
    user = await make_user(session, 1, "a")
    for day in (1, 8, 15):  # three consecutive Tuesdays
        await complete_report(session, user, msk(2026, 9, day))
    stats = await user_stats(session, user.id, now=msk(2026, 9, 16))
    assert stats.weekly_streak == 3


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_plural_reports(count: int) -> None:
    expected = {1: "1 разбор", 2: "2 разбора", 3: "3 разбора", 4: "4 разбора"}[count]
    assert messages.plural_reports(count) == expected
    assert messages.plural_reports(11) == "11 разборов"
    assert messages.plural_reports(21) == "21 разбор"
