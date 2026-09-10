from __future__ import annotations

from aiogram import Router

from app.bot.handlers import analyze, fallback, history, payment, start


def build_root_router() -> Router:
    router = Router(name="root")
    router.include_router(start.router)
    router.include_router(analyze.router)
    router.include_router(payment.router)
    router.include_router(history.router)
    # Must stay last: a bare catch-all that would otherwise shadow every
    # more specific handler above.
    router.include_router(fallback.router)
    return router
