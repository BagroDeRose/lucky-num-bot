"""The Monday job: TOP-5 rewards, public promo, channel post, idempotency,
failure isolation and the scheduler loop. Telegram is faked.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging

import pytest
from _engagement_fakes import (  # noqa: F401
    MSK,
    FakeBot,
    complete_report,
    make_user,
    msk,
    session_factory,
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.database.models import (
    DeliveryStatus,
    PromoCode,
    PromoType,
    WeeklyLeaderboard,
    WeeklyReward,
)
from app.engagement import automation as automation_module
from app.engagement import promo as P
from app.engagement import scheduler as scheduler_module
from app.engagement.automation import WeeklyAutomationService, WeeklyRunReport
from app.engagement.periods import last_valid_day
from app.engagement.rewards import MAX_AUTO_DELIVERY_ATTEMPTS, reward_valid_until

MONDAY_RUN = msk(2026, 9, 21, 0, 5)
CHANNEL = -100123


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_timezone", "+03:00")
    monkeypatch.setattr(settings, "promo_channel_id_raw", str(CHANNEL))
    monkeypatch.setattr(settings, "weekly_promo_discount_percent", 25)
    monkeypatch.setattr(settings, "weekly_promo_max_activations", 100)
    monkeypatch.setattr(settings, "top_reward_discount_percent", 50)
    monkeypatch.setattr(settings, "top_reward_valid_days", 7)


def _forbidden() -> TelegramForbiddenError:
    return TelegramForbiddenError(SendMessage(chat_id=1, text="x"), "Forbidden: bot was blocked by the user")


def _network() -> TelegramNetworkError:
    return TelegramNetworkError(SendMessage(chat_id=1, text="x"), "timeout")


async def _users_with_reports(
    factory: async_sessionmaker[AsyncSession], counts: list[int], *, no_username: set[int] = frozenset()  # type: ignore[assignment]
) -> list[int]:
    """Create users (telegram_id = 1..n) with the given report counts in the
    week of 14.09–20.09. Returns user ids in the same order.
    """
    ids = []
    async with factory() as s:
        for index, count in enumerate(counts, start=1):
            user = await make_user(s, index, None if index in no_username else f"user{index}")
            for n in range(count):
                await complete_report(s, user, msk(2026, 9, 15, 10) + dt.timedelta(minutes=index * 10 + n))
            ids.append(user.id)
    return ids


async def _count(factory: async_sessionmaker[AsyncSession], model: type) -> int:
    async with factory() as s:
        return int((await s.execute(select(func.count(model.id)))).scalar_one())


async def test_full_monday_run_with_more_than_five_users(
    session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    await _users_with_reports(session_factory, [1, 7, 3, 5, 2, 6, 4], no_username={2})
    bot = FakeBot()

    report = await WeeklyAutomationService(session_factory, bot).run(MONDAY_RUN)

    assert report.ok, report.errors
    assert report.week_label == "14.09 — 20.09"
    assert report.finalized_now and report.entries == 7
    assert report.rewards_generated == 5 and report.rewards.sent == 5

    # TOP-5 by count: tg 2 (7), 6 (6), 4 (5), 7 (4), 3 (3).
    for rank, telegram_id in enumerate([2, 6, 4, 7, 3], start=1):
        texts = bot.to(telegram_id)
        assert len(texts) == 1
        assert f"#{rank} место" in texts[0]
        assert "TOP50-" in texts[0]
    assert bot.to(1) == [] and bot.to(5) == []

    async with session_factory() as s:
        rewards = (await s.execute(select(WeeklyReward).order_by(WeeklyReward.rank))).scalars().all()
        assert [r.delivery_status for r in rewards] == [DeliveryStatus.SENT] * 5
        promos = (await s.execute(select(PromoCode).where(PromoCode.promo_type == PromoType.TOP_REWARD))).scalars().all()
        assert len(promos) == 5
        assert all(p.discount_percent == 50 and p.max_activations == 1 and p.target_user_id for p in promos)
        # Valid for 7 days from Monday 00:00: through Sunday 27.09.
        assert all(P.as_utc(p.valid_until) == msk(2026, 9, 28, 0, 0) for p in promos)

        public = (await s.execute(select(PromoCode).where(PromoCode.promo_type == PromoType.WEEKLY_PUBLIC))).scalar_one()
        assert public.code == report.public_promo_code and public.code.startswith("LUCKY-")
        assert (public.discount_percent, public.max_activations) == (25, 100)
        assert P.as_utc(public.valid_from) == msk(2026, 9, 21, 0, 0)
        assert P.as_utc(public.valid_until) == msk(2026, 9, 28, 0, 0)

        leaderboard = (await s.execute(select(WeeklyLeaderboard))).scalar_one()
        assert leaderboard.channel_status == DeliveryStatus.SENT
        assert leaderboard.channel_message_id is not None

    post = bot.to(CHANNEL)
    assert len(post) == 1
    assert public.code in post[0]
    assert "@user2" not in post[0]  # user 2 has no username
    assert "Исследователь без ника" in post[0]
    assert "@user6" in post[0] and "@user1" not in post[0]  # only the TOP-5


async def test_repeated_run_is_a_no_op(session_factory: async_sessionmaker[AsyncSession]) -> None:  # noqa: F811
    await _users_with_reports(session_factory, [2, 1])
    bot = FakeBot()
    service = WeeklyAutomationService(session_factory, bot)
    first = await service.run(MONDAY_RUN)
    sends_after_first = len(bot.sent)

    second = await service.run(MONDAY_RUN + dt.timedelta(minutes=1))
    third = await service.run(MONDAY_RUN + dt.timedelta(hours=5))  # e.g. restart later that day

    assert first.finalized_now and not second.finalized_now and not third.finalized_now
    assert len(bot.sent) == sends_after_first
    assert second.public_promo_code == first.public_promo_code and not second.public_promo_created
    assert await _count(session_factory, WeeklyLeaderboard) == 1
    assert await _count(session_factory, WeeklyReward) == 2
    assert await _count(session_factory, PromoCode) == 3


async def test_concurrent_runs_do_not_duplicate(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """Two services racing the same Monday. The in-process lock is disabled
    so this exercises the database guarantees, as two processes would.
    """
    monkeypatch.setattr(automation_module, "_run_lock", contextlib.nullcontext())
    await _users_with_reports(session_factory, [3, 2, 1, 1, 1, 1])
    bot = FakeBot()
    reports = await asyncio.gather(
        WeeklyAutomationService(session_factory, bot).run(MONDAY_RUN),
        WeeklyAutomationService(session_factory, bot).run(MONDAY_RUN),
    )
    # SQLite may make the losing writer fail a phase with "database is
    # locked"; that must be recorded (not crash) and finished by a later run.
    for report in reports:
        assert all(":" in error for error in report.errors)
    await WeeklyAutomationService(session_factory, bot).run(MONDAY_RUN + dt.timedelta(minutes=10))
    assert await _count(session_factory, WeeklyLeaderboard) == 1
    assert await _count(session_factory, WeeklyReward) == 5
    assert len([m for m in bot.sent if m.chat_id != CHANNEL]) == 5
    assert len(bot.to(CHANNEL)) == 1
    async with session_factory() as s:
        publics = (await s.execute(select(PromoCode).where(PromoCode.promo_type == PromoType.WEEKLY_PUBLIC))).scalars().all()
        assert len(publics) == 1


@pytest.mark.parametrize("user_count", [0, 1, 5])
async def test_fewer_or_exactly_five_users(
    session_factory: async_sessionmaker[AsyncSession], user_count: int  # noqa: F811
) -> None:
    await _users_with_reports(session_factory, [1] * user_count)
    bot = FakeBot()
    report = await WeeklyAutomationService(session_factory, bot).run(MONDAY_RUN)
    assert report.ok
    assert report.rewards_generated == user_count
    assert len([m for m in bot.sent if m.chat_id != CHANNEL]) == user_count
    assert report.public_promo_code is not None  # the weekly code is issued even for an empty week
    assert len(bot.to(CHANNEL)) == 1


async def test_ties_resolved_deterministically_for_rewards(
    session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    # Six users with 1 report each; the earliest five (tg 1..5) win.
    await _users_with_reports(session_factory, [1] * 6)
    bot = FakeBot()
    await WeeklyAutomationService(session_factory, bot).run(MONDAY_RUN)
    assert {m.chat_id for m in bot.sent if m.chat_id != CHANNEL} == {1, 2, 3, 4, 5}


async def test_previous_public_promo_deactivated(session_factory: async_sessionmaker[AsyncSession]) -> None:  # noqa: F811
    await _users_with_reports(session_factory, [1])
    bot = FakeBot()
    service = WeeklyAutomationService(session_factory, bot)
    first = await service.run(MONDAY_RUN)
    second = await service.run(MONDAY_RUN + dt.timedelta(days=7))

    assert first.public_promo_code != second.public_promo_code
    assert second.deactivated_promos == 1
    async with session_factory() as s:
        old = (await s.execute(select(PromoCode).where(PromoCode.code == first.public_promo_code))).scalar_one()
        new = (await s.execute(select(PromoCode).where(PromoCode.code == second.public_promo_code))).scalar_one()
        assert old.is_active is False and new.is_active is True


def _chat_not_found() -> TelegramBadRequest:
    return TelegramBadRequest(SendMessage(chat_id=1, text="x"), "Bad Request: chat not found")


async def test_chat_not_found_reason_is_logged_and_attempts_never_decrease(
    session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression for the production run: the log said only
    "TOP reward delivery failed (... permanent=True): TelegramBadRequest", and
    a manual retry reset delivery_attempts from 4 back to 3.
    """
    await _users_with_reports(session_factory, [2, 1])
    bot = FakeBot(fail_for={2: _chat_not_found(), CHANNEL: _chat_not_found()})
    service = WeeklyAutomationService(session_factory, bot)

    with caplog.at_level(logging.WARNING):
        report = await service.run(MONDAY_RUN)
    assert "rank=2, user_id=2, permanent=True): TelegramBadRequest" in caplog.text
    assert "Bad Request: chat not found" in caplog.text  # the actual reason, for rewards...
    assert caplog.text.count("chat not found") >= 2  # ...and for the channel post
    expected = "TelegramBadRequest: Telegram server says - Bad Request: chat not found"  # as stored in production
    assert report.rewards.errors == [expected]
    assert report.channel_error == expected

    async def attempts() -> int:
        async with session_factory() as s:
            reward = (await s.execute(select(WeeklyReward).where(WeeklyReward.rank == 2))).scalar_one()
            return reward.delivery_attempts

    assert await attempts() == MAX_AUTO_DELIVERY_ATTEMPTS  # permanent: no automatic retries left
    assert report.leaderboard_id is not None
    await service.retry_rewards(report.leaderboard_id)  # admin retry, fails again
    assert await attempts() == MAX_AUTO_DELIVERY_ATTEMPTS + 1
    await service.retry_rewards(report.leaderboard_id)
    assert await attempts() == MAX_AUTO_DELIVERY_ATTEMPTS + 2


async def test_blocked_user_does_not_stop_others_and_is_not_retried(
    session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    await _users_with_reports(session_factory, [3, 2, 1])
    bot = FakeBot(fail_for={1: _forbidden()})
    service = WeeklyAutomationService(session_factory, bot)

    report = await service.run(MONDAY_RUN)
    assert report.rewards.sent == 2 and report.rewards.failed == 1
    assert report.public_promo_code is not None
    assert len(bot.to(CHANNEL)) == 1

    async with session_factory() as s:
        failed = (await s.execute(select(WeeklyReward).where(WeeklyReward.rank == 1))).scalar_one()
        assert failed.delivery_status == DeliveryStatus.FAILED
        assert failed.delivery_attempts == MAX_AUTO_DELIVERY_ATTEMPTS
        assert "TelegramForbiddenError" in (failed.delivery_error or "")

    bot.fail_for.clear()
    again = await service.run(MONDAY_RUN + dt.timedelta(hours=1))
    assert again.rewards.sent == 0  # permanent failure: no automatic retry
    assert bot.to(1) == []

    summary = await service.retry_rewards(failed.leaderboard_id)  # admin retry
    assert summary is not None and summary.sent == 1
    assert len(bot.to(1)) == 1


async def test_transient_failure_retried_automatically_with_cap(
    session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    await _users_with_reports(session_factory, [1])
    bot = FakeBot(fail_for={1: _network()})
    service = WeeklyAutomationService(session_factory, bot)
    for hour in range(5):
        await service.run(MONDAY_RUN + dt.timedelta(hours=hour))
    async with session_factory() as s:
        reward = (await s.execute(select(WeeklyReward))).scalar_one()
        assert reward.delivery_attempts == MAX_AUTO_DELIVERY_ATTEMPTS  # not infinite
        assert reward.delivery_status == DeliveryStatus.FAILED
    bot.fail_for.clear()
    await service.run(MONDAY_RUN + dt.timedelta(hours=6))
    assert bot.to(1) == []


async def test_channel_failure_recorded_and_retry_reuses_same_code(
    session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    await _users_with_reports(session_factory, [1])
    bot = FakeBot(fail_for={CHANNEL: _network()})
    service = WeeklyAutomationService(session_factory, bot)
    report = await service.run(MONDAY_RUN)
    assert report.channel_status == DeliveryStatus.FAILED
    assert report.rewards.sent == 1  # rewards unaffected

    async with session_factory() as s:
        leaderboard = (await s.execute(select(WeeklyLeaderboard))).scalar_one()
        assert leaderboard.channel_error and "TelegramNetworkError" in leaderboard.channel_error

    bot.fail_for.clear()
    status = await service.retry_channel(leaderboard.id)
    assert status == DeliveryStatus.SENT
    assert report.public_promo_code is not None
    assert report.public_promo_code in bot.to(CHANNEL)[0]
    async with session_factory() as s:
        assert (await s.execute(select(func.count(PromoCode.id)).where(PromoCode.promo_type == PromoType.WEEKLY_PUBLIC))).scalar_one() == 1


async def test_no_channel_configured_is_skipped(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    monkeypatch.setattr(settings, "promo_channel_id_raw", "")
    await _users_with_reports(session_factory, [1])
    bot = FakeBot()
    report = await WeeklyAutomationService(session_factory, bot).run(MONDAY_RUN)
    assert report.channel_status == DeliveryStatus.SKIPPED
    assert report.public_promo_code is not None
    assert report.ok


async def test_late_run_after_downtime_still_finalizes_previous_week(
    session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    await _users_with_reports(session_factory, [2])
    bot = FakeBot()
    report = await WeeklyAutomationService(session_factory, bot).run(msk(2026, 9, 23, 15))  # Wednesday
    assert report.finalized_now and report.week_label == "14.09 — 20.09"
    async with session_factory() as s:
        reward_code = (await s.execute(select(PromoCode).where(PromoCode.promo_type == PromoType.TOP_REWARD))).scalar_one()
        # A late run still grants the full TOP_REWARD_VALID_DAYS: issued Wed 23.09,
        # valid through Tue 29.09 inclusive (7 local days), not just until Sunday.
        assert P.as_utc(reward_code.valid_until) == msk(2026, 9, 30, 0, 0)


@pytest.mark.parametrize(
    ("run_at", "expected_until"),
    [
        (msk(2026, 9, 21, 0, 5), msk(2026, 9, 28, 0, 0)),  # on schedule: through Sunday 27.09
        (msk(2026, 9, 16, 22, 23), msk(2026, 9, 23, 0, 0)),  # production case: issued Wed 16.09 22:23
        (msk(2026, 9, 20, 23, 59), msk(2026, 9, 27, 0, 0)),  # Sunday night: still 7 local days
    ],
)
def test_reward_validity_is_full_period_from_issue_day(
    run_at: dt.datetime, expected_until: dt.datetime
) -> None:
    """Regression: codes issued Wednesday 16.09 expired 20.09 21:00 UTC (4 days)
    although TOP_REWARD_VALID_DAYS=7.
    """
    assert reward_valid_until(run_at) == expected_until
    days = (last_valid_day(expected_until) - run_at.astimezone(MSK).date()).days + 1
    assert days == 7


async def test_scheduler_retries_failed_runs_with_backoff_then_resumes_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a failed startup catch-up used to log "Next weekly job at
    <next Monday>" and leave the finished week unprocessed for a whole week.
    """
    clock = [msk(2026, 9, 20, 23, 0)]  # Sunday 23:00; scheduled run Monday 00:05
    runs: list[dt.datetime] = []
    outcomes = iter(["crash", "phase_error", "ok", "ok"])

    class FakeService:
        async def run(self) -> WeeklyRunReport:
            runs.append(clock[0])
            outcome = next(outcomes)
            if outcome == "crash":
                raise RuntimeError("no such column")  # a crash must not kill the loop
            return WeeklyRunReport(errors=["finalize: OperationalError"] if outcome == "phase_error" else [])

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += dt.timedelta(seconds=seconds)
        if len(runs) == 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(scheduler_module, "utcnow", lambda: clock[0])
    monkeypatch.setattr(scheduler_module.asyncio, "sleep", fake_sleep)

    scheduler = scheduler_module.WeeklyScheduler(FakeService())  # type: ignore[arg-type]
    with pytest.raises(asyncio.CancelledError):
        await scheduler._loop()

    assert runs == [
        msk(2026, 9, 20, 23, 0),  # startup catch-up: crashed
        msk(2026, 9, 20, 23, 10),  # retry after 10 min: a phase failed
        msk(2026, 9, 20, 23, 30),  # retry after 20 more min: ok
        msk(2026, 9, 21, 0, 5),  # back on the regular Monday schedule
    ]
    assert sleeps[-1] == scheduler_module.MAX_SLEEP_SECONDS  # waiting toward the next Monday


def test_retry_never_postpones_the_scheduled_run() -> None:
    scheduler = scheduler_module.WeeklyScheduler(object())  # type: ignore[arg-type]
    now = msk(2026, 9, 20, 23, 0)
    assert scheduler.next_attempt(0, now) == msk(2026, 9, 21, 0, 5)
    assert scheduler.next_attempt(1, now) == msk(2026, 9, 20, 23, 10)
    assert scheduler.next_attempt(10, now) == msk(2026, 9, 21, 0, 5)  # backoff capped by schedule
    wednesday = msk(2026, 9, 23, 12, 0)
    assert scheduler.next_attempt(50, wednesday) == wednesday + dt.timedelta(hours=6)


async def test_scheduler_refuses_second_instance() -> None:
    class IdleService:
        async def run(self) -> None:
            await asyncio.sleep(3600)

    scheduler = scheduler_module.WeeklyScheduler(IdleService())  # type: ignore[arg-type]
    assert scheduler.start() is True
    assert scheduler.start() is False
    await scheduler.stop()
    assert not scheduler.running


@pytest.mark.parametrize("in_flight", [DeliveryStatus.SENDING, DeliveryStatus.SENT])
async def test_skip_never_overwrites_a_post_in_flight_or_sent(
    session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    in_flight: str,
) -> None:
    """Regression (found by the concurrent-run test): a run that could not see
    the public code yet overwrote another run's SENDING/SENT with SKIPPED, and
    a later run then published the same channel post a second time.
    """
    monkeypatch.setattr(settings, "promo_channel_id_raw", "")  # this run would skip
    async with session_factory() as s:
        leaderboard = WeeklyLeaderboard(
            week_start=msk(2026, 9, 14, 0, 0), week_end=msk(2026, 9, 21, 0, 0), channel_status=in_flight
        )
        s.add(leaderboard)
        await s.commit()
        status = await automation_module.publish_channel_post(s, FakeBot(), leaderboard)
    assert status == in_flight
    async with session_factory() as s:
        stored = (await s.execute(select(WeeklyLeaderboard))).scalar_one()
        assert stored.channel_status == in_flight


async def test_transient_delivery_failure_makes_scheduler_retry_soon(
    session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    """Regression for the production log: channel=failed / failed=1 with
    errors=[] was treated as success, so nothing was retried before the next
    Monday — which processes a different week and never retries this one.
    """
    await _users_with_reports(session_factory, [1])
    bot = FakeBot(fail_for={1: _network(), CHANNEL: _network()})
    service = WeeklyAutomationService(session_factory, bot)

    report = await service.run(MONDAY_RUN)
    assert report.ok  # no phase crashed...
    assert report.pending_retries == 2 and report.needs_retry  # ...but two deliveries await a retry
    scheduler = scheduler_module.WeeklyScheduler(service)
    bot.fail_for.clear()

    class Clocked:
        async def run(self) -> WeeklyRunReport:
            return await service.run(MONDAY_RUN + dt.timedelta(minutes=10))

    scheduler._service = Clocked()  # type: ignore[assignment]
    assert await scheduler._run_once() is True  # retry delivered both; back to the weekly schedule
    assert len(bot.to(1)) == 1 and len(bot.to(CHANNEL)) == 1


async def test_permanent_failures_do_not_keep_the_scheduler_retrying(
    session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    await _users_with_reports(session_factory, [1])
    bot = FakeBot(fail_for={1: _chat_not_found()})
    report = await WeeklyAutomationService(session_factory, bot).run(MONDAY_RUN)
    assert report.rewards.failed == 1
    assert report.pending_retries == 0 and not report.needs_retry
