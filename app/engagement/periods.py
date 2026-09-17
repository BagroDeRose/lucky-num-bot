"""Leaderboard week boundaries — the single definition used everywhere.

A leaderboard week runs from Monday 00:00:00 to the following Monday
00:00:00 (exclusive) in the configured APP_TIMEZONE, i.e. Monday through
Sunday 23:59:59.999999 local time. Periods are always handled as timezone-
aware UTC instants internally and converted to local time only for display.

Why UTC internally: SQLite stores DateTime(timezone=True) values as naive
wall-clock text and compares them as strings. Every timestamp this app writes
is UTC, so boundaries passed to queries must be UTC too — a local-time
boundary would silently shift the week by the UTC offset.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import settings

_OFFSET_RE = re.compile(r"^(?:UTC)?\s*([+-])(\d{1,2}):?(\d{2})?$", re.IGNORECASE)


def parse_timezone(value: str) -> dt.tzinfo:
    """Parse "+03:00" / "-0530" / "UTC+3" / "UTC" / an IANA name.

    Raises ValueError with an actionable message instead of silently falling
    back to server local time, which is exactly the ambiguity a leaderboard
    week must not have.
    """
    text = (value or "").strip()
    if text.upper() in {"UTC", "Z", "GMT"}:
        return dt.UTC
    match = _OFFSET_RE.match(text)
    if match:
        sign, hours, minutes = match.groups()
        delta = dt.timedelta(hours=int(hours), minutes=int(minutes or 0))
        if delta >= dt.timedelta(hours=24):
            raise ValueError(f"APP_TIMEZONE offset out of range: {value!r}")
        return dt.timezone(-delta if sign == "-" else delta)
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"APP_TIMEZONE {value!r} is not a known timezone. Use a UTC offset such as "
            "'+03:00', or install the `tzdata` package to use IANA names like 'Europe/Moscow'."
        ) from exc


def parse_schedule_time(value: str) -> dt.time:
    try:
        hours, minutes = (int(part) for part in value.strip().split(":"))
        return dt.time(hours, minutes)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"WEEKLY_SCHEDULE_TIME must be HH:MM, got {value!r}") from exc


def app_timezone() -> dt.tzinfo:
    return parse_timezone(settings.app_timezone)


def as_utc(moment: dt.datetime) -> dt.datetime:
    """Naive datetimes read back from SQLite are UTC wall time."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass(frozen=True)
class WeekPeriod:
    """[start, end) in UTC. `end` is exclusive: the next Monday 00:00 local."""

    start: dt.datetime
    end: dt.datetime
    tz: dt.tzinfo

    @property
    def first_day(self) -> dt.date:
        return self.start.astimezone(self.tz).date()

    @property
    def last_day(self) -> dt.date:
        """The Sunday — the last calendar day inside the week."""
        return (self.end - dt.timedelta(microseconds=1)).astimezone(self.tz).date()

    @property
    def label(self) -> str:
        return f"{self.first_day:%d.%m} — {self.last_day:%d.%m}"

    def contains(self, moment: dt.datetime) -> bool:
        return self.start <= as_utc(moment) < self.end


def _local_midnight(day: dt.date, tz: dt.tzinfo) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(0), tzinfo=tz)


def week_containing(moment: dt.datetime, tz: dt.tzinfo | None = None) -> WeekPeriod:
    tz = tz or app_timezone()
    local_day = as_utc(moment).astimezone(tz).date()
    monday = local_day - dt.timedelta(days=local_day.weekday())
    start = _local_midnight(monday, tz).astimezone(dt.UTC)
    # Built from the calendar date, not start + 7 days, so a DST change inside
    # the week (IANA zones) still ends the week at local midnight.
    end = _local_midnight(monday + dt.timedelta(days=7), tz).astimezone(dt.UTC)
    return WeekPeriod(start=start, end=end, tz=tz)


def previous_week(moment: dt.datetime, tz: dt.tzinfo | None = None) -> WeekPeriod:
    current = week_containing(moment, tz)
    return week_containing(current.start - dt.timedelta(microseconds=1), current.tz)


def week_starting(start_utc: dt.datetime, tz: dt.tzinfo | None = None) -> WeekPeriod:
    """Rebuild a stored period from its start instant."""
    return week_containing(as_utc(start_utc), tz)


def month_start(moment: dt.datetime, tz: dt.tzinfo | None = None) -> dt.datetime:
    tz = tz or app_timezone()
    local = as_utc(moment).astimezone(tz)
    return _local_midnight(local.date().replace(day=1), tz).astimezone(dt.UTC)


def local_date(moment: dt.datetime, tz: dt.tzinfo | None = None) -> dt.date:
    return as_utc(moment).astimezone(tz or app_timezone()).date()


def last_valid_day(valid_until: dt.datetime, tz: dt.tzinfo | None = None) -> dt.date:
    """valid_until is an exclusive instant; users are shown the last day on
    which the code still works.
    """
    return local_date(as_utc(valid_until) - dt.timedelta(microseconds=1), tz)


def next_weekly_run(moment: dt.datetime, run_at: dt.time, tz: dt.tzinfo | None = None) -> dt.datetime:
    """The next Monday at `run_at` local time strictly after `moment` (UTC)."""
    tz = tz or app_timezone()
    week = week_containing(moment, tz)
    candidate = dt.datetime.combine(week.first_day, run_at, tzinfo=tz).astimezone(dt.UTC)
    if candidate <= as_utc(moment):
        next_monday = week.first_day + dt.timedelta(days=7)
        candidate = dt.datetime.combine(next_monday, run_at, tzinfo=tz).astimezone(dt.UTC)
    return candidate
