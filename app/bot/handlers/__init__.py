from __future__ import annotations

from aiogram import Router

from app.bot.handlers import admin, analyze, engagement, fallback, history, payment, start


def build_root_router() -> Router:
    router = Router(name="root")
    router.include_router(start.router)
    router.include_router(analyze.router)
    router.include_router(payment.router)
    router.include_router(history.router)
    router.include_router(engagement.router)
    # Admin handlers are filtered by IsAdmin; the denial router right after
    # answers everyone else who reaches an admin command, button or state.
    router.include_router(admin.router)
    router.include_router(admin.denied_router)
    # Must stay last: a bare catch-all that would otherwise shadow every
    # more specific handler above.
    router.include_router(fallback.router)
    return router
