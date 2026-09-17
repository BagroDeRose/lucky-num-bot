"""Startup schema gate (regression for the production crash
"no such column: analyses.report_completed_at").

The crash: the bot was started against a database still at revision
b7c41d2e9f08. The old init_db() ran create_all, which created the new
leaderboard/promo tables but could not add analyses.report_completed_at, so
the scheduler, /stats, /top and the admin panel all failed at runtime.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import main as main_module
from app.database.models import Base
from app.database.schema import SchemaOutOfDateError, migration_head, prepare_database
from app.engagement.leaderboard import compute_standings
from app.engagement.periods import utcnow, week_containing

ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_HEAD = "b7c41d2e9f08"


def _url(db: Path) -> str:
    return f"sqlite+aiosqlite:///{db.as_posix()}"


def _alembic(db: Path, *args: str) -> None:
    env = dict(os.environ, DATABASE_URL=_url(db))
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr[-3000:]


def _schema_snapshot(db: Path) -> list[tuple]:
    with sqlite3.connect(db) as con:
        return sorted(con.execute("select type, name, sql from sqlite_master").fetchall(), key=str)


def _revision(db: Path) -> str | None:
    with sqlite3.connect(db) as con:
        row = con.execute("select version_num from alembic_version").fetchone()
    return row[0] if row else None


async def _prepare(db: Path) -> str:
    engine = create_async_engine(_url(db))
    try:
        return await prepare_database(engine)
    finally:
        await engine.dispose()


async def _make_half_upgraded(db: Path) -> None:
    """Exactly the production state: old revision + tables created by the
    old create_all-based init_db, but no new column.
    """
    _alembic(db, "upgrade", PREVIOUS_HEAD)
    engine = create_async_engine(_url(db))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


async def test_reproduces_original_crash_on_half_upgraded_schema(tmp_path: Path) -> None:
    db = tmp_path / "half.db"
    await _make_half_upgraded(db)
    engine = create_async_engine(_url(db))
    async with async_sessionmaker(engine)() as session:
        with pytest.raises(Exception, match="no such column: analyses.report_completed_at"):
            await compute_standings(session, week_containing(utcnow()))
    await engine.dispose()


async def test_out_of_date_database_is_refused_and_left_untouched(tmp_path: Path) -> None:
    db = tmp_path / "half.db"
    await _make_half_upgraded(db)
    before = _schema_snapshot(db)

    with pytest.raises(SchemaOutOfDateError) as exc:
        await _prepare(db)

    message = str(exc.value)
    assert PREVIOUS_HEAD in message and migration_head() in message
    assert "alembic upgrade head" in message
    assert _schema_snapshot(db) == before
    assert _revision(db) == PREVIOUS_HEAD


async def test_old_revision_without_new_tables_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    _alembic(db, "upgrade", PREVIOUS_HEAD)
    before = _schema_snapshot(db)
    with pytest.raises(SchemaOutOfDateError):
        await _prepare(db)
    assert _schema_snapshot(db) == before  # no partial create_all any more


async def test_after_alembic_upgrade_the_same_database_starts_and_queries_work(tmp_path: Path) -> None:
    db = tmp_path / "half.db"
    await _make_half_upgraded(db)
    _alembic(db, "upgrade", "head")

    assert await _prepare(db) == "current"
    engine = create_async_engine(_url(db))
    async with async_sessionmaker(engine)() as session:
        assert await compute_standings(session, week_containing(utcnow())) == []
    await engine.dispose()


async def test_empty_database_is_bootstrapped_at_head(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    assert await _prepare(db) == "bootstrapped"
    assert _revision(db) == migration_head()
    assert await _prepare(db) == "current"
    _alembic(db, "upgrade", "head")  # nothing to replay: no "table already exists"
    assert _revision(db) == migration_head()


async def test_unversioned_legacy_tables_are_refused(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    engine = create_async_engine(_url(db))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    with pytest.raises(SchemaOutOfDateError, match="without an Alembic revision"):
        await _prepare(db)


def test_bot_does_not_start_on_out_of_date_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    async def outdated() -> None:
        raise SchemaOutOfDateError("Database schema is out of date")

    def must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("nothing may start before the schema check passes")

    monkeypatch.setattr(main_module.settings, "bot_token", "123456:TEST")
    monkeypatch.setattr(main_module, "init_db", outdated)
    monkeypatch.setattr(main_module, "Bot", must_not_run)
    monkeypatch.setattr(main_module, "WeeklyScheduler", must_not_run)

    with pytest.raises(SystemExit) as exc:
        main_module.main()
    assert exc.value.code == 1
