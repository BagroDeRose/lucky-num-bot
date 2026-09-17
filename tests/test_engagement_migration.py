"""Alembic migration c3e9a1f4d2b6 (leaderboard, promo codes, rewards).

Runs the real `alembic` CLI against throwaway SQLite files: a clean
database, and a database at the previous head holding existing data.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from app.database.models import Base

ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_HEAD = "b7c41d2e9f08"
NEW_HEAD = "c3e9a1f4d2b6"
NEW_TABLES = {
    "promo_codes",
    "promo_code_redemptions",
    "weekly_leaderboards",
    "weekly_leaderboard_entries",
    "weekly_rewards",
}


def _alembic(db: Path, *args: str) -> None:
    env = dict(os.environ, DATABASE_URL=f"sqlite+aiosqlite:///{db.as_posix()}")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr[-3000:]


def _tables(db: Path) -> set[str]:
    with sqlite3.connect(db) as con:
        return {row[0] for row in con.execute("select name from sqlite_master where type='table'")}


def _columns(db: Path, table: str) -> set[str]:
    with sqlite3.connect(db) as con:
        return {row[1] for row in con.execute(f"pragma table_info({table})")}


def _drift(db: Path) -> list:
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    try:
        with engine.connect() as conn:
            return compare_metadata(MigrationContext.configure(conn), Base.metadata)
    finally:
        engine.dispose()


def test_clean_database_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    db = tmp_path / "clean.db"
    _alembic(db, "upgrade", "head")
    assert _tables(db) >= NEW_TABLES
    assert "report_completed_at" in _columns(db, "analyses")
    assert _drift(db) == []

    _alembic(db, "downgrade", PREVIOUS_HEAD)
    assert not (NEW_TABLES & _tables(db))
    assert "report_completed_at" not in _columns(db, "analyses")

    _alembic(db, "upgrade", "head")
    assert _drift(db) == []
    with sqlite3.connect(db) as con:
        assert con.execute("select version_num from alembic_version").fetchone()[0] == NEW_HEAD


def test_existing_data_preserved_and_completion_backfilled(tmp_path: Path) -> None:
    db = tmp_path / "existing.db"
    _alembic(db, "upgrade", PREVIOUS_HEAD)

    with sqlite3.connect(db) as con:
        con.execute(
            "insert into users (id, telegram_id, username, created_at) values (1, 555, 'old', '2026-09-01 10:00:00')"
        )
        columns = (
            "user_id, number, digit_sum, final_number, money_score, luck_score, growth_score, "
            "stability_score, overall_score, algorithm_version, analysis_payload, paid, report, created_at"
        )
        base = "1, '2200373', 17, 8, 50, 50, 50, 50, 50, 'v1', '{}'"
        con.execute(f"insert into analyses (id, {columns}) values (1, {base}, 1, 'report A', '2026-09-02 09:00:00')")
        con.execute(f"insert into analyses (id, {columns}) values (2, {base}, 1, 'report B', '2026-09-03 09:00:00')")
        con.execute(f"insert into analyses (id, {columns}) values (3, {base}, 0, NULL, '2026-09-04 09:00:00')")
        # Analysis 1 has a success event (the real completion time); analysis 2 predates events.
        con.execute(
            "insert into events (user_id, name, payload, created_at) values (1, 'report_generation_success', ?, '2026-09-02 09:05:00')",
            (json.dumps({"analysis_id": 1}),),
        )
        con.commit()

    _alembic(db, "upgrade", "head")

    with sqlite3.connect(db) as con:
        rows = dict(con.execute("select id, report_completed_at from analyses order by id").fetchall())
        assert rows[1].startswith("2026-09-02 09:05:00")
        assert rows[2].startswith("2026-09-03 09:00:00")  # fallback: analysis created_at
        assert rows[3] is None  # never completed: not counted
        assert con.execute("select count(*) from users").fetchone()[0] == 1
        assert con.execute("select count(*) from events").fetchone()[0] == 1
    assert _drift(db) == []
