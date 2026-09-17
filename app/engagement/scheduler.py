"""In-process weekly scheduler.

The bot is a single long-polling process, so the schedule lives inside it as
one asyncio task instead of an external cron. Correctness does not depend on
the scheduler: every phase of the weekly job is idempotent in the database,
so a missed Monday (bot down), a restart, or a second process only ever
causes "nothing left to do" runs.

- At startup the job runs once as a catch-up: if last week is already
  finalized and all its work is done, it is a no-op; if the bot was down on
  Monday, the missed work happens now.
- Then it sleeps until the next Monday WEEKLY_SCHEDULE_TIME (APP_TIMEZONE).
  Sleeps are capped so clock changes or a suspended host are noticed.
- A run that fails (an exception, or a phase that recorded an error) is
  retried with backoff instead of waiting for the next Monday.
- start() refuses to create a second task in the same process.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt

from app.config import settings
from app.engagement.automation import WeeklyAutomationService
from app.engagement.periods import app_timezone, next_weekly_run, parse_schedule_time, utcnow
from app.logging import get_logger

logger = get_logger(__name__)

MAX_SLEEP_SECONDS = 3600.0
# Failed runs are retried after 10 min, 20 min, 40 min, ... capped at 6 hours.
RETRY_BASE_SECONDS = 600
RETRY_MAX_SECONDS = 6 * 3600


class WeeklyScheduler:
    def __init__(self, service: WeeklyAutomationService) -> None:
        self._service = service
        self._task: asyncio.Task[None] | None = None
        self._run_at = parse_schedule_time(settings.weekly_schedule_time)
        self._tz = app_timezone()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def next_run(self, now: dt.datetime | None = None) -> dt.datetime:
        return next_weekly_run(now or utcnow(), self._run_at, self._tz)

    def start(self) -> bool:
        if self.running:
            logger.warning("Weekly scheduler already running; not starting a second instance")
            return False
        self._task = asyncio.create_task(self._loop(), name="weekly-scheduler")
        return True

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run_once(self) -> bool:
        """Run the job; True only if every phase succeeded and no failed
        delivery is waiting for an automatic retry."""
        try:
            report = await self._service.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Weekly job crashed")
            return False
        if not report.ok:
            logger.warning("Weekly job finished with errors: %s", report.errors)
        if report.pending_retries:
            logger.warning(
                "Weekly job left %s failed deliveries with automatic retries remaining",
                report.pending_retries,
            )
        return not report.needs_retry

    def next_attempt(self, failures: int, now: dt.datetime | None = None) -> dt.datetime:
        """After a successful run: the next scheduled Monday. After failures:
        a retry with exponential backoff, never later than the next scheduled
        run. Retrying is safe because every phase of the job is idempotent;
        waiting a whole week would delay a finished week's rewards and post.
        """
        now = now or utcnow()
        scheduled = self.next_run(now)
        if failures == 0:
            return scheduled
        delay = min(RETRY_BASE_SECONDS * 2 ** (failures - 1), RETRY_MAX_SECONDS)
        return min(now + dt.timedelta(seconds=delay), scheduled)

    async def _loop(self) -> None:
        logger.info("Weekly scheduler started; running startup catch-up")
        failures = 0 if await self._run_once() else 1
        while True:
            target = self.next_attempt(failures)
            if failures:
                logger.info(
                    "Weekly job retry #%s at %s UTC", failures, target.isoformat(timespec="minutes")
                )
            else:
                logger.info("Next weekly job at %s UTC", target.isoformat(timespec="minutes"))
            while (remaining := (target - utcnow()).total_seconds()) > 0:
                await asyncio.sleep(min(remaining, MAX_SLEEP_SECONDS))
            failures = 0 if await self._run_once() else failures + 1
