"""Week boundaries, timezone parsing and the Monday schedule."""

from __future__ import annotations

import datetime as dt

import pytest
from _engagement_fakes import MSK, msk

from app.config import settings
from app.engagement.periods import (
    as_utc,
    last_valid_day,
    next_weekly_run,
    parse_schedule_time,
    parse_timezone,
    previous_week,
    week_containing,
)


@pytest.fixture(autouse=True)
def _moscow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "app_timezone", "+03:00")


def test_week_runs_monday_to_sunday_in_app_timezone() -> None:
    week = week_containing(msk(2026, 9, 16))  # Wednesday
    assert week.start == msk(2026, 9, 14, 0, 0)
    assert week.end == msk(2026, 9, 21, 0, 0)
    assert week.first_day == dt.date(2026, 9, 14)
    assert week.last_day == dt.date(2026, 9, 20)
    assert week.label == "14.09 — 20.09"


def test_monday_boundary_belongs_to_new_week() -> None:
    sunday_last_second = msk(2026, 9, 20, 23, 59, 59)
    monday_midnight = msk(2026, 9, 21, 0, 0, 0)
    assert week_containing(sunday_last_second).first_day == dt.date(2026, 9, 14)
    assert week_containing(monday_midnight).first_day == dt.date(2026, 9, 21)
    assert week_containing(sunday_last_second).contains(sunday_last_second)
    assert not week_containing(sunday_last_second).contains(monday_midnight)


def test_boundary_uses_app_timezone_not_utc() -> None:
    # Sunday 22:30 UTC is already Monday 01:30 in Moscow.
    moment = dt.datetime(2026, 9, 20, 22, 30, tzinfo=dt.UTC)
    assert week_containing(moment).first_day == dt.date(2026, 9, 21)


def test_previous_week() -> None:
    assert previous_week(msk(2026, 9, 21, 0, 5)).label == "14.09 — 20.09"


def test_naive_database_values_are_utc() -> None:
    naive = dt.datetime(2026, 9, 20, 21, 0)
    assert as_utc(naive) == dt.datetime(2026, 9, 20, 21, 0, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("value", "offset"),
    [("+03:00", 3 * 60), ("UTC+3", 3 * 60), ("-0530", -(5 * 60 + 30)), ("UTC", 0)],
)
def test_parse_timezone_offsets(value: str, offset: int) -> None:
    tz = parse_timezone(value)
    assert tz.utcoffset(None) == dt.timedelta(minutes=offset)


def test_parse_timezone_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        parse_timezone("Mars/Olympus")


def test_parse_schedule_time() -> None:
    assert parse_schedule_time("00:05") == dt.time(0, 5)
    with pytest.raises(ValueError):
        parse_schedule_time("5 past midnight")


def test_next_weekly_run_is_next_monday_at_configured_time() -> None:
    run_at = dt.time(0, 5)
    assert next_weekly_run(msk(2026, 9, 16), run_at, MSK) == msk(2026, 9, 21, 0, 5)
    # Monday 00:01 — today's run is still ahead.
    assert next_weekly_run(msk(2026, 9, 21, 0, 1), run_at, MSK) == msk(2026, 9, 21, 0, 5)
    # Exactly at run time — the next one is a week later.
    assert next_weekly_run(msk(2026, 9, 21, 0, 5), run_at, MSK) == msk(2026, 9, 28, 0, 5)


def test_last_valid_day_is_inclusive_display_of_exclusive_end() -> None:
    assert last_valid_day(msk(2026, 9, 28, 0, 0)) == dt.date(2026, 9, 27)
