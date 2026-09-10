"""Async SQLAlchemy engine/session setup."""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database.models import Base

engine = create_async_engine(settings.database_url, echo=False)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection: object, connection_record: object) -> None:
    """SQLite ignores FOREIGN KEY constraints unless enforcement is turned on
    per-connection — without this, the ForeignKey() declarations in
    app.database.models are purely documentation and never actually reject
    orphaned rows. No-op on any other backend (e.g. a future Postgres move).
    """
    if engine.dialect.name != "sqlite":
        return
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


async def init_db() -> None:
    """Create tables if they don't exist yet.

    For production schema evolution use Alembic migrations (see
    app/database/migrations). This is kept for fast local/dev bootstrap and
    for the test suite.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
