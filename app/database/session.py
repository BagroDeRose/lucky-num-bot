"""Async SQLAlchemy engine/session setup."""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database.models import Base

engine = create_async_engine(settings.database_url, echo=False)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def _configure_sqlite_connection(dbapi_connection: object, connection_record: object) -> None:
    """SQLite-only connection setup. No-op on any other backend (Postgres in
    production — see README "PostgreSQL (production)").

    Two independent settings, both addressing real SQLite behavior rather
    than papering over it with application-level retries:

    * `foreign_keys=ON` — SQLite ignores FOREIGN KEY constraints unless
      enforcement is turned on per-connection; without this, the
      ForeignKey() declarations in app.database.models are purely
      documentation and never actually reject orphaned rows.
    * `journal_mode=WAL` + `busy_timeout` — SQLite's default rollback-journal
      mode takes an exclusive lock on the *entire file* for the duration of
      a write, and the default busy_timeout is 0ms, so a second connection
      writing at the same moment fails immediately with "database is
      locked" instead of waiting. WAL mode lets readers and a writer
      proceed concurrently, and a non-zero busy_timeout makes the driver
      wait briefly for a lock to clear before giving up — this is genuine
      SQLite configuration, not a blind retry loop. It meaningfully
      improves local/test concurrency but does not make SQLite a
      multi-writer database; PostgreSQL is the intended production backend.
    """
    if engine.dialect.name != "sqlite":
        return
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


async def init_db() -> None:
    """Create tables if they don't exist yet.

    For production schema evolution use Alembic migrations (see
    app/database/migrations). This is kept for fast local/dev bootstrap and
    for the test suite.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
