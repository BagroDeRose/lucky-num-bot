"""CLI: print a simple funnel breakdown from the events table.

Usage:
    python scripts/funnel_stats.py
"""

from __future__ import annotations

import asyncio

from app.database import repositories as repo
from app.database.session import async_session_factory


async def main() -> None:
    async with async_session_factory() as session:
        stats = await repo.funnel_stats(session)

    if not stats:
        print("No events recorded yet.")
        return

    width = max(len(name) for name in stats)
    for name, count in sorted(stats.items(), key=lambda kv: kv[1], reverse=True):
        print(f"{name.ljust(width)}  {count}")


if __name__ == "__main__":
    asyncio.run(main())
