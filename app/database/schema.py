"""Startup schema gate: the code and the database schema must be in sync.

Why this exists: `Base.metadata.create_all()` only creates *missing tables*.
Started against a database that is behind the code's Alembic head, it used to
create the new tables but could never add new columns to existing ones
(e.g. analyses.report_completed_at), leaving a half-upgraded schema. The bot
then started "successfully" and failed at runtime on every query touching the
new column ("no such column"), including the weekly job's startup catch-up.

Rules applied before the bot does anything else:

- Empty database (no application tables, no Alembic revision): bootstrap it
  from the models and stamp it at the current head, so later releases can
  upgrade it with `alembic upgrade head` like any other database.
- Database at the current head: continue.
- Anything else (an older revision, an unknown revision, or application
  tables without any revision): refuse to start with an actionable message.
  Schema changes to existing data are never applied implicitly; the operator
  runs `alembic upgrade head` (see README "Database setup / migrations").
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from app.database.models import Base
from app.logging import get_logger

logger = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class SchemaOutOfDateError(RuntimeError):
    """The database schema does not match this version of the code."""


def _config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


def migration_head() -> str:
    head = ScriptDirectory.from_config(_config()).get_current_head()
    if head is None:  # pragma: no cover - the repository always ships migrations
        raise RuntimeError("no Alembic migrations found")
    return head


def _prepare(connection: Connection, head: str) -> str:
    context = MigrationContext.configure(connection)
    current = context.get_current_revision()
    app_tables = set(Base.metadata.tables) & set(inspect(connection).get_table_names())

    if current == head:
        return "current"

    if current is None and not app_tables:
        Base.metadata.create_all(connection)
        context.stamp(ScriptDirectory.from_config(_config()), head)
        return "bootstrapped"

    if current is None:
        found = "application tables without an Alembic revision"
        fix = (
            "Stamp the revision the schema actually matches, then run `alembic upgrade head` "
            '(README: "Upgrading a database that was created by init_db()").'
        )
    else:
        found = f"revision {current}"
        fix = "Back up the database, then run `alembic upgrade head`."
    raise SchemaOutOfDateError(
        f"Database schema is out of date: found {found}, this code requires revision {head}. "
        f"{fix} The bot was not started."
    )


async def prepare_database(engine: AsyncEngine) -> str:
    """Verify (or bootstrap an empty) database. Returns "current" or
    "bootstrapped"; raises SchemaOutOfDateError otherwise.
    """
    head = migration_head()
    async with engine.begin() as connection:
        outcome = await connection.run_sync(_prepare, head)
    if outcome == "bootstrapped":
        logger.info("Empty database bootstrapped at schema revision %s", head)
    else:
        logger.info("Database schema is at revision %s", head)
    return outcome
